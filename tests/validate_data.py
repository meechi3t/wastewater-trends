#!/usr/bin/env python3
"""Validate docs/data.json (and, optionally, the live upstream schema).

    python tests/validate_data.py
    python tests/validate_data.py --check-upstream   # also hits the network

Exits 0 if every check passes, 1 otherwise. Deliberately dependency-free
apart from what build_data.py already needs, so it runs anywhere the build
runs. Designed to be usable both as a test and as the pre-publish safety gate
that the GitHub Actions workflow runs before replacing docs/data.json.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

MAX_BYTES = 25 * 1024 * 1024
# A build whose newest sample is older than this is stale enough to be a bug
# (upstream publishes several times a week; sites report at least weekly).
MAX_STALENESS_DAYS = 45
# Guard against a catastrophic upstream truncation silently shipping. Chosen
# loose enough that real churn - sites joining or leaving the program, a
# retention-ladder step down - does not trip it.
MIN_SHRINK_RATIO = 0.5

failures: list[str] = []
passes = 0


def check(condition: bool, message: str) -> bool:
    global passes
    if condition:
        passes += 1
    else:
        failures.append(message)
    return condition


def validate_payload(payload: dict, size_bytes: int) -> None:
    # --- structure -------------------------------------------------------
    for key in ("meta", "sites", "pathogens", "series"):
        if not check(key in payload, f"data.json is missing top-level key {key!r}"):
            return

    meta, sites, pathogens, series = (
        payload["meta"], payload["sites"], payload["pathogens"], payload["series"]
    )

    check(isinstance(sites, list) and len(sites) >= 1, "data.json has no sites")
    check(isinstance(pathogens, list) and len(pathogens) >= 1, "data.json has no pathogens")
    check(isinstance(series, dict) and len(series) >= 1, "data.json has no series")
    check(size_bytes <= MAX_BYTES,
          f"data.json is {size_bytes / 1e6:.1f} MB, over the {MAX_BYTES / 1e6:.0f} MB budget")

    # --- metadata --------------------------------------------------------
    for key in ("schema_version", "epoch", "latest_sample_date", "measurements",
                "categories", "counts", "source"):
        check(key in meta, f"meta is missing {key!r}")
    check("retrieved_at" not in json.dumps(meta),
          "meta contains a retrieval timestamp; data.json would churn daily")

    try:
        epoch = dt.date.fromisoformat(meta["epoch"])
        latest = dt.date.fromisoformat(meta["latest_sample_date"])
    except (KeyError, ValueError) as exc:
        failures.append(f"meta dates do not parse: {exc}")
        return

    check(epoch <= latest, f"epoch {epoch} is after latest_sample_date {latest}")
    staleness = (dt.date.today() - latest).days
    check(staleness <= MAX_STALENESS_DAYS,
          f"newest sample is {staleness} days old ({latest}); upstream may be stalled")

    # --- ids and cross-references ---------------------------------------
    site_ids = {s["id"] for s in sites}
    pathogen_ids = {p["id"] for p in pathogens}
    check(len(site_ids) == len(sites), "duplicate site ids")
    check(len(pathogen_ids) == len(pathogens), "duplicate pathogen ids")

    for site in sites:
        for key in ("id", "name", "state"):
            if not check(site.get(key) not in (None, ""),
                         f"site {site.get('id')!r} has no {key!r}"):
                break
    for pathogen in pathogens:
        check(bool(pathogen.get("label")), f"pathogen {pathogen.get('id')!r} has no label")

    unknown_sites = set(series) - site_ids
    check(not unknown_sites,
          f"series reference sites absent from the site list: {sorted(unknown_sites)[:5]}")

    # --- series contents -------------------------------------------------
    total_observations = 0
    unsorted_series: list[str] = []
    duplicate_series: list[str] = []
    bad_values: list[str] = []
    unknown_pathogen_refs: set[str] = set()
    max_day = (latest - epoch).days
    n_categories = len(meta.get("categories", []))

    for site_id, by_pathogen in series.items():
        for pathogen_id, entry in by_pathogen.items():
            name = f"{site_id}/{pathogen_id}"
            if pathogen_id not in pathogen_ids:
                unknown_pathogen_refs.add(pathogen_id)
                continue

            days = entry.get("d")
            values = entry.get("v")
            if not isinstance(days, list) or not isinstance(values, list):
                bad_values.append(f"{name}: missing 'd'/'v' arrays")
                continue
            if len(days) != len(values):
                bad_values.append(f"{name}: 'd' has {len(days)} entries, 'v' has {len(values)}")
                continue

            total_observations += len(days)

            # Chronologically sorted, and no repeated date within a series.
            if any(b < a for a, b in zip(days, days[1:])):
                unsorted_series.append(name)
            if len(set(days)) != len(days):
                duplicate_series.append(name)
            if days and (days[0] < 0 or days[-1] > max_day):
                bad_values.append(f"{name}: day offsets outside [0, {max_day}]")

            for value in values:
                if value is not None and (not isinstance(value, (int, float))
                                          or isinstance(value, bool) or value < 0):
                    bad_values.append(f"{name}: bad concentration {value!r}")
                    break

            normalized = entry.get("p")
            if normalized is not None:
                if len(normalized) != len(days):
                    bad_values.append(f"{name}: 'p' length {len(normalized)} != {len(days)}")
                else:
                    for value in normalized:
                        if value is not None and (not isinstance(value, (int, float))
                                                  or isinstance(value, bool) or value < 0):
                            bad_values.append(f"{name}: bad normalized value {value!r}")
                            break

            categories = entry.get("c")
            if categories is not None:
                if len(categories) != len(days):
                    bad_values.append(f"{name}: 'c' length {len(categories)} != {len(days)}")
                elif any(not isinstance(c, int) or not 0 <= c < n_categories for c in categories):
                    bad_values.append(f"{name}: category index out of range")

    check(not unsorted_series,
          f"{len(unsorted_series)} series are not chronologically sorted, e.g. {unsorted_series[:3]}")
    check(not duplicate_series,
          f"{len(duplicate_series)} series contain duplicate dates, e.g. {duplicate_series[:3]}")
    check(not bad_values, f"{len(bad_values)} value problems, e.g. {bad_values[:3]}")
    check(not unknown_pathogen_refs,
          f"series reference unknown pathogens: {sorted(unknown_pathogen_refs)[:5]}")
    check(total_observations >= 1, "data.json contains zero observations")

    declared = meta.get("counts", {})
    check(declared.get("observations") == total_observations,
          f"meta.counts.observations says {declared.get('observations')}, "
          f"series contain {total_observations}")
    check(declared.get("sites") == len(sites),
          f"meta.counts.sites says {declared.get('sites')}, site list has {len(sites)}")
    check(declared.get("pathogens") == len(pathogens),
          f"meta.counts.pathogens says {declared.get('pathogens')}, "
          f"pathogen list has {len(pathogens)}")

    # --- measurement units must stay separable ---------------------------
    measurements = meta.get("measurements", {})
    check(set(measurements) == {"raw", "norm"},
          f"expected exactly the 'raw' and 'norm' measurements, got {sorted(measurements)}")
    units = {m.get("unit") for m in measurements.values()}
    check(len(units) == len(measurements),
          "the two measurements declare the same unit; they must not be interchangeable")

    print(f"  sites={len(sites)} pathogens={len(pathogens)} "
          f"observations={total_observations:,} range={epoch}..{latest} "
          f"size={size_bytes / 1e6:.2f} MB")


def compare_against_previous(payload: dict, previous_path: Path) -> None:
    """Refuse a build that lost most of the previous build's observations."""
    if not previous_path.exists():
        print("  (no previous data.json to compare against)")
        return
    try:
        previous = json.loads(previous_path.read_text())
    except (OSError, json.JSONDecodeError):
        print("  (previous data.json unreadable; skipping shrink check)")
        return

    before = previous.get("meta", {}).get("counts", {}).get("observations", 0)
    after = payload.get("meta", {}).get("counts", {}).get("observations", 0)
    if not before:
        return
    ratio = after / before
    check(ratio >= MIN_SHRINK_RATIO,
          f"new build has {after:,} observations vs {before:,} previously "
          f"({ratio:.0%}). That is a suspicious drop - refusing to replace "
          f"known-good data. Re-run, or raise MIN_SHRINK_RATIO deliberately "
          f"if upstream really did shrink.")

    previous_latest = previous.get("meta", {}).get("latest_sample_date")
    new_latest = payload.get("meta", {}).get("latest_sample_date")
    if previous_latest and new_latest:
        check(new_latest >= previous_latest,
              f"new build's newest sample ({new_latest}) predates the previous "
              f"build's ({previous_latest}); upstream may have served stale data")


def check_upstream_schema() -> None:
    """Confirm the live endpoints still match what the adapter expects."""
    import build_data

    print("  downloading live upstream sample for schema check...")
    plants_doc = build_data._fetch_json(build_data.PLANTS_URL)
    targets_doc = build_data._fetch_json(build_data.TARGETS_URL)
    if not check("plants" in plants_doc and "targets" in targets_doc,
                 "upstream index files no longer have the expected top-level keys"):
        return
    uid = plants_doc["plants"][0]["uid"]
    plant_doc = build_data._fetch_json(build_data.PLANT_URL.format(uid=uid))
    raw = {"plants": plants_doc["plants"], "targets": targets_doc["targets"],
           "plant_samples": {uid: plant_doc}}
    try:
        build_data.validate_source_schema(raw)
        check(True, "")
        print("  upstream schema matches the adapter's expectations")
    except build_data.SchemaChanged as exc:
        failures.append(f"upstream schema check failed: {exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=REPO_ROOT / "docs" / "data.json")
    parser.add_argument("--previous", type=Path, default=None,
                        help="A known-good data.json to compare sizes against.")
    parser.add_argument("--check-upstream", action="store_true",
                        help="Also verify the live upstream schema (network).")
    args = parser.parse_args(argv)

    print(f"Validating {args.data}")
    if not args.data.exists():
        print(f"FAIL: {args.data} does not exist", file=sys.stderr)
        return 1

    body = args.data.read_bytes()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        print(f"FAIL: {args.data} is not valid JSON: {exc}", file=sys.stderr)
        return 1

    validate_payload(payload, len(body))
    if args.previous:
        compare_against_previous(payload, args.previous)
    if args.check_upstream:
        check_upstream_schema()

    print()
    if failures:
        print(f"FAILED {len(failures)} check(s):", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print(f"OK: {passes} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
