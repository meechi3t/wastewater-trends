#!/usr/bin/env python3
"""Build docs/data.json from the WastewaterSCAN public dashboard data feed.

Pipeline (see README "Source adapter boundary"):

    download_source() -> validate_source_schema() -> normalize_source()
    -> validate_normalized_data() -> build_compact_json() -> atomic_write()

Everything that knows about the upstream shape lives in the first three
functions. Everything after them works on a single normalized long-form
DataFrame, so swapping the upstream source (for example to the Stanford
Digital Repository CSV or to CDC NWSS) means rewriting the adapter only.

Run:  python scripts/build_data.py [--out docs/data.json] [--cache DIR]
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

# --------------------------------------------------------------------------
# Upstream source (see README "Data source" for why this source was chosen)
#
# UNDOCUMENTED ENDPOINT. These URLs are the public, unauthenticated Google
# Cloud Storage objects that https://data.wastewaterscan.org/ itself fetches
# in the browser, and that its own "Download all program data" button reads.
# They are not a published, versioned API: WastewaterSCAN may change or remove
# them without notice. Every assumption this file makes about their shape is
# asserted in validate_source_schema(), which fails the build loudly rather
# than emitting a quietly wrong dataset.
# --------------------------------------------------------------------------
SOURCE_BASE = "https://storage.googleapis.com/wastewater-dev-data/json"
PLANTS_URL = f"{SOURCE_BASE}/plants.json"
TARGETS_URL = f"{SOURCE_BASE}/targets.json"
PLANT_URL = f"{SOURCE_BASE}/{{uid}}.json"

USER_AGENT = (
    "wastewater-trends/1.0 (+https://github.com/; independent open-source "
    "visualization of public WastewaterSCAN data)"
)
HTTP_TIMEOUT = 120
HTTP_RETRIES = 3
DOWNLOAD_WORKERS = 8

# Required keys. If any of these disappear upstream the build must stop.
REQUIRED_PLANT_FIELDS = {"uid", "name", "site_name", "city", "state"}
OPTIONAL_PLANT_FIELDS = {"sewershed_pop", "point", "place_name", "zipcode"}
REQUIRED_TARGET_FIELDS = {"id", "public"}
OPTIONAL_TARGET_FIELDS = {"suggested_label", "target", "gene", "reference"}
REQUIRED_SAMPLE_FIELDS = {"collection_date", "targets"}
# The two measurements we publish, plus the categorical level.
REQUIRED_MEASUREMENT_FIELDS = {"gc_g_dry_weight", "gc_g_dry_weight_pmmov"}
OPTIONAL_MEASUREMENT_FIELDS = {"activity_category", "num_wells"}

# Ordered categorical levels published by WastewaterSCAN ("activity_category").
# Index 0 means "no level published for this observation". These are the
# source's own categories; this project does not define public-health levels.
CATEGORIES = [
    "not calculated",
    "not detected",
    "very low",
    "low",
    "medium",
    "high",
    "very high",
]
CATEGORY_INDEX = {name: i for i, name in enumerate(CATEGORIES)}

# Human-readable pathogen names, captured from the WastewaterSCAN dashboard's
# own front-end configuration on 2026-09-19. Keyed by upstream assay id. Any
# assay not listed here falls back to the API's own "suggested_label", so a
# newly added target renders with the publisher's label instead of breaking.
ASSAY_DISPLAY_NAMES = {
    "N Gene": "SARS-CoV-2",
    "S Gene": "S Gene - all SARS-CoV-2",
    "RSV": "RSV",
    "Influenza A": "Influenza A",
    "Influenza B": "Influenza B",
    "InfA_H1": "H1 influenza marker",
    "InfA_H3_V2": "H3 influenza marker",
    "InfA_H5": "H5 influenza marker",
    "HMPV_4": "Human Metapneumovirus",
    "EVD68": "EVD68",
    "Noro_G2": "Norovirus",
    "Rota": "Rotavirus",
    "C_auris": "Candidozyma auris",
    "HAV": "Hepatitis A",
    "HAdV_F": "Human Adenovirus Group F",
    "HPIV": "Parainfluenza",
    "MPXV_G2R_G": "Mpox clade II",
    "MPXV_G2R_WA": "Mpox clade II",
    "MPXV_dD14-16": "Mpox clade Ib",
    "MeV_Roy": "Measles",
    "Parvo_B19": "Parvovirus",
    "WNV": "West Nile Virus",
    "NDM": "blaNDM",
    "TB_RD9": "Mycobacterium tuberculosis",
    "HV 69-70 Del": "Omicron BA.4 + BA.5 + BQ*",
    "BA.4 ORF1a Del 141-143": "Omicron BA.4",
    "BA.2 LPPA24S": "BA.2 + BA.4 + BA.5",
    "Delta 156-157": "Delta",
    "Omicron Del 143-145": "Omicron BA.1",
    "XBB_bkpt": "XBB*",
}

# WastewaterSCAN groups its pathogens into three categories on its own
# dashboard. Captured from the dashboard's front-end configuration on
# 2026-09-19, keyed by upstream assay id. Anything unmapped falls into "Other";
# PMMoV is the normalization control and gets its own group.
ASSAY_CATEGORIES = {
    "Respiratory": [
        "N Gene", "S Gene", "BA.4 ORF1a Del 141-143", "BA.2 LPPA24S",
        "Delta 156-157", "HV 69-70 Del", "Omicron Del 143-145", "XBB_bkpt",
        "BA.2.75_S:147E_S:152R",
        "Influenza A", "Influenza B", "InfA_H1", "InfA_H3_V2", "InfA_H5",
        "RSV", "HMPV_4", "EVD68", "HPIV", "Parvo_B19",
    ],
    "Gastrointestinal": ["Noro_G2", "Rota", "HAdV_F"],
    "Other": [
        "MPXV_G2R_G", "MPXV_dD14-16", "HAV", "C_auris",
        "MeV_Roy", "WNV", "NDM", "TB_RD9",
    ],
}
CATEGORY_ORDER = ["Respiratory", "Gastrointestinal", "Other", "Control"]

# Display order WITHIN each category, curated by this project so the pathogens
# people actually come looking for sit at the top of the list rather than
# wherever the alphabet puts them. Anything not listed sorts after these,
# alphabetically. This orders the picker; it ranks nothing epidemiologically.
PATHOGEN_ORDER = [
    # Respiratory
    "SC2_N", "Influenza_A", "Influenza_B", "RSV", "HMPV_4", "EV-D68",
    "HPIV", "Parvo_B19", "InfA_H1", "InfA_H3", "InfA_H5", "SC2_S",
    # Gastrointestinal
    "Noro_G2", "Rotavirus", "HAdV_F",
    # Other
    "MPXV_G2R", "MPXV_dD14-16", "HAV", "C_auris", "MeV", "WNV", "NDM", "TB_RD9",
    # Control
    "PMMoV",
]

# Plain-English names and search terms, added by THIS PROJECT so that someone
# searching "covid" or "bird flu" can find the right series. The publisher's
# own label always stays the primary name shown on screen - these are a
# secondary line and extra search keys, never a replacement. They are ordinary
# common names for the organism, not public-health categories or thresholds.
COMMON_NAMES = {
    "SC2_N":        ("COVID-19", ["covid", "covid-19", "covid19", "coronavirus", "corona", "sars-cov-2", "sars cov 2"]),
    "SC2_S":        ("COVID-19 (S gene)", ["covid", "covid-19", "coronavirus", "sars-cov-2", "spike"]),
    "Influenza_A":  ("Flu A", ["flu", "influenza"]),
    "Influenza_B":  ("Flu B", ["flu", "influenza"]),
    "InfA_H1":      ("Seasonal flu H1 marker", ["flu", "influenza", "h1", "h1n1"]),
    "InfA_H3":      ("Seasonal flu H3 marker", ["flu", "influenza", "h3", "h3n2"]),
    "InfA_H5":      ("Avian (bird) flu marker", ["bird flu", "avian", "influenza", "h5", "h5n1"]),
    "RSV":          ("Respiratory syncytial virus", ["rsv", "respiratory syncytial"]),
    "HMPV_4":       ("hMPV", ["metapneumovirus", "hmpv"]),
    "EV-D68":       ("Enterovirus D68", ["enterovirus", "evd68", "ev-d68"]),
    "HPIV":         ("Parainfluenza virus", ["parainfluenza", "hpiv", "croup"]),
    "Parvo_B19":    ("Parvovirus B19 (fifth disease)", ["parvovirus", "fifth disease", "slapped cheek", "b19"]),
    "Noro_G2":      ("Norovirus GII (stomach bug)", ["norovirus", "noro", "stomach bug", "stomach flu", "winter vomiting"]),
    "Rotavirus":    ("Rotavirus", ["rotavirus", "rota"]),
    "HAdV_F":       ("Adenovirus group F", ["adenovirus", "hadv"]),
    "MPXV_G2R":     ("Mpox (monkeypox), clade II", ["mpox", "monkeypox"]),
    "MPXV_dD14-16": ("Mpox (monkeypox), clade Ib", ["mpox", "monkeypox"]),
    "HAV":          ("Hepatitis A", ["hepatitis", "hep a", "hav"]),
    "C_auris":      ("Candida auris (drug-resistant yeast)", ["candida", "candida auris", "candidozyma", "c auris", "fungus", "yeast"]),
    "MeV":          ("Measles", ["measles", "rubeola", "mev"]),
    "WNV":          ("West Nile virus", ["west nile", "wnv"]),
    "NDM":          ("NDM antibiotic-resistance gene", ["antibiotic resistance", "antimicrobial resistance", "carbapenem", "superbug", "ndm", "amr"]),
    "TB_RD9":       ("Tuberculosis", ["tuberculosis", "tb", "mycobacterium"]),
    "PMMoV":        ("Pepper mild mottle virus - fecal-strength control", ["pmmov", "control", "normalization", "pepper"]),
}

# The two measurements published to the browser. They are NOT interchangeable
# and the dashboard never plots them on one axis.
MEASUREMENTS = {
    "raw": {
        "key": "raw",
        "label": "Concentration",
        "unit": "copies/g dry weight",
        "short_unit": "copies/g",
        "source_field": "gc_g_dry_weight",
        "description": (
            "Pathogen gene copies per gram of dry wastewater solids, as "
            "reported by WastewaterSCAN."
        ),
    },
    "norm": {
        "key": "norm",
        "label": "Normalized to PMMoV",
        "unit": "copies per million PMMoV copies",
        "short_unit": "per M PMMoV",
        "source_field": "gc_g_dry_weight_pmmov",
        "description": (
            "Concentration divided by pepper mild mottle virus (PMMoV), a "
            "fecal-strength control, multiplied by 1,000,000. This is the "
            "measure WastewaterSCAN uses for comparison between sites, "
            "because it adjusts for how dilute each sample is."
        ),
    },
}
# Upstream reports the PMMoV ratio as a bare fraction; the dashboard displays
# it per million. Matching that keeps our numbers comparable to the source.
PMMOV_SCALE = 1_000_000

SIGNIFICANT_DIGITS = 4
MAX_OUTPUT_BYTES = 25 * 1024 * 1024
# Retention ladder, in days back from the newest sample in the feed. The build
# publishes the largest window that fits under MAX_OUTPUT_BYTES. `None` means
# "everything upstream has".
RETENTION_LADDER = [None, 730, 365, 180]

# Results reach the feed a couple of days after collection, and they arrive in
# waves - a date can triple its site count over the following three days. So
# the newest sample date is a poor freshness signal: it represents a handful of
# plants, not the programme. `complete_through` is the most recent date by
# which most plants had reported, which is the date a reader should actually
# judge currency by.
#
# Plants test every two to three days on staggered schedules, so no single day
# ever has every plant. Coverage is therefore measured over a rolling 3-day
# window of distinct reporting sites, against the sites active recently.
COMPLETENESS_WINDOW_DAYS = 3
COMPLETENESS_THRESHOLD = 0.85
ACTIVE_SITE_WINDOW_DAYS = 30

SCHEMA_VERSION = 1


class SchemaChanged(RuntimeError):
    """Upstream no longer looks the way this adapter expects."""


# --------------------------------------------------------------------------
# 1. download
# --------------------------------------------------------------------------
def _fetch(url: str, cache_dir: Path | None = None) -> bytes:
    """GET `url`, retrying transient failures. Uses `cache_dir` if present."""
    cached = None
    if cache_dir is not None:
        cached = cache_dir / (url.rsplit("/", 1)[-1])
        if cached.exists():
            return cached.read_bytes()

    last_error: Exception | None = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                # urlopen raises on 4xx/5xx, so reaching here means 2xx.
                if response.status != 200:
                    raise urllib.error.HTTPError(
                        url, response.status, "unexpected status", response.headers, None
                    )
                body = response.read()
            if not body:
                raise SchemaChanged(f"Empty response body from {url}")
            if cached is not None:
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_bytes(body)
            return body
        except Exception as exc:  # noqa: BLE001 - retry anything transient
            last_error = exc
            if attempt < HTTP_RETRIES:
                time.sleep(2 * attempt)
    raise RuntimeError(f"Failed to download {url} after {HTTP_RETRIES} attempts: {last_error}")


def _fetch_json(url: str, cache_dir: Path | None = None):
    body = _fetch(url, cache_dir)
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise SchemaChanged(
            f"{url} did not return valid JSON (got {len(body)} bytes starting "
            f"{body[:80]!r}). The upstream endpoint may have moved or now "
            f"returns an error page."
        ) from exc


def download_source(cache_dir: Path | None = None) -> dict:
    """Download plants, targets, and every plant's sample history."""
    print(f"Downloading plant index  {PLANTS_URL}")
    plants_doc = _fetch_json(PLANTS_URL, cache_dir)
    print(f"Downloading target index {TARGETS_URL}")
    targets_doc = _fetch_json(TARGETS_URL, cache_dir)

    if not isinstance(plants_doc, dict) or "plants" not in plants_doc:
        raise SchemaChanged(
            "Upstream WastewaterSCAN schema changed: plants.json no longer has "
            "a top-level 'plants' key."
        )
    if not isinstance(targets_doc, dict) or "targets" not in targets_doc:
        raise SchemaChanged(
            "Upstream WastewaterSCAN schema changed: targets.json no longer has "
            "a top-level 'targets' key."
        )

    plants = plants_doc["plants"]
    targets = targets_doc["targets"]
    if not plants:
        raise SchemaChanged("Upstream returned zero plants.")
    if not targets:
        raise SchemaChanged("Upstream returned zero assay targets.")

    uids = [p.get("uid") for p in plants if p.get("uid")]
    print(f"Downloading {len(uids)} per-plant sample files ({DOWNLOAD_WORKERS} at a time)...")

    def one(uid: str):
        return uid, _fetch_json(PLANT_URL.format(uid=uid), cache_dir)

    plant_samples: dict[str, dict] = {}
    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
        for uid, doc in pool.map(one, uids):
            plant_samples[uid] = doc
    print(f"  downloaded in {time.monotonic() - started:.1f}s")

    return {"plants": plants, "targets": targets, "plant_samples": plant_samples}


# --------------------------------------------------------------------------
# 2. validate the upstream shape
# --------------------------------------------------------------------------
def _missing(required: set[str], present: set[str]) -> list[str]:
    return sorted(required - present)


def validate_source_schema(raw: dict) -> None:
    """Fail loudly if upstream dropped or renamed anything we depend on."""
    plant_fields = set().union(*(set(p) for p in raw["plants"]))
    missing = _missing(REQUIRED_PLANT_FIELDS, plant_fields)
    if missing:
        raise SchemaChanged(
            "Upstream WastewaterSCAN schema changed. plants.json is missing "
            f"required fields: {', '.join(missing)}. Present: "
            f"{', '.join(sorted(plant_fields))}"
        )

    target_fields = set().union(*(set(t) for t in raw["targets"]))
    missing = _missing(REQUIRED_TARGET_FIELDS, target_fields)
    if missing:
        raise SchemaChanged(
            "Upstream WastewaterSCAN schema changed. targets.json is missing "
            f"required fields: {', '.join(missing)}. Present: "
            f"{', '.join(sorted(target_fields))}"
        )

    # Sample shape: inspect the first plant that actually has samples, rather
    # than every one of them, so validation stays cheap.
    for uid, doc in raw["plant_samples"].items():
        samples = (doc or {}).get("samples") or []
        if not samples:
            continue
        sample_fields = set(samples[0])
        missing = _missing(REQUIRED_SAMPLE_FIELDS, sample_fields)
        if missing:
            raise SchemaChanged(
                f"Upstream WastewaterSCAN schema changed. Samples for plant "
                f"{uid} are missing required fields: {', '.join(missing)}. "
                f"Present: {', '.join(sorted(sample_fields))}"
            )
        measurements = samples[0].get("targets") or {}
        if not measurements:
            continue
        any_assay = next(iter(measurements.values()))
        missing = _missing(REQUIRED_MEASUREMENT_FIELDS, set(any_assay))
        if missing:
            raise SchemaChanged(
                "Upstream WastewaterSCAN schema changed. Per-target "
                f"measurements are missing required fields: {', '.join(missing)}. "
                f"Present: {', '.join(sorted(any_assay))}"
            )
        break
    else:
        raise SchemaChanged("Upstream returned no samples for any plant.")

    optional_seen = (plant_fields & OPTIONAL_PLANT_FIELDS) | (
        target_fields & OPTIONAL_TARGET_FIELDS
    )
    absent_optional = sorted(
        (OPTIONAL_PLANT_FIELDS | OPTIONAL_TARGET_FIELDS) - optional_seen
    )
    if absent_optional:
        # Not fatal: optional fields only enrich the output.
        print(f"  note: optional upstream fields absent: {', '.join(absent_optional)}")


# --------------------------------------------------------------------------
# 3. normalize to a long-form DataFrame
# --------------------------------------------------------------------------
def _site_records(plants: list[dict]) -> pd.DataFrame:
    rows = []
    for p in plants:
        point = p.get("point") or {}
        coords = point.get("coordinates") if isinstance(point, dict) else None
        lon, lat = (coords + [None, None])[:2] if isinstance(coords, list) else (None, None)
        rows.append(
            {
                "site_id": p["uid"],
                # "name" upstream is the plant/sewershed label ("Napa, CA");
                # "site_name" is the facility ("Soscol Water Recycling Facility").
                "site_name": p.get("site_name") or p.get("name"),
                "plant": p.get("name"),
                "city": p.get("city"),
                "state_name": p.get("state"),
                "population": p.get("sewershed_pop"),
                "lat": lat,
                "lon": lon,
            }
        )
    return pd.DataFrame(rows)


def _assay_lookup(targets: list[dict]) -> pd.DataFrame:
    """Map each upstream assay id to the publisher's own public target id.

    WastewaterSCAN swaps assay chemistries over time (`Influenza A F1R1` ->
    `Influenza A`, `EVD68` -> `EVD68_V2`, ...). Its own `public` field is the
    authoritative grouping of those assays into one continuous public series,
    so we use it rather than inventing our own mapping.
    """
    rows = []
    for t in targets:
        rows.append(
            {
                "assay": t["id"],
                "pathogen_id": t["public"],
                "suggested_label": t.get("suggested_label") or t["id"],
                "target": t.get("target"),
                "gene": t.get("gene"),
                "reference": t.get("reference"),
            }
        )
    return pd.DataFrame(rows)


def normalize_source(raw: dict) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Flatten upstream into (observations, sites, pathogens) DataFrames."""
    sites = _site_records(raw["plants"])
    assays = _assay_lookup(raw["targets"])
    assay_to_pathogen = dict(zip(assays["assay"], assays["pathogen_id"]))

    raw_field = MEASUREMENTS["raw"]["source_field"]
    norm_field = MEASUREMENTS["norm"]["source_field"]

    records = []
    unknown_assays: set[str] = set()
    source_rows = 0
    for uid, doc in raw["plant_samples"].items():
        for sample in (doc or {}).get("samples") or []:
            collection_date = sample.get("collection_date")
            for assay, measurement in (sample.get("targets") or {}).items():
                source_rows += 1
                pathogen_id = assay_to_pathogen.get(assay)
                if pathogen_id is None:
                    # A target present in the data but absent from targets.json.
                    # Skip it rather than guess how it should be grouped.
                    unknown_assays.add(assay)
                    continue
                records.append(
                    (
                        uid,
                        collection_date,
                        pathogen_id,
                        assay,
                        measurement.get(raw_field),
                        measurement.get(norm_field),
                        measurement.get("activity_category"),
                    )
                )

    if unknown_assays:
        print(
            "  note: skipped assays not listed in targets.json: "
            + ", ".join(sorted(unknown_assays))
        )

    observations = pd.DataFrame.from_records(
        records,
        columns=["site_id", "date", "pathogen_id", "assay", "raw", "norm", "category"],
    )
    print(f"  source target-observations: {source_rows:,}")

    # Clean dates: anything unparseable is unusable, not guessable.
    observations["date"] = pd.to_datetime(observations["date"], errors="coerce")
    bad_dates = int(observations["date"].isna().sum())
    if bad_dates:
        print(f"  dropped {bad_dates:,} observations with unparseable dates")
        observations = observations[observations["date"].notna()]

    # Clean numerics: coerce, then drop non-finite and physically impossible
    # (negative) concentrations. Zero is meaningful here - it is a non-detect.
    for column in ("raw", "norm"):
        observations[column] = pd.to_numeric(observations[column], errors="coerce")
    finite = observations["raw"].notna() & (observations["raw"] >= 0)
    dropped_values = int((~finite).sum())
    if dropped_values:
        print(f"  dropped {dropped_values:,} observations with missing/negative concentration")
    observations = observations[finite]
    # A negative normalized value is equally impossible; blank it rather than
    # dropping the whole observation, since the raw value is still good.
    observations.loc[observations["norm"] < 0, "norm"] = pd.NA

    observations["norm"] = observations["norm"] * PMMOV_SCALE

    observations["category"] = (
        observations["category"].map(CATEGORY_INDEX).fillna(0).astype("int8")
    )

    # Keep only sites we have metadata for.
    known_sites = set(sites["site_id"])
    observations = observations[observations["site_id"].isin(known_sites)]

    pathogens = _pathogen_records(assays, observations)
    return observations, sites, pathogens


def _pathogen_records(assays: pd.DataFrame, observations: pd.DataFrame) -> pd.DataFrame:
    """One row per public pathogen id, labelled with the publisher's own name."""
    # When several assays feed one pathogen, label it from the most recently
    # used assay so the name tracks the current chemistry.
    latest_date = {}
    if observations.empty:
        latest_assay = {}
    else:
        latest_date = {
            pid: ts.date().isoformat()
            for pid, ts in observations.groupby("pathogen_id")["date"].max().items()
        }
        newest = observations.groupby(["pathogen_id", "assay"])["date"].max().reset_index()
        newest = newest.sort_values(["pathogen_id", "date"])
        latest_assay = dict(
            zip(newest.groupby("pathogen_id").tail(1)["pathogen_id"],
                newest.groupby("pathogen_id").tail(1)["assay"])
        )

    rows = []
    for pathogen_id, group in assays.groupby("pathogen_id"):
        preferred = latest_assay.get(pathogen_id)
        chosen = group[group["assay"] == preferred]
        record = (chosen if len(chosen) else group).iloc[0]
        # Prefer the newest assay's display name, but fall back to any sibling
        # assay that has one. Upstream introduces revised chemistries as
        # "<assay>_V2"/"_Verily" variants that the dashboard config does not
        # list separately, and they should still show the pathogen's name.
        label = next(
            (
                ASSAY_DISPLAY_NAMES[a]
                for a in [record["assay"], *sorted(group["assay"])]
                if a in ASSAY_DISPLAY_NAMES
            ),
            record["suggested_label"],
        )
        is_control = pathogen_id == "PMMoV"
        common, aliases = COMMON_NAMES.get(pathogen_id, (None, []))
        rows.append(
            {
                "pathogen_id": pathogen_id,
                "label": label,
                "common": common,
                "aliases": aliases,
                "category": "Control" if is_control else _category_for(group["assay"]),
                "order": (PATHOGEN_ORDER.index(pathogen_id)
                          if pathogen_id in PATHOGEN_ORDER else 999),
                # PMMoV is the fecal-strength normalization control, not a
                # pathogen. It is published because it is real data and useful
                # context, but the dashboard labels it as a control.
                "is_control": is_control,
                "assays": sorted(group["assay"]),
                "latest": latest_date.get(pathogen_id),
                "target": record["target"],
                "gene": record["gene"],
                "reference": record["reference"],
            }
        )
    return pd.DataFrame(rows)


def _category_for(assays) -> str:
    """Map a pathogen's assays to WastewaterSCAN's own category."""
    for category, members in ASSAY_CATEGORIES.items():
        if any(assay in members for assay in assays):
            return category
    return "Other"


# --------------------------------------------------------------------------
# 4. validate the normalized data
# --------------------------------------------------------------------------
def validate_normalized_data(observations: pd.DataFrame, sites: pd.DataFrame,
                             pathogens: pd.DataFrame) -> None:
    if observations.empty:
        raise SchemaChanged("Normalization produced zero observations.")
    if sites.empty:
        raise SchemaChanged("Normalization produced zero sites.")
    if pathogens.empty:
        raise SchemaChanged("Normalization produced zero pathogens.")

    # There must be exactly one observation per (site, date, pathogen).
    # Upstream has never produced a collision here - two assays feeding one
    # public pathogen have never reported on the same sample - so a duplicate
    # means the upstream grouping changed and we must not silently average it.
    duplicated = observations.duplicated(subset=["site_id", "date", "pathogen_id"], keep=False)
    if duplicated.any():
        sample = (
            observations[duplicated]
            .sort_values(["site_id", "date", "pathogen_id"])
            .head(6)[["site_id", "date", "pathogen_id", "assay", "raw"]]
        )
        raise SchemaChanged(
            "Upstream WastewaterSCAN data changed: found "
            f"{int(duplicated.sum()):,} duplicate (site, date, pathogen) rows. "
            "Two assays now report the same public pathogen for one sample. "
            "Decide deliberately how to combine them before continuing - do "
            f"not average blindly.\nExamples:\n{sample.to_string(index=False)}"
        )

    if (observations["raw"] < 0).any():
        raise SchemaChanged("Negative concentrations survived cleaning.")


# --------------------------------------------------------------------------
# 5. compact JSON for the browser
# --------------------------------------------------------------------------
def _sig(value, digits: int = SIGNIFICANT_DIGITS):
    """Round to N significant digits.

    ddPCR concentrations carry nothing like 15 digits of precision, and full
    float repr would roughly double the download. Rounding here is both more
    honest and much smaller.
    """
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return None
    value = float(value)
    if value == 0:
        return 0
    rounded = round(value, -int(math.floor(math.log10(abs(value)))) + (digits - 1))
    # Render whole numbers as ints so the JSON has no trailing ".0".
    return int(rounded) if rounded == int(rounded) and abs(rounded) < 1e15 else rounded


def build_compact_json(observations: pd.DataFrame, sites: pd.DataFrame,
                       pathogens: pd.DataFrame, source_info: dict) -> dict:
    """Assemble the browser payload.

    Site and pathogen metadata are stored once each; observations are stored
    as parallel arrays per (site, pathogen) series to avoid repeating keys
    hundreds of thousands of times.
    """
    epoch = observations["date"].min().normalize()
    latest = observations["date"].max().normalize()

    day_offsets = (observations["date"] - epoch).dt.days.astype("int32")
    frame = observations.assign(day=day_offsets)
    frame = frame.sort_values(["site_id", "pathogen_id", "day"], kind="stable")

    series: dict[str, dict] = {}
    used_sites: set[str] = set()
    used_pathogens: set[str] = set()
    for (site_id, pathogen_id), group in frame.groupby(["site_id", "pathogen_id"], sort=True):
        days = group["day"].tolist()
        raw_values = [_sig(v) for v in group["raw"].tolist()]
        norm_values = [_sig(v) for v in group["norm"].tolist()]
        categories = group["category"].tolist()

        entry = {"d": days, "v": raw_values}
        # Only carry arrays that say something: a normalized series that is
        # entirely absent, or categories that are all "not calculated", are
        # pure overhead.
        if any(v is not None for v in norm_values):
            entry["p"] = norm_values
        if any(c for c in categories):
            entry["c"] = [int(c) for c in categories]

        series.setdefault(site_id, {})[pathogen_id] = entry
        used_sites.add(site_id)
        used_pathogens.add(pathogen_id)

    site_records = []
    for record in sites.sort_values("site_id").to_dict("records"):
        if record["site_id"] not in used_sites:
            continue
        site_records.append(
            {
                "id": record["site_id"],
                "name": record["site_name"],
                "plant": record["plant"],
                "city": record["city"],
                "state": record["state_name"],
                "pop": _int_or_none(record["population"]),
                "lat": _round_or_none(record["lat"], 4),
                "lon": _round_or_none(record["lon"], 4),
            }
        )

    pathogen_records = []
    for record in pathogens.sort_values("pathogen_id").to_dict("records"):
        if record["pathogen_id"] not in used_pathogens:
            continue
        pathogen_records.append(
            {
                "id": record["pathogen_id"],
                "label": record["label"],
                "common": _clean(record["common"]),
                "aliases": record["aliases"],
                "category": record["category"],
                "order": int(record["order"]),
                "control": bool(record["is_control"]),
                "latest": _clean(record["latest"]),
                "assays": record["assays"],
                "target": _clean(record["target"]),
                "gene": _clean(record["gene"]),
                "reference": _clean(record["reference"]),
            }
        )

    return {
        "meta": {
            "schema_version": SCHEMA_VERSION,
            "generator": "scripts/build_data.py",
            # Deliberately NO build timestamp: data.json must be byte-identical
            # for identical upstream data, so an unchanged day produces no
            # commit. Retrieval time lives in docs/source-info.json instead.
            "source": source_info,
            "epoch": epoch.date().isoformat(),
            "first_sample_date": epoch.date().isoformat(),
            "latest_sample_date": latest.date().isoformat(),
            "complete_through": _complete_through(observations, latest),
            "measurements": MEASUREMENTS,
            "categories": CATEGORIES,
            "pathogen_groups": CATEGORY_ORDER,
            "counts": {
                "sites": len(site_records),
                "pathogens": len(pathogen_records),
                "observations": int(len(frame)),
            },
        },
        "sites": site_records,
        "pathogens": pathogen_records,
        "series": series,
    }


def _complete_through(observations: pd.DataFrame, latest: pd.Timestamp) -> str | None:
    """The most recent date by which most reporting plants had reported."""
    by_day = observations.groupby("date")["site_id"].apply(set)
    if by_day.empty:
        return None

    recent_cutoff = latest - pd.Timedelta(days=ACTIVE_SITE_WINDOW_DAYS)
    active: set = set()
    for day, sites in by_day.items():
        if day >= recent_cutoff:
            active |= sites
    if not active:
        return None

    lookup = by_day.to_dict()
    for back in range(0, 60):
        day = latest - pd.Timedelta(days=back)
        window: set = set()
        for offset in range(COMPLETENESS_WINDOW_DAYS):
            window |= lookup.get(day - pd.Timedelta(days=offset), set())
        if len(window) / len(active) >= COMPLETENESS_THRESHOLD:
            return day.date().isoformat()
    return None


def _clean(value):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return value


def _int_or_none(value):
    try:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _round_or_none(value, digits):
    try:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return None
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def encode(payload: dict) -> bytes:
    # Deterministic: fixed separators, no sort_keys surprises (we build the
    # dicts in sorted order ourselves), newline-terminated.
    return (json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


# --------------------------------------------------------------------------
# 6. atomic write
# --------------------------------------------------------------------------
def atomic_write(path: Path, body: bytes) -> None:
    """Write via a temp file in the same directory, then os.replace().

    A failed or partial write must never leave a truncated data.json behind.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".data-", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(body)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="docs/data.json", type=Path)
    parser.add_argument("--source-info", default="docs/source-info.json", type=Path)
    parser.add_argument(
        "--cache",
        type=Path,
        default=None,
        help="Directory to cache downloads in (development only; never used in CI).",
    )
    args = parser.parse_args(argv)

    started = time.monotonic()
    raw = download_source(args.cache)
    validate_source_schema(raw)
    observations, sites, pathogens = normalize_source(raw)
    validate_normalized_data(observations, sites, pathogens)

    newest = observations["date"].max()
    source_info = {
        "provider": "WastewaterSCAN (Stanford University, Emory University, and Verily)",
        "dataset": "WastewaterSCAN public dashboard measurements",
        "source_url": SOURCE_BASE,
        "dashboard_url": "https://data.wastewaterscan.org/",
        "endpoint_status": (
            "Undocumented. These are the public storage objects the "
            "WastewaterSCAN dashboard itself reads; they are not a published "
            "API and may change without notice."
        ),
        "license": "CC BY-NC 4.0 (per https://data.wastewaterscan.org/about/)",
        "attribution": (
            "These data were collected as part of the WastewaterSCAN / SCAN "
            "project, a partnership between Stanford University, Emory "
            "University, and Verily funded philanthropically through a gift "
            "to Stanford University."
        ),
        "update_frequency": "Upstream refreshes daily; this dataset rebuilds daily.",
        "retrieved_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "latest_sample_date": newest.date().isoformat(),
        "notes": [
            "Raw concentrations and PMMoV-normalized values are published as "
            "separate measurements and are never combined on one axis.",
            "Assays are grouped into public pathogen series using the "
            "publisher's own targets.json 'public' field.",
            "This project is an independent visualization and is not "
            "affiliated with or endorsed by WastewaterSCAN.",
        ],
    }

    # docs/data.json must stay byte-identical when upstream has not changed,
    # so the copy of the source block embedded in it carries no wall-clock
    # time. The retrieval timestamp lives only in docs/source-info.json.
    source_info_public = {k: v for k, v in source_info.items() if k != "retrieved_at"}

    # Publish the largest retention window that stays under the size budget.
    payload = None
    body = b""
    retention_used = None
    for retention_days in RETENTION_LADDER:
        if retention_days is None:
            window = observations
        else:
            cutoff = newest - pd.Timedelta(days=retention_days)
            window = observations[observations["date"] >= cutoff]
        candidate = build_compact_json(window, sites, pathogens, source_info_public)
        encoded = encode(candidate)
        label = "all history" if retention_days is None else f"{retention_days} days"
        print(f"  candidate retention {label}: {len(encoded) / 1e6:.1f} MB")
        payload, body, retention_used = candidate, encoded, retention_days
        if len(encoded) <= MAX_OUTPUT_BYTES:
            break
    else:
        raise RuntimeError(
            f"Even the smallest retention window ({RETENTION_LADDER[-1]} days) "
            f"produced {len(body) / 1e6:.1f} MB, over the "
            f"{MAX_OUTPUT_BYTES / 1e6:.0f} MB budget. Lower RETENTION_LADDER."
        )

    payload["meta"]["retention_days"] = retention_used
    payload["meta"]["retention_note"] = (
        "Full upstream history."
        if retention_used is None
        else f"Trimmed to the most recent {retention_used} days to keep the "
             f"download under {MAX_OUTPUT_BYTES // (1024 * 1024)} MB."
    )
    body = encode(payload)

    atomic_write(args.out, body)
    atomic_write(
        args.source_info,
        (json.dumps(source_info, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
    )

    meta = payload["meta"]
    print()
    print("Build summary")
    print("-------------")
    print(f"  source rows (target-observations) : {len(observations):,} usable")
    print(f"  retained observations             : {meta['counts']['observations']:,}")
    print(f"  retention window                  : {meta['retention_note']}")
    print(f"  sites                             : {meta['counts']['sites']}")
    print(f"  pathogens                         : {meta['counts']['pathogens']}")
    print(f"  date range                        : {meta['first_sample_date']} -> {meta['latest_sample_date']}")
    print(f"  {args.out}                        : {len(body) / 1e6:.2f} MB")
    print(f"  elapsed                           : {time.monotonic() - started:.1f}s")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SchemaChanged as error:
        print(f"\nERROR: {error}", file=sys.stderr)
        sys.exit(2)
