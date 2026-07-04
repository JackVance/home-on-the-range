# Austin-Metro School-Zone Overlay Tool — Design Spec

## Problem Statement

Build a **local, single-user** tool that, for a set of target Austin-metro high
school attendance zones (initially Eanes/Westlake plus two more, extensible to
15+), overlays three layers:

1. **Attendance-zone boundary polygons** (from district ArcGIS FeatureServers).
2. **Census (ACS 5-year) demographics** apportioned to each zone (race/ethnicity,
   income, educational attainment).
3. **Active-listing / market context** — without ever touching Zillow's or any
   site's anti-bot systems.

The scarce resource is **trust**, not automation. The tool must remain
believable eight months from now when an ArcGIS endpoint has moved or a district
has rezoned. The design therefore optimizes for: (a) provenance and legible
change detection, (b) correctness on the *heterogeneous* zones the tool exists to
compare, and (c) honest disclosure of uncertainty over false precision.

### Guiding principles (adopted throughout)

- **Snapshot-and-freeze, never query-on-demand.** The viewer reads committed
  artifacts on disk; it never hits an external service at view time. Fetching is
  a deliberate, logged act.
- **Automate fetching from structured APIs; never automate interpretation.**
- **Git *is* the version/diff/provenance layer.** Do not hand-roll timestamped
  filenames, pointer files, or geometry-hash sidecars. A change is an uncommitted
  diff; promotion is a commit; "verified by human" is "committed." (No symlinks
  anywhere — this repo runs on **win32**, where a `current ->` symlink is a
  footgun that breaks git, editors, and `os.path`. The working-tree file *is*
  current by definition.)
- **Commit the small trustworthy artifacts; gitignore the bulky reproducible
  inputs.**
- **Correct *bias* in code; *disclose* variance and residual error in the
  viewer.** Bias is directional and correlates with the comparison — fix it down
  to the resolution the free data supports. Variance (MOE) and un-cheaply-fixable
  geometric error get shown, not hidden.
- **Degrade loudly and on the record, never silently.**
- **Spike the load-bearing empirical assumption before building on it.**

---

## Architecture

A thin build step over static, provenance-stamped data. Four production scripts +
one viewer + one fixture test, plus a throwaway spike that gates the boundary
layer. There is deliberately **no `build_site.py`**: the data is already JSON, so
the viewer reads it live and there is nothing to template or keep in sync.

```
austin-zones/
  spike/
    probe.sh                       # STEP 0, throwaway: GO/NO-GO on anonymous geometry fetch
  src/
    fetch_census.py                # counts+MOE tables + TIGER + PL-94; reproject to 4326
    fetch_boundaries.py            # ArcGIS query + normalize_zone + gates
    join_zones.py                  # apportionment + derived stats + MOE  (THE trust artifact)
    refresh.py                     # thin orchestration wrapper; ends in `git diff --stat`
  tests/
    test_join.py
    fixtures/
      toy_blocks.geojson
      toy_zone.geojson
      toy_acs.json
  data/
    census/                        # gitignored — bulky, reproducible
      acs5-2023-b03002.json
      acs5-2023-b19001.json
      acs5-2023-b15003.json
      tiger-bg-48453.geojson       # reprojected to 4326 on ingest
      pl94-blocks-48453.geojson    # P1/P3/H1 per block, 4326
    boundaries/                    # committed — the trust artifacts
      eanes-westlake.geojson
      eanes-westlake.meta.json
      _snapshots/                  # created ONLY by refresh --archive-before
        eanes-westlake@2024-25.geojson
    joined/                        # committed — THE trust artifact
      eanes-westlake.demographics.json
  zones.toml                       # the registry / extensibility seam
  viewer/
    index.html                     # Leaflet; fetch()es committed JSON; ZERO math
```

### CRS invariant (asserted, not assumed)

**Everything is WGS84 (EPSG:4326).** The fetch pins `outSR=4326`; the census
ingest reprojects TIGER/PL-94 (native NAD83 / EPSG:4269) to 4326. Point-in-polygon
runs in this single declared space. **Area is computed geodesically** on the
ellipsoid via `pyproj.Geod` — no projected-CRS choice to configure or rot. Any
geometric value (area gate, `.within()` join) is invalid unless both operands are
4326; this is enforced, not trusted. This is the `normalize_geojson` of the
spatial layer: an implicit CRS silently corrupts every gate and join built on top
of it.

### Dependencies

Python 3.11+ stdlib, plus `shapely` (`unary_union`, `make_valid`) and `pyproj`
(rides along with shapely; geodesic area, datum reprojection). No web framework —
the viewer is served with `python -m http.server`.

---

## Components

### 0. `spike/probe.sh` — GO/NO-GO gate (throwaway, blocking)

Tests the **verb the whole fetch architecture depends on**: does an *anonymous*
request return real polygon *geometry* (not merely metadata/count)? Run for one
district (Eanes) before any production boundary code exists.

```bash
# Deleted after use. No project code depends on it.
curl -s "https://services.arcgis.com/.../FeatureServer/0/query?\
where=1=1&outFields=*&resultRecordCount=1&outSR=4326&f=geojson"
```

Interpretation:

| Response | Verdict |
|---|---|
| `200` + a `Polygon`/`MultiPolygon` with coordinates | **GO** — fetch architecture is real |
| `200` + `{"features":[]}` or an `"error"` object | **NO-GO** — gated/empty; pivot boundary layer to manual QGIS tracing |
| `403` / token error | **NO-GO** — gated; pivot to manual tracing |

`returnCountOnly=true` is explicitly **not** the GO/NO-GO probe (a count can
succeed while geometry is withheld, capped, or generalized). Count-only is
reserved for the annual liveness sweep (`refresh.py --check`).

---

### 1. `zones.toml` — the registry / extensibility seam

Adding a zone is config, not code. Values authored **once, at the one-time
eyeball**, encode the operator's acknowledgment of ground truth.

```toml
[eanes-westlake]
district            = "Eanes ISD"
high_school         = "Westlake HS"
counties            = ["48453"]                       # drives TIGER/ACS/PL-94 pulls
boundary_source     = "arcgis_featureserver"
boundary_url        = "https://services.arcgis.com/XXXX/FeatureServer/0"
boundary_where      = "SchoolName='Westlake High School'"   # pin ONE school, not SchoolType='High'
expected_features   = 1        # structural gate; bump to 3 if district republishes as 3 pieces
expected_area_km2   = 40       # order-of-magnitude gate (geodesic km²)
boundary_has_name   = true     # false = service lacks a usable name field (recorded limitation)
listings_links      = [
  "https://www.redfin.com/school/.../Westlake-High-School",
]
# v2 only:
# redfin_region     = "zip/78746"   # honestly labeled as APPROXIMATE, never a zone stat
```

**Module constants (in code, not config — change in review, not per-zone):**
`CV_FALLBACK = 0.30`, `MIN_BLOCKS = 30`, `AREA_TOLERANCE = 3.0` (×), coordinate
rounding `ndigits = 6` (~11 cm). These are cached judgment calls; keeping them in
code (not `zones.toml`) means they can't rot silently as per-zone state.

---

### 2. `src/fetch_census.py` — demographics inputs (fully automated)

Census is the settled, versioned, government API — automate it end to end. Pulls
**count tables + their MOE columns** (the `_M` variant of each `_E` variable, free
in the same request), plus the geographies needed for population-correct
apportionment.

**Tables (counts only — never pull medians directly):**

| Table | Universe | ACS content |
|---|---|---|
| `B03002` | persons | race/ethnicity counts |
| `B19001` | **households** | household counts by income bracket |
| `B15003` | persons 25+ | educational-attainment counts |

`B19013` (median household income) is **deliberately excluded** — averaging
medians is statistically illegal, so the median is derived from the apportioned
`B19001` bracket counts in `join_zones.py`.

**Geographies:**

- TIGER block groups for each county in `counties` (e.g. `48453` Travis,
  `48491` Williamson).
- **PL-94-171 2020 blocks** with `P1` (total population), `P3` (18+ voting-age
  population), and `H1` (occupied housing units) per block — all three ship in the
  same file; marginal cost ≈ zero.

**Responsibilities:**

- **Reproject** TIGER + PL-94 from NAD83 (4269) to WGS84 (4326) on ingest,
  explicitly.
- **Assert vintage nesting:** every block's `GEOID[:12]` must exist in the
  block-group set (blocks nest in block groups nest in ACS geographies). Hard-fail
  otherwise — a mismatched vintage (e.g. TIGER 2023 blocks against ACS on 2020
  geographies) silently corrupts apportionment weights with no error.

Signature sketch:

```python
def fetch_census(counties: list[str], vintage: str = "acs5-2023") -> None:
    """Pull B03002/B19001/B15003 counts+MOE + TIGER bg + PL-94 P1/P3/H1.
       Reproject geometry to EPSG:4326. Assert GEOID[:12] nesting.
       Writes data/census/*  (gitignored, reproducible)."""
```

Output → `data/census/` (**gitignored** — bulky, fully reproducible).

---

### 3. `src/fetch_boundaries.py` — attendance-zone polygons (fetch automated, promotion gated)

Endpoint discovery (finding the FeatureServer behind each district's Instant App)
is a **one-time manual step** recorded in `zones.toml`; never auto-discovered
(auto-discovery is the fragile over-automation the briefing warns against).

**Query:** pins `outSR=4326`, `f=geojson`, the zone's `boundary_where`.

**Failure classification** (remediation differs per class — a named error beats a
stack trace reverse-engineered next February):

```python
class EndpointMoved(Exception): ...   # 404/400/error object -> district republished; re-run discovery
class EmptyResult(Exception): ...     # 200 + 0 features      -> field/value renamed on republish
class ReviewRequired(Exception): ...  # gate tripped          -> file written, commit blocked

def fetch_boundary(zone_cfg) -> list[dict]:
    resp = query_featureserver(zone_cfg.boundary_url, zone_cfg.boundary_where, out_sr=4326)
    if resp.status in (404, 400) or "error" in resp.json():
        raise EndpointMoved(f"{zone_cfg.name}: FeatureServer unreachable. Re-run endpoint discovery.")
    features = resp.json().get("features", [])
    if not features:
        raise EmptyResult(f"{zone_cfg.name}: 0 features for where={zone_cfg.boundary_where!r}.")
    return features
```

**`normalize_zone` — canonicalization that makes git-as-diff-layer honest.**
FeatureServer output is diff-hostile: permuted feature order across queries,
jittering coordinate tails (13–15 sig digits that shift between responses), and
churning bookkeeping attributes. Without normalization, `git diff` screams
"everything changed" on an unchanged re-fetch (cry-wolf) and is unreadable on a
real change. Normalization makes an unchanged re-fetch **byte-identical** (clean
`git status`) and a real rezoning a **small, readable diff**.

```python
def normalize_zone(features: list[dict], zone_cfg) -> dict:
    """N raw features for ONE school -> one canonical MultiPolygon FC. Idempotent.
       Postcondition: output geometry is valid AND byte-stable."""
    # Structural gate (schema-independent, PRIMARY):
    if len(features) != zone_cfg.expected_features:
        raise ReviewRequired(
            f"{zone_cfg.name}: {len(features)} features, expected {zone_cfg.expected_features}")
    # Semantic backstop (used only if service provides a name field):
    if zone_cfg.boundary_has_name:
        names = {normalize_name(f["properties"].get("school_name")) for f in features}
        if len(names) != 1 or None in names:
            raise ReviewRequired(f"{zone_cfg.name}: mixed/unlabeled schools: {names}")
    geom = unary_union([shape(f["geometry"]) for f in features])   # dissolve discontiguous pieces
    geom = round_coords(geom, ndigits=6)                           # ~11cm; kills tail jitter
    geom = make_valid(geom)                                        # rounding is topology-hostile
    assert geom.is_valid, "normalization produced invalid geometry"
    geom = canonicalize_rings(geom)   # RFC7946 winding + intra-geom polygon sort by (min-x, min-y)
    props = whitelist_props(features)  # KEEP {school_name, district, effective_year}; DROP OBJECTID,
                                       #   Shape_Length/Area, last_edited_date, GlobalID
    return {"type": "FeatureCollection", "features": [feature(geom, props)]}
    # json.dump(..., indent=1, sort_keys=True) -> readable, line-oriented
```

**Why `expected_features` is the PRIMARY gate and name is the BACKSTOP:** count is
returned by every service regardless of schema; name is not — leaning the primary
gate on an attribute the third-party service isn't obligated to provide is the
fragility we're knifing. `expected_features` catches the loose-`where`
(5 schools ≠ 1) *and* legit discontiguity (bump config to 3 = the human confirming
annexation is real, identical acknowledgment semantics to `expected_area_km2`).
The name field catches only the **wrong-layer-same-count** case ("Westlake
**Middle**," also one feature). If the service has no usable name field,
`boundary_has_name = false` records it as a *known limitation*, never a silent
bypass — and `fetch_boundaries.py` asserts name-field presence at authoring time
when `boundary_has_name = true`.

**The `unary_union` fang:** dissolve silently fuses whatever it's handed and cannot
distinguish "three discontiguous pieces of Westlake" from "Westlake plus a
neighbor the `where` caught." The area gate won't catch two adjacent same-size
zones (≈2×, under the 3× net). The `expected_features` + name assertions **before**
the union are what make dissolve safe; ungated, it's the one operation that
launders a config error into a plausible wrong answer.

**Intra-geometry sort:** byte-stability of a dissolved MultiPolygon requires
ordering its constituent polygons deterministically. Sort by the `(min-x, min-y)`
of each exterior ring — a pure geometric serialization tiebreak scoped *inside one
feature* (not the circular/OBJECTID-unstable cross-feature sort, which is a
landmine and is not used).

**Geodesic area gate** — catches the wrong-layer / whole-district / empty-result
class that survives the count gate, and does so in *real* km² (a Web-Mercator area
at Austin's latitude is inflated ~1.33×, hiding inside a naive gate):

```python
from pyproj import Geod
_GEOD = Geod(ellps="WGS84")

def area_km2(geom_wgs84) -> float:
    """Ellipsoidal area from lon/lat. No projected-CRS choice to get wrong."""
    a, _ = _GEOD.geometry_area_perimeter(geom_wgs84)
    return abs(a) / 1e6
```

The gate is **always-write / taint-run / gate-commit**, never fetch-abort — when
the check trips, the file you most need to inspect is the one the fetch just
produced:

- `fetch_boundaries.py` **always writes** the normalized file (the evidence).
- If `area_km2` is off by > `AREA_TOLERANCE` (3×) from `expected_area_km2`: print
  loudly, write a `.REVIEW` marker beside the file, make `refresh.py` exit
  non-zero. The commit is blocked until the operator either fixes `boundary_where`
  or bumps `expected_area_km2` (the config edit *is* the acknowledgment).
- In a multi-zone run, **one zone tripping must not abort the others** — collect
  failures, report at end, exit non-zero. (Leander's legit rezoning must not block
  the Round Rock refresh.)

Area is a **smoke alarm, not a smoke detector**: it catches gross failures (≈0
empty, 10–20× whole-district) but not a subtly-wrong 1.4× neighbor polygon. The
one-time `verified_against` eyeball remains the real baseline guard.

**`meta.json`** — only what git genuinely cannot know:

```json
{
  "source_url": "https://services.arcgis.com/.../FeatureServer/0",
  "source_type": "arcgis_featureserver",
  "fetched_at": "2026-07-03T09:00:00Z",
  "district_effective_year": "2025-26",
  "known_rezoning": "Barton Creek/Valley View elem, approved Feb 2025",
  "verified_against": "Eanes official boundary PDF 2025-26 + zipdatamaps HS map, eyeballed 2026-07-03",
  "verified_by_human": true
}
```

`verified_against` closes the baseline-trust gap git cannot: git only confirms
"unchanged since last fetch," never "the first fetch was right." You could grab an
old draft layer (the briefing warns Leander has a stale webappviewer alongside the
current Instant App) and every refresh faithfully confirms the wrong baseline is
unchanged. The one-time human match against the district's authoritative map
(official PDF; zipdatamaps as a **manual cross-check, never scraped**) gives the
first commit real meaning: it asserts a human matched the polygon to an
authoritative rendering. Subsequent commits need only "I looked at the git diff."

Output → `data/boundaries/*.geojson` + `*.meta.json` (**committed** — trust
artifacts).

---

### 4. `src/join_zones.py` — apportionment + derived statistics (THE trust artifact)

This is the densest fiddly numeric code in the project and the one output a human
**cannot eyeball**. It owns all statistics; the viewer does none. The committed
`demographics.json` is the number a homebuyer reads — it must live in git so it
can be diffed, reviewed, and caught when it silently shifts. A render-time number
has no representation in git and is disallowed.

**Block-population apportionment** (not centroid-at-block-group, not areal).
School zones cut through large heterogeneous block groups; a block-group centroid
can fall outside the zone while most of its population is inside — quietly wrong
for exactly the zones being compared. Areal weighting is *also* wrong here —
Westlake contains Lake Austin and low-density canyon land, so area-weighting
mis-attributes acreage as people. The correct, cheap move: run the simple
geometric test on a **small enough unit that it stops lying** — 2020 PL-94 blocks
(~a city block; centroid-in-zone is accurate at that resolution) — and weight ACS
counts by block population.

**Per-table weight universe** (the weight must match the ACS table's universe —
person-weighting a household universe is bias *correlated with the comparison*,
because household size differs across Westlake vs. Vandegrift, and that bias does
not average out):

```python
UNIVERSE = {
    "B03002": "P1_total_pop",     # persons     -> weight by total population
    "B15003": "P3_pop_18plus",    # persons 25+ -> weight by 18+ (closest free PL-94 proxy)
    "B19001": "H1_occupied_units",# households  -> weight by occupied housing units
}

def apportion(zone_geom, blocks, acs_bg_counts, table) -> dict:
    """Weight ACS block-group COUNTS into the zone by the table's universe.
       blocks: PL-94 blocks (GEOID, P1, P3, H1, polygon), all EPSG:4326.
       bg GEOID = block GEOID[:12]."""
    wfield  = UNIVERSE[table]
    in_zone = [b for b in blocks if b.centroid.within(zone_geom)]
    zone_w  = sum_by_bg(in_zone, wfield)
    total_w = sum_by_bg(blocks,  wfield)
    out = {}
    for var, counts in acs_bg_counts.items():
        for bg, n in counts.items():
            w = zone_w.get(bg, 0) / total_w[bg]
            out[var] = out.get(var, 0) + w * n
    return out
```

**Stopping rule for universe correction (bias vs. variance):** eliminate *bias*
down to the resolution free PL-94 data supports, then disclose the residual and
quit. P3 (18+) is the terminus for B15003 even though its universe is 25+ — the
18–24 residual is small in these residential suburbs and not obviously correlated
with the wealth axis being compared (it's variance, not directional bias). Chasing
exact-25+ would require ACS itself, which is circular (you'd need the answer to
weight the answer). Bias gets corrected in code; variance gets shown via MOE.

**Derive medians *after* apportionment, never apportion a median:**

- Income: apportion the **B19001 bracket counts**, then compute the median by
  linear interpolation within the containing bracket. **Open-top guard:** if the
  median falls in the open top bracket (`$200k+`, where much of Westlake's mass
  sits), do **not** interpolate a false-precision number — Pareto/arithmetic
  interpolation on an open bracket holding a plurality can shoot the estimate past
  anything defensible. Report `">$200,000"` and flag it. Honest beats precise.
- Race/ethnicity, education: compute shares/rates from apportioned counts.

**MOE (the precision wrongness class):** ACS block-group estimates carry margins
of error that, for small or split zones, can be enormous (40–90% CV). Pull the
`_M` columns, combine in quadrature when apportioning:

```
MOE_zone = sqrt( Σ (wᵢ · MOEᵢ)² )     # standard Census practice; ignores weight variance
```

Commit `value`, `moe`, `cv` per estimate.

**Automatic per-variable resolution fallback** (replaces a per-zone config knob,
which would be cached judgment that rots and can't express a per-variable
problem). Compute at block group first; if combined CV exceeds `CV_FALLBACK`
(0.30), **automatically recompute that variable at tract** (fewer, larger units,
tighter MOEs, at the cost of exact geography) and stamp the outcome:

```json
{
  "median_household_income": {
    "value": null, "display": ">$200,000",
    "moe": 14200, "cv": 0.065,
    "resolution": "tract", "reason": "block_group CV 0.34 > 0.30"
  },
  "pct_hispanic": {
    "value": 0.11, "moe": 0.03, "cv": 0.09,
    "resolution": "block_group", "reason": null
  },
  "block_count": 78
}
```

`resolution` + `reason` are **committed, diffed fields** — they are the
load-bearing disambiguator that stops the next vintage's fallback from reading as
a real value change. `218000 → 231000` alongside `resolution: block_group → tract`
tells the reader to distrust the delta (geography moved, not the neighborhood).
Legibility over stability: when CV wiggles flip a variable across vintages,
`reason` records the cause; do not engineer hysteresis. The CV threshold itself is
the module constant `CV_FALLBACK`, changed in review, never per-zone config.

**`block_count` disclosure** (the knowingly-accepted straddling-block error).
Centroid-in-block error is <1% for the ~40 km²/many-block target zones but could
be a ~5% swing for a hypothetical compact 20-block urban zone. We **disclose, not
fix**: commit `block_count`; the viewer flags zones under `MIN_BLOCKS` (30) as
"coarse geography." The better diagnostic (fraction of pop in straddling blocks)
is *not* built because computing it requires the areal-within-block estimator we
refused on Lake-Austin grounds — `block_count` is the most honest signal computable
without reopening that door. And at the block counts where straddle error bites,
MOE is already the binding constraint and already fallback-and-greyed.

Output → `data/joined/*.demographics.json` (**committed** — the trust artifact).

---

### 5. `tests/test_join.py` — the one required test

The un-eyeballable math is the one thing that must have a fixture pinning a
known-correct answer. Four hand-computed blocks; every future refactor either
matches or is a bug.

```
tests/fixtures/
  toy_blocks.geojson   # 4 blocks, known P1/P3/H1; ≥2 straddle the zone edge
  toy_zone.geojson     # a square
  toy_acs.json         # B03002/B19001/B15003 counts with hand-worked expected output
```

Required cases:

1. **A block straddling the zone edge** (centroid out but most pop in, and its
   mirror) — pins the geometry test and documents the knowingly-accepted error.
2. **A zone whose median lands in the open top bracket** — pins the `">$200,000"`
   guard *firing* (Westlake's actual branch, most likely to rot in a refactor; a
   fixture exercising only the interpolation path leaves the guard untested where
   it's load-bearing).
3. **The bg→tract fallback trigger** — pins that a high-CV variable recomputes and
   stamps `resolution`/`reason`.

Also assert the MOE-in-quadrature combination against the hand-worked value.

Test line: `join_zones.py` gets a fixture; the fetchers do not (they hit live
third-party endpoints; mocks rot). `normalize_zone`'s self-test is **validity AND
idempotence** — `normalize(x).is_valid` *and* `normalize(normalize(x)) ==
normalize(x)` byte-for-byte. Idempotence alone is insufficient: a stably-invalid
geometry is idempotent and wrong, and the round-then-diff trust model is worthless
if the polygon it diffs is topologically broken.

---

### 6. `src/refresh.py` — thin orchestration wrapper

No versioning, hashing, or diff computation of its own — git is the diff layer.

```
refresh.py [--check] [--archive-before]
```

- **default:** run fetches + join for all zones, collect per-zone failures, end in
  `git diff --stat`. Exit non-zero if any zone tainted (area/count/name gate,
  `EndpointMoved`, `EmptyResult`).
- **`--check`:** annual liveness sweep. Hit every zone with a
  `returnCountOnly=true` query (cheap, no geometry), classify each as
  reachable / moved / empty, print a table. Finds dead endpoints without pulling
  megabytes. Run once ~Feb, before the real refresh.
- **`--archive-before`:** copy each `boundaries/*.geojson` →
  `_snapshots/…@<year>.geojson` before overwriting — **opt-in, used only across a
  known rezoning**. The write is nearly free and the pre-rezoning data is
  unrecoverable if not captured. Default refresh archives nothing. The browser A/B
  *read* UI is deferred to v2; in v1, `git diff` (thanks to normalization) or QGIS
  answers "what moved."

---

### 7. `viewer/index.html` — static Leaflet viewer (ZERO statistics)

No build step. A single page `fetch()`es the committed JSON directly; there is no
templating (the data is already JSON), which removes a whole class of
"site stale relative to data" bugs. Served, because `file://` blocks `fetch()`:

```
python -m http.server 8000 --directory .
# open http://localhost:8000/viewer/
```

Reads a small `zones.json` (machine dump of `zones.toml`, or hand-maintained) to
know which zones exist, then loads each `boundaries/*.geojson`,
`boundaries/*.meta.json`, and `joined/*.demographics.json`. Renders:

- boundary polygon(s) per zone;
- committed demographic numbers **as-is** — no math, ever;
- freshness/trust chrome from committed fields: `fetched_at`, `verified_by_human`,
  per-estimate `value ±moe`, grey-out where `cv > CV_FALLBACK`, `resolution`
  annotation, "coarse geography" flag where `block_count < MIN_BLOCKS`, open-top
  `">$200,000"` display;
- **listings as link-out only** (`listings_links` → new browser tab into Redfin/
  Zillow's normal UI). No scraping, no anti-bot surface.

---

## Data Flow

```
STEP 0 (blocking, throwaway):
  spike/probe.sh  ──►  anonymous geometry fetch?  ──► GO / NO-GO on the boundary layer

PARALLEL — Census track (independent of STEP 0; ships first):
  Census API + TIGER + PL-94
        │  fetch_census.py  (reproject→4326; assert GEOID[:12] nesting)
        ▼
  data/census/*  (gitignored)
        │
        │  ┌── if STEP 0 == GO ──────────────────────────────┐
        │  │  ArcGIS FeatureServer (outSR=4326)              │
        │  │        │ fetch_boundaries.py                     │
        │  │        │   classify EndpointMoved/EmptyResult    │
        │  │        │   normalize_zone (expected_features gate,│
        │  │        │     name backstop, dissolve, make_valid, │
        │  │        │     round6, whitelist props)             │
        │  │        │   geodesic area gate (always-write/      │
        │  │        │     taint-run/gate-commit)               │
        │  │        ▼                                          │
        │  │  data/boundaries/*.geojson + *.meta.json (committed)
        │  └──── if NO-GO: hand-trace polygon in QGIS ────────┘
        │                          │
        └──────────┬───────────────┘
                   ▼
        join_zones.py
          block-pop apportionment (P1/P3/H1 per-table universe, EPSG:4326 .within)
          median-from-brackets + open-top guard
          MOE in quadrature
          auto bg↔tract fallback (commit resolution + reason)
          block_count
                   ▼
        data/joined/*.demographics.json  (committed — THE trust artifact)
                   ▼
        viewer/index.html  (fetch()es committed JSON + meta; renders trust chrome; ZERO math)

  refresh.py orchestrates fetch+join, ends in `git diff --stat`
    --check          annual liveness sweep (returnCountOnly)
    --archive-before snapshot before overwrite, across a known rezoning
```

**Trust line (which artifacts live where, and why):**

| Artifact | Location | Committed? | Rationale |
|---|---|---|---|
| Raw ACS/TIGER/PL-94 | `data/census/` | **No** (gitignored) | Bulky, fully reproducible from `fetch_census.py` |
| Normalized boundary GeoJSON | `data/boundaries/` | **Yes** | Small, trustworthy; git = its diff/history/gate |
| `meta.json` | `data/boundaries/` | **Yes** | Provenance git can't infer (source, verify, rezoning) |
| Demographics-with-MOE | `data/joined/` | **Yes** | The auditable output a homebuyer reads |
| Snapshots | `data/boundaries/_snapshots/` | **Yes**, on demand | Deliberate pre-rezoning exhibit |

---

## Build Order

0. **`spike/probe.sh`** — one `curl`, one afternoon. GO/NO-GO on the fetch
   architecture. Blocking for the boundary layer only.
1. **`fetch_census.py` + `join_zones.py` + `test_join.py`** for Eanes/Westlake —
   the hardest *code* (apportionment math) and its fixture. **Independent of
   step 0**: if boundaries turn out gated, hand-trace one polygon in QGIS and feed
   it in. Lowest risk, ships first — the real payoff of the "Census is solved,
   boundaries are the risk" split.
2. *(Only if step 0 = GO)* **`fetch_boundaries.py`** with `normalize_zone`, the
   `expected_features` / `expected_area_km2` / name gates, and the
   `EndpointMoved` / `EmptyResult` taxonomy.
3. **`refresh.py`** (`--check`, `--archive-before`) → **`viewer/index.html`**
   reading committed JSON, zero math.
4. **v2:** `fetch_cad.py` recent-sales-in-polygon; honestly region-labeled Redfin
   trend strip; browser A/B snapshot toggle.

**Explicitly out of v1:** CAD ingestion, zipdatamaps scraping (manual cross-check
only, ever), Redfin trend strip, browser snapshot A/B UI, areal-within-block
apportionment.

---

## Listings & Market Context (the no-clean-source gap)

- **v1 — link-out only.** Per-zone deep links into Redfin/Zillow's normal UI. No
  scraping, no anti-bot surface, zero maintenance. CAD is *not* a listings
  substitute and must never be labeled as active listings ("no for-sale status").
- **v2 — CAD-in-polygon is the honest market layer, not a consolation prize.**
  TCAD/WCAD parcels ship with geometry + sale date/price, are legally bulk-
  downloadable, and reuse `join_zones.py`'s point/parcel-in-polygon machinery to
  give **actual recent arms-length sales inside the real zone polygon** —
  geographically exact, unlike any zip approximation. A second fetcher
  (`fetch_cad.py`), two county schemas; deferred, not abandoned. It is the *only*
  market layer that inherits the geographic rigor of the boundary + join pipeline.
- **Redfin Data Center** is published by zip / neighborhood / metro — **never by
  attendance zone**. Shipping it as a "zone strip" would reintroduce the exact
  geography lie the demographics layer spent its design purging. If shown at all
  (v2), it must be explicitly labeled `Redfin region: 78746 (approximate — not the
  attendance-zone boundary)`, mapped in `zones.toml`, and shown as context, never
  as a zone statistic.

---

## Open Questions

1. **Geodesic area vs. a single projected CRS.** The spec pins geodesic area
   (`pyproj.Geod`) specifically to eliminate per-zone projection config before it
   can rot. A defensible alternative is one stated projected CRS (EPSG:2277 Texas
   Central) for *all* geometry, so the `.within()` join and area share one planar
   space with no geodesic-vs-planar mental overhead. Correctness is equivalent at
   this scale — the tiebreak is minimum config surface. **Recommendation:**
   geodesic, as specified, unless the implementer finds the planar model
   materially simpler to reason about across the codebase.

2. **Commit the reprojected TIGER/PL-94, or keep it gitignored?** Reprojection is
   now a *transform we apply*, not a raw download — a future `pyproj`/PROJ version
   shift could move committed demographics without the upstream source moving.
   This is the one place "reproducible inputs stay gitignored" brushes against
   "the trust artifact must be diffable." **Recommendation:** keep gitignored and
   trust pyproj's versioned, stable datum transforms — but if reproducibility
   proves shaky in practice, commit a small intermediate (reprojected block
   centroids + P1/P3/H1) rather than the full geometry. Decide on purpose.

3. **Nameless FeatureServers.** When `boundary_has_name = false`, a zone's only
   guards are `expected_features` + geodesic area + the one-time `verified_against`
   eyeball — the wrong-layer-same-count case ("Westlake **Middle**") is uncaught
   by machine. Is that acceptable as a recorded per-zone limitation, or should a
   nameless service be disqualified from automated fetch and routed to manual
   tracing? **Leaning:** acceptable-with-record; revisit if a real target zone
   hits it.

4. **Urban zone extension.** The straddling-block error is disclosed via
   `block_count`, not fixed, on YAGNI grounds (the briefing names only suburban
   zones). If the operator later adds a compact urban HS (e.g. Austin High), that
   is the concrete moment to revisit areal-within-block refinement — with a real
   zone to test against, not a hypothetical.

5. **Empirical confirmation still pending.** Neither the FeatureServer
   public-geometry assumption (step 0) nor the single-vs-multi-feature shape of the
   three target zones has been verified against a live endpoint. The design builds
   for the multi-feature reality (dissolve gated by `expected_features`) precisely
   because that ground truth is unconfirmed. The spike resolves the first; the
   first real fetch resolves the second.