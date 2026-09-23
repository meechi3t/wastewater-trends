# Wastewater Trends

A free, zero-infrastructure dashboard for exploring [WastewaterSCAN](https://www.wastewaterscan.org/)
pathogen surveillance data.

Pick a treatment plant and see what is being detected there, compare several
pathogens at one site or one pathogen across several sites, look at 30 days or
six years, and bookmark the exact view you built.

There is no server, no database, and no API key. A scheduled GitHub Action
rebuilds a single JSON file once a day; GitHub Pages serves it and one HTML
file. Running this costs nothing.

---

## Architecture

```
  WastewaterSCAN public data feed          (upstream, refreshed daily)
                 |
                 |  scripts/build_data.py   download -> validate schema ->
                 |                          normalize -> validate -> compact
                 v
  .github/workflows/update-data.yml         daily cron; builds to a staging
                 |                          dir, validates, then promotes and
                 |                          commits only if the data changed
                 v
  docs/data.json  (21 MB, ~4 MB gzipped over the wire)
  docs/index.html (one file: HTML + CSS + JS, no build step)
                 |
                 v
  GitHub Pages -> the browser fetches data.json once and does the rest locally
```

The browser never talks to WastewaterSCAN. It only ever loads `data.json` from
the same origin, plus Chart.js from cdnjs and one web font from Google Fonts.

---

## Data source

**Source used:** the public data feed behind the official WastewaterSCAN
dashboard at <https://data.wastewaterscan.org/> — the Google Cloud Storage
objects `plants.json`, `targets.json`, and one file per plant under
`https://storage.googleapis.com/wastewater-dev-data/json/`.

**Publisher:** WastewaterSCAN, a partnership between Stanford University,
Emory University, and Verily.

**Licence:** the WastewaterSCAN dashboard states that its content is licensed
**CC BY-NC 4.0**. Required attribution, reproduced in the app footer and in
`docs/source-info.json`:

> These data were collected as part of the WastewaterSCAN / SCAN project, a
> partnership between Stanford University, Emory University, and Verily funded
> philanthropically through a gift to Stanford University.

**Update frequency:** upstream refreshes daily. During development the feed's
`Last-Modified` was the same day it was fetched, and the newest sample was two
days old.

### Why this source, and not the other two

Three candidates were investigated, in the order recommended for this kind of
project — an official archival deposit first, a government redistribution
second, and the dashboard's own feed only as a last resort.

**1. Stanford Digital Repository — rejected: the data is frozen.**
The WastewaterSCAN data descriptor is deposited at
[doi:10.25740/hj801ns5929](https://doi.org/10.25740/hj801ns5929)
(PURL `purl.stanford.edu/hj801ns5929`), a 20 MB CSV named
`wwscan_data_descriptor_SDR_2024_Update.csv`, licensed CC BY-NC-ND 4.0. It is
the companion dataset to Boehm et al. in *Scientific Data*.

This deposit was downloaded and inspected rather than taken on trust. It is
a wide CSV — one row per plant-day, one column triple (value, LCI, UCI) per
assay — covering **191 plants from 2022-01-01 to 2024-06-30**, 48,758 rows,
and the file's `Last-Modified` is 2024-09-20. It is a frozen research snapshot
published alongside a paper, not a living feed. As of this build it is more
than two years stale, which makes it unusable for a dashboard whose entire
purpose is showing current trends. Its CC BY-**ND** (NoDerivatives) term is
also a poor fit for a project that reshapes the data for plotting.

**2. CDC NWSS — rejected: unavailable, and a different measurement.**
`data.cdc.gov` returned **HTTP 503 "Site Currently Unavailable"** across the
whole Socrata domain throughout this build — the catalog API, `api/views.json`,
and the known NWSS resource ids `2ew6-ywp6` and `g653-rqe2` alike. Beyond the
outage, NWSS is a different programme: it publishes percentile- and
trend-based metrics for participating sewersheds rather than WastewaterSCAN's
per-assay concentrations, so it could not have backed the same dashboard
without changing what the numbers mean. Coverage also differs — NWSS is
broader for SARS-CoV-2 and much narrower for the two dozen other targets
WastewaterSCAN reports.

**3. The dashboard's own feed — selected.**
With the official archive frozen and the government mirror down, the remaining
machine-readable source is the feed the dashboard itself reads. It is public,
unauthenticated, needs no key, and is exactly the data the site renders. It is
also what the dashboard's own **"Download all program data"** button fetches —
the app has no bulk endpoint, so that button loops over the same per-plant
files this pipeline downloads. Bulk access is a feature the publisher offers,
not something this project worked around.

No HTML was scraped. The feed is JSON.

**This endpoint is undocumented.** It is not a published, versioned API and
WastewaterSCAN may change or withdraw it without notice. Three things contain
that risk:

- every upstream URL and field name lives at the top of `scripts/build_data.py`,
  in the `download_source` / `validate_source_schema` / `normalize_source`
  adapter, and nowhere else;
- `validate_source_schema()` asserts every field the build depends on and
  **fails the build loudly** — `Upstream WastewaterSCAN schema changed. …
  is missing required fields: …` — rather than emitting a quietly wrong dataset;
- the workflow builds to a staging directory and only promotes over
  `docs/data.json` after validation passes, so a broken upstream leaves the
  last known good dataset in place.

### Fields used

| Normalized field | Upstream origin |
|---|---|
| site id | `plants[].uid` (stable short id; preferred over names) |
| site name | `plants[].site_name` (the facility) |
| plant / sewershed | `plants[].name` |
| city, state | `plants[].city`, `plants[].state` |
| population, lat/lon | `plants[].sewershed_pop`, `plants[].point.coordinates` |
| sample date | `samples[].collection_date` |
| pathogen id | `targets[].public` (see below) |
| concentration | `samples[].targets.<assay>.gc_g_dry_weight` |
| normalized value | `samples[].targets.<assay>.gc_g_dry_weight_pmmov` |
| categorical level | `samples[].targets.<assay>.activity_category` |

### The two measurements

The dashboard offers two measurements and **never plots them on the same
axis**:

- **Concentration** — `gc_g_dry_weight`, gene copies per gram of dry
  wastewater solids.
- **Normalized to PMMoV** — `gc_g_dry_weight_pmmov`, the concentration divided
  by pepper mild mottle virus, a fecal-strength control, multiplied by
  1,000,000 to match the source's own presentation. Because it adjusts for how
  dilute each sample is, this is the more defensible measure for **comparing
  one pathogen across different sites**.

Raw concentrations are in the same unit at every site, but sewer systems
differ in dilution, industrial input, and travel time, so a higher number at
site A than site B does not by itself mean more infection at A. That is why
both are offered and the choice is explicit, rather than presenting raw
concentrations as if they were comparable.

### Assay grouping

WastewaterSCAN changes assay chemistry over time: `Influenza A F1R1` was
replaced by `Influenza A`, `EVD68` by `EVD68_V2`, `MeV_Roy` by `MeV_Roy_V2`,
and `InfA_H1` has three generations. Rather than invent a mapping, the build
uses the publisher's own `targets[].public` field, which groups those assays
into one continuous public series.

This was verified against the real data, not assumed: across all 1,477,982
observations there is **not one case** of two assays reporting the same public
pathogen for the same sample. The lab swaps chemistry cleanly, so no
aggregation of duplicates is needed. `validate_normalized_data()` enforces
that invariant and **fails the build** if a duplicate `(site, date, pathogen)`
ever appears, with an explicit instruction not to average blindly — because if
that ever happens it means the upstream grouping changed and somebody needs to
decide what it should mean.

Pathogen display names ("SARS-CoV-2", "Norovirus", "Candidozyma auris") come
from the WastewaterSCAN dashboard's own front-end configuration, captured in
`ASSAY_DISPLAY_NAMES`. An assay that is not in that table falls back to the
API's own `suggested_label`, so a newly added target renders with the
publisher's wording instead of breaking. No public-health terminology is
invented here.

Pathogens are also grouped into **Respiratory / Gastrointestinal / Other**,
again using WastewaterSCAN's own categories (`ASSAY_CATEGORIES`), with PMMoV
in its own "Control" group.

### Common names (added by this project)

The source labels a series by its virus, not its disease — so somebody looking
for "COVID" finds nothing, and "blaNDM" or "TB_RD9" mean little to a general
reader. `COMMON_NAMES` in `scripts/build_data.py` adds, **for each pathogen**:

- a plain-English name shown as a second line under the label
  (`SARS-CoV-2` → "COVID-19", `TB_RD9` → "Tuberculosis"), and
- extra search keys, so "covid", "bird flu", "stomach bug", "monkeypox",
  "superbug" and "candida" all find the right series.

The publisher's label always stays the primary name on screen; these are
additive. They are ordinary common names for the organism — not public-health
categories, severity levels, or thresholds, none of which this project defines.

`PATHOGEN_ORDER` likewise sets the order *within* each category so that the
commonly-wanted series lead the list instead of landing wherever the alphabet
puts them. It orders a dropdown; it ranks nothing epidemiologically.

Each pathogen also carries its `latest` sample date. The dashboard marks
anything with no sample in 120 days as **"not reported now"** and sorts it to
the bottom of its group, so nobody picks a discontinued marker — several
SARS-CoV-2 variant assays ended in 2022–23 — and gets an empty chart.

PMMoV is included as a series but flagged `control: true` — it is the
normalization control, not a pathogen — and sorts last in the picker.

---

## Local development

```sh
pip install pandas

# Build the dataset (downloads ~555 MB from upstream, takes a minute or two)
python scripts/build_data.py

# Check it
python tests/validate_data.py

# Serve the dashboard (do NOT open index.html with file://; fetch() will fail)
cd docs && python -m http.server 8000
# -> http://localhost:8000
```

`scripts/build_data.py --cache DIR` caches downloads in `DIR` so repeat local
runs are instant. It is a development convenience and is never used in CI.

`python tests/validate_data.py --check-upstream` additionally hits the network
to confirm the live feed still matches what the adapter expects.

---

## Deployment

1. Create an empty repository on GitHub.
2. Push this repository to it:
   ```sh
   git remote add origin git@github.com:<you>/<repo>.git
   git push -u origin main
   ```
3. **Settings → Pages**: set *Source* to **Deploy from a branch**, branch
   **`main`**, folder **`/docs`**, and Save.
4. **Actions** tab → **Update data** → **Run workflow** to do the first
   refresh manually. (The repository already ships a built `docs/data.json`,
   so the site works before this runs.)
5. Open `https://<you>.github.io/<repo>/` and confirm the status strip reads
   "Data through …" with a recent date and that the chart draws.

After that the workflow runs itself daily at 09:20 UTC.

If the Actions run fails to push, check **Settings → Actions → General →
Workflow permissions** is set to *Read and write permissions*.

### Data refresh

`.github/workflows/update-data.yml` runs daily and on demand. It:

1. builds into `build/` — never straight over the published file;
2. runs `tests/validate_data.py` against the new file, comparing it to the
   current `docs/data.json`;
3. promotes the new file only if every check passed;
4. commits **only when `docs/data.json` actually changed**, as
   `data: update wastewater surveillance dataset`.

`docs/source-info.json` carries a `retrieved_at` timestamp and so changes on
every run; the workflow discards it when the data itself did not change, so an
uneventful day produces no commit at all. For the same reason `data.json`
deliberately contains **no build timestamp** — two runs against identical
upstream data produce byte-identical output. (Verified: three consecutive
builds from the same source produced the same SHA-256.)

The build fails visibly, leaving the previous dataset in place, if the
download fails, the upstream schema changed, the JSON does not parse, there
are zero sites / pathogens / observations, the newest sample is more than 45
days old, the new build has less than half the previous observation count, or
the output exceeds 25 MB.

---

## Sharing a view

Every control writes to the URL via `history.replaceState()` — the page never
reloads. Copy the address bar, or use **Copy view link**, and the recipient
gets the identical dashboard.

```
?mode=site&sites=dd36fbfb&pathogens=SC2_N,RSV&measure=raw&range=90d&avg=0&scale=linear
?mode=pathogen&state=Texas&sites=5a3fce0a,7daae816&pathogens=SC2_N&measure=norm&range=1y&avg=1&scale=log
```

| Parameter | Values |
|---|---|
| `mode` | `site` (one site, many pathogens) or `pathogen` (one pathogen, many sites) |
| `state` | full state name, e.g. `Texas`; omitted means all states |
| `sites` | comma-separated stable site ids |
| `pathogens` | comma-separated pathogen ids (`pathogen` is accepted as an alias) |
| `measure` | `raw` or `norm` |
| `range` | `30d`, `90d`, `1y`, `all` |
| `avg` | `1` or `0` — 7-day average |
| `scale` | `linear` or `log` |

Settings are also mirrored to `localStorage`, so returning to the bare URL
restores your last view. Priority is **URL parameters → localStorage →
defaults**. Only configuration is stored, never the dataset.

Anything unrecognised — a site that left the programme, a hand-edited range, a
typo — is dropped and replaced with a working default rather than erroring.

---

## Two views

The site opens in **Simple view**, which is the one most people want. It asks a
single question — where do you live — and then answers "what is going around
here" in words.

**Simple view**

- **Only plants that are actually reporting.** 45 of the 193 plants in the feed
  left the programme and have not tested since 2024; the split is clean, with
  every other site reporting within the last month. The simple view offers only
  the 148 live ones. Searching a town whose plant has stopped says so, with its
  last test date, and offers the nearest plants that are still reporting.
  Geolocation skips dormant plants too, and a dormant plant arriving from a
  bookmark or an old saved choice gets the same explanation instead of a page
  of blank cards. (The detailed view still lists all 193 — historical data is
  legitimately useful there.)
- **One decision.** Pick the nearest treatment plant, by typing a town, city or
  state, or with **Use my location** (client-side only: coordinates are compared
  against the plant list in the browser and never leave it, because there is no
  backend for them to go to). The choice is remembered, so it is asked once.
- **Answers in words, not numbers.** Each illness shows WastewaterSCAN's own
  published level — *not detected / very low / low / medium / high / very high*
  — as the headline, with a five-segment meter and a plain sentence for
  direction ("and rising", "and none found recently"). No concentrations, no
  units, no scale choices. These are the source's categories; this project
  does not define levels of its own.
- **Five illnesses**, chosen because they are household names and the source
  publishes a level for each: COVID-19, Flu A, Flu B, RSV and Norovirus.
  Anything a given plant does not test for is simply left out.
- **Bigger type and bigger targets** (17px base, 48px minimum controls), one
  card per row on a phone.
- **A plain-language explainer** of what the levels and the direction mean,
  and an explicit line that this is not a measure of personal risk.

The level colours are deliberately **not** green-to-red. "High" here means a
lot of something is in the wastewater, which is not the same as a safety
judgement, and the source makes no such judgement. Instead the bottom three
levels read as muted grey and medium upwards escalate through dark blue. Every
one of those is real text, so the values were measured rather than eyeballed:
all clear WCAG 4.5:1 against the surface in both themes. The full gradation is
carried by the meter and the word, so nothing depends on distinguishing two
blues.

**Detailed view** is the original dashboard — chart, comparison modes,
measurement and scale choices, CSV export. Reach it from **Detailed view** in
the header or **See the full charts** at the bottom of the simple view. The
first-visit guide belongs to this view; the simple view is meant to need no
tour at all.

The active view is part of the shared URL (`?view=simple` / `?view=detailed`),
so a link opens the way its sender left it.

## Finding your way around the detailed view

The detailed view is built to be usable without reading this file:

- **First visit to it opens a short guide** — what the site is, four steps, and the
  handful of things that are genuinely non-obvious (where COVID is, when to
  normalize, when to switch to a log scale, what a gap in a line means). It is
  dismissed permanently to `localStorage` and reopens from **How to use this**
  in the header or the link in the footer. Opening a *shared link* skips it:
  that visitor came for someone's specific view, not a tour.
- **"Show me an example"** loads a real working view — one site, four common
  pathogens, log scale — because a worked example explains the controls faster
  than prose does.
- **Pathogens are grouped and searchable** by label, common name, alias, or id.
- **Discontinued series are labelled and demoted** rather than silently
  producing empty charts.
- **A contextual hint offers the log scale** when the visible series' peaks
  span more than 25×, since at that point the smaller ones are a flat line on
  the baseline and look like missing data. It appears only on the linear scale,
  only with two or more series, and switches scale in one click.

## How current the data is

Two different clocks, and the useful one is not the obvious one.

**Each plant is tested every 2–3 days**, not daily: across the 148 active
sites the median gap between tests is 2 days, and 89% of gaps are 1–3 days.
Schedules are staggered, so no single day ever carries every plant.

**Results reach the feed about 2 days after collection, in waves.** Watching
one date across three consecutive daily builds, 2026-09-18 went from 6 to 20
to 66 reporting plants. So the newest sample date represents a handful of
plants whose results happened to land, not the programme.

`build_data.py` therefore also publishes **`meta.complete_through`**: the most
recent date by which most plants had reported. Because schedules are
staggered, coverage is measured over a rolling 3-day window of distinct
reporting sites against the sites active in the last 30 days, and the date is
the newest one clearing 85%. On the build above that gave 2026-09-18, three
days behind the newest sample — the coverage figures step 24% → 48% → 67% →
**90%** → 97% as you walk back, so the answer is not sensitive to the exact
threshold.

The dashboard leads with that date ("Most recent complete data: …") rather
than the newest sample, and says that recent days are still filling in. The
per-card "Last tested" date stays site-specific, because for one plant that
date is exact.

Practical consequence: **checking more than once a day is pointless**, and
since a level is built from several weeks of measurements, weekly is about
right for noticing a real change.

## What the published levels are, and are not

Each simple-view card leads with WastewaterSCAN's own `activity_category`
("not detected" through "very high"). Those are the source's categories; this
project defines none of its own.

They are **not** a reading of the measurement shown beside them, and this was
measured rather than assumed. Across ~1,900 labelled SARS-CoV-2 samples, when
a site's published level changed, the direction agreed with:

| Compared against | Agreement |
|---|---|
| that day's single measurement (raw, normalized, or smoothed) | ~52% — no better than chance |
| a 2–4 week trailing median of the raw value | ~68% |
| a 2–4 week trailing median of the PMMoV-normalized value | **~74%** |

So the level reflects several weeks of dilution-adjusted measurements compared
against the site's own range, not the latest test. The publisher's front-end
config describes the same shape: a tertile method for commonly-detected
targets and a seasonal onset/offset method for the rest.

This matters for presentation. An early version put the latest raw
concentration directly under the level word, and they periodically contradicted
each other — Oceanside WPCP on 2026-09-17 read 274,000 copies/g labelled
*medium*, sitting between neighbouring days of 133,000 and 103,000 labelled
*high*. Nothing was wrong with either figure: the spike was real, and the level
had not moved because several weeks of normalized values had not moved. The
cards now label the figure as the latest test and explain why a single result
can sit above or below the level.

## Trend methodology

Each summary card shows **↑ Rising**, **↓ Falling**, **→ Stable**, or
**— Insufficient data / No detections**. The exact rule, implemented in
`trendFor()`:

1. Take the **last 21 days** of the visible date range (ending at the newest
   sample in the dataset, not at today).
2. Split that window at its midpoint, 10.5 days in: observations before the
   midpoint are the **early** half, the rest are the **late** half.
3. Require **at least 2 observations in each half**. Otherwise report
   **— Insufficient data**. (Most sites sample two or three times a week, so a
   healthy site has 6–9 observations in the window.)
4. If both halves are entirely zero, report **— No detections**. A series with
   no signal is not meaningfully "stable".
5. Otherwise compare the two arithmetic means as a ratio, `late / early`:
   - ratio **≥ 1.5** → **Rising**
   - ratio **≤ 1 / 1.5** (≈ 0.667) → **Falling**
   - anything between → **Stable**
   - early mean exactly zero with a non-zero late mean → **Rising**

Notes on why it is built this way:

- It deliberately does **not** compare the last two measurements. Wastewater
  concentrations are noisy enough that consecutive samples routinely differ by
  more than a factor of two with no underlying change.
- The ±1.5× band is the "tolerance around zero": it is symmetric in ratio
  terms, so a doubling and a halving are treated as equally significant.
- It is computed on the **unsmoothed** observations of the currently selected
  measurement, so the 7-day-average toggle does not change the trend.
- These thresholds are a display convention, not a clinical or public-health
  classification. For the source's own categorical levels, see the
  "WastewaterSCAN level" line on each card.

## Rolling average

The **7-day average** toggle replaces each plotted point with the mean of all
observations in the **trailing 7 calendar days**, that point included.

Sampling is not daily and cadence differs between sites, so "the last 7
observations" would silently span one week at a frequently-sampled site and a
month at an infrequent one. Anchoring the window to the calendar keeps it
meaning the same thing everywhere.

No synthetic daily observations are invented. Every plotted point still sits
on a real sample date; only its value is smoothed. Early points in a series
average over fewer samples, since only the ones that exist are included.

---

## Limitations

- **This is population-level surveillance.** A measurement describes what is
  present in a sewershed's wastewater. It is not a case count, not a
  prevalence estimate, and not an individual's risk of infection.
- **Sampling schedules vary.** Sites report on different days and at different
  frequencies, and some pause. A gap in a line is missing data, not a zero.
- **Non-detects are real zeros,** not missing values — and they cannot be
  drawn on a logarithmic axis, where they appear as gaps. The dashboard says
  so on screen when it happens, rather than nudging them to a small positive
  number and inventing a detection.
- **Concentrations are not freely comparable between sites.** Dilution,
  sewer travel time, and industrial inputs differ. Use *Normalized to PMMoV*
  for cross-site comparison. The dashboard never mixes the two measurements on
  one axis.
- **Methods change over time.** Where WastewaterSCAN replaced an assay, the
  public series continues across the change (see "Assay grouping"). The join
  is the publisher's, but a step change at a chemistry transition is still
  possible.
- **Upstream revises history.** Values for past dates can change as the lab
  re-analyses samples. Each daily rebuild takes upstream's current answer;
  this project keeps no independent archive.
- **The upstream endpoint is undocumented** and may change or disappear. See
  "Data source".
- **Values are rounded to 4 significant digits** to keep the download small.
  The assay does not support more precision than that, and the dashboard
  displays at most 3.
- **This is an independent project.** It is not affiliated with, endorsed by,
  or reviewed by WastewaterSCAN, Stanford University, Emory University, or
  Verily. No evidence of any official affiliation is claimed or implied.

---

## Repository layout

```
.github/workflows/update-data.yml   daily build, validate, promote, commit
docs/index.html                     the entire dashboard (HTML + CSS + JS)
docs/data.json                      generated dataset, served to the browser
docs/source-info.json               provenance: source, licence, retrieval time
docs/og-image.png                   1200x630 card shown when the link is shared
docs/favicon-32.png                 browser tab icon
docs/favicon-180.png                iOS home-screen icon
docs/tip10-logo.png                 footer credit badge
scripts/build_data.py               the ingestion pipeline
scripts/make_images.py              regenerates the icons and preview card
tests/validate_data.py              dataset checks; also the CI safety gate
```

`scripts/make_images.py` is only run when the branding changes. It needs
Pillow, which is a development-only dependency — neither the daily build nor
the site requires it.

If you fork this, change the absolute `og:*` URLs in `docs/index.html` to your
own Pages address. Link previews are fetched server-side, so a relative path
has nothing to resolve against.

`docs/data.json` is committed on purpose: GitHub Pages serves it directly, and
committing it is what makes the dashboard work with no backend.

---

## Licence

Code in this repository is MIT licensed — see [LICENSE](LICENSE).

`docs/tip10-logo.png` is excluded: it is a trademark of Tip10 Technologies,
included to identify who built the visualization, and is not covered by the
MIT grant.

That covers the code only. The wastewater measurements are published by
WastewaterSCAN under their own terms (CC BY-NC 4.0, attribution as quoted
above) and this project's licence does not change them.
