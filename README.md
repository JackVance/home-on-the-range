# Home On The Range

Where should the herd settle? Every Austin-metro school attendance zone
(29 districts; 400+ zones across the district, high, middle, and elementary
levels) on one interactive map, with TEA ratings, Census demographics,
appraised home values, and recent deed transfers per zone.

**Live map:** https://bigolbuffalo.com/HomeOnTheRange.html
(the [bigolbuffalo](https://github.com/JackVance/bigolbuffalo) repo hosts the
site wrapper; this repo is the whole tool — pipeline, data, and viewer).

Formerly "home-finder". The tool overlays, per attendance zone (all
registered in `zones.toml`):

1. **Attendance-zone boundary polygons** (district ArcGIS services, gated +
   provenance-stamped)
2. **Census ACS demographics** apportioned to each zone (race/ethnicity,
   income, education — with honest MOE/uncertainty disclosure)
3. **Listings context** (per-zone link-outs; no scraping, ever)

Design: [`docs/SPEC.md`](docs/SPEC.md) — produced by a cross-model design
debate (see also [`docs/BRIEFING.md`](docs/BRIEFING.md)). Step-0 spike results:
[`spike/FINDINGS.md`](spike/FINDINGS.md).

## Principles (from the spec)

- **Snapshot-and-freeze**: the viewer only reads committed artifacts; fetching
  is a deliberate, logged act. **Git is the version/diff/trust layer.**
- **Automate fetching, never interpretation.** Boundary changes are surfaced
  for human review (gates + `git diff`), never auto-promoted.
- **Correct bias in code; disclose variance in the viewer.** All statistics
  live in tested Python (`src/join_zones.py`); the viewer does zero math.
- **Everything is EPSG:4326; area is geodesic.**

## Layout

```
zones.toml            zone registry (adding a zone = config, not code)
src/fetch_census.py   ACS counts+MOE + PL-94 block weights + block points
src/join_zones.py     apportionment + derived stats  (THE trust artifact)
tests/test_join.py    hand-computed fixture pinning the math (7 cases)
data/census/          gitignored — reproducible inputs
data/boundaries/      committed — normalized boundary polygons + meta.json
data/joined/          committed — demographics.json per zone
```

## Usage

```bash
python tests/test_join.py            # verify the math against the fixture
export CENSUS_API_KEY=...            # free: https://api.census.gov/data/key_signup.html
python src/fetch_census.py           # pull ACS + PL-94 for zones.toml counties
python src/join_zones.py             # apportion into each committed boundary
```

Boundary fetching (`src/fetch_boundaries.py`) and the viewer are build-order
steps 2–3 — see `docs/SPEC.md` §Build Order.

## Status

- [x] Step 0 spike — Leander **GO**, Round Rock **GO**, Eanes **NO-GO** (manual trace)
- [x] Step 1 — census fetcher, apportionment join, fixture test (7/7 passing)
- [ ] Step 2 — `fetch_boundaries.py` (normalize_zone + gates + failure taxonomy)
- [ ] Step 3 — `refresh.py`, `viewer/index.html`
- [ ] v2 — CAD sales-in-polygon, labeled Redfin strip, snapshot A/B
