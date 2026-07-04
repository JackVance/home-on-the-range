# Briefing: Austin-Metro School Zone / Housing / Demographic Data Tool

## Design question / tensions to resolve

This is a build-scoping design debate, not a research task — most of the
data-source research below is already settled (dead ends confirmed, leads
identified). The friction the debate should resolve is **how to architect a
local tool** around this data reality:

1. **Automation depth vs. maintainability** — how much of the pipeline should be
   automated (ArcGIS FeatureServer queries, spatial joins, scraping
   zipdatamaps) vs. accepting cheap manual steps (hand-tracing boundaries,
   linking out to Zillow/Redfin)? Over-automating fragile third-party endpoints
   may cost more than it saves.
2. **Boundary trust under active rezoning** — all three districts are actively
   rezoning. How does the tool stay trustworthy (freshness disclaimers,
   re-scrape cadence, provenance/versioning of each boundary) without becoming a
   maintenance burden?
3. **The listings gap** — active for-sale listings have no clean, legal, free
   bulk source. Resolve how the tool handles this (manual link-out per zone vs.
   CAD/Redfin trend proxies vs. something else) without ever touching Zillow's
   anti-bot systems.

Secondary: overall architecture, storage, the ACS↔zone spatial join approach,
and extensibility from the 3 target zones to more.

## Objective

Build a local tool that overlays, for a set of target high school attendance
zones (initially: Westlake/Eanes ISD, Vandegrift/Leander ISD, Westwood/Round
Rock ISD — extensible to others):

1. Accurate attendance-zone **boundary polygons** (not point markers/radii)
2. **Active home listing data** within each zone
3. **Demographic data** at zone or sub-zone resolution (race/ethnicity, income
   proxies)

This doc lists what's confirmed, what's promising-but-unverified, and what's a
dead end, so the debate isn't re-discovering the same ground.

---

## Requirement 1: Attendance zone boundary polygons

**Confirmed dead end:** No free, current, national dataset exists. NCES's School
Attendance Boundary Survey (SABS) is the only free federal source and it's stale
— last collected 2015-16, not updated since, unreliable for fast-growing/rezoning
districts. Endpoint exists at
`https://nces.ed.gov/opengis/rest/services/K12_School_Locations/SABS_1516/MapServer`
(supports GeoJSON output) if a rough historical shape is ever useful, but treat
as unreliable for current boundaries.

**Most promising lead — district-run Esri/ArcGIS apps.** All three districts host
their zone lookup on Esri ArcGIS Online / Instant Apps, which are almost always
backed by a public FeatureServer that can be queried directly for GeoJSON without
scraping (it's the same data the public-facing lookup tool queries client-side).

- **Leander ISD**: "Zone Lookup" Instant App —
  `https://www.arcgis.com/apps/instant/lookup/index.html?appid=68baf4729b254d0e9c281f78bf62db67`
  (also mirrored at `pasatx.maps.arcgis.com`). Older webapp viewer version also
  found: `arcgis.com/apps/webappviewer/index.html?id=a22c14e2442349cca7e71f84547a0fa2`
  (labeled 2021-22, may be outdated — prefer the Instant App).
- **Round Rock ISD**: webapp viewer at
  `portal.metrostudygis.com/arcgis/apps/webappviewer/index.html?id=664b1d4bdb2448aa86307343b34ba9d0`,
  and an ArcGIS Online item at
  `arcgis.com/home/item.html?id=4349a7b2dd0249c58c66ac946b8ec2a9`. Official
  district page: `roundrockisd.org/o/rrisd/page/school-boundaries-and-maps`.
- **Eanes ISD**: official page at `eanesisd.net/district/maps`, links a
  "SchoolSearch" address-lookup tool. Underlying tech not yet confirmed as
  ArcGIS — needs inspection.

**Task:** For each Instant App/webapp URL, inspect the page source or network
requests (browser devtools / `curl` + grep) to find the underlying
`FeatureServer` or `MapServer` URL. If it's `f=json` capable and access is set to
"everyone" (no auth), it can be queried directly, e.g.:
```
https://services.arcgis.com/.../FeatureServer/0/query?where=1=1&outFields=*&f=geojson
```
This returns actual polygon geometry, not points. Verify the `access` level isn't
org-restricted before assuming this works — some ArcGIS Online items are shared
publicly for viewing in the app wrapper but the service itself may still require a
token. If blocked, fall back to manual tracing (see below).

**Reliable fallback — zipdatamaps.com.** Independently confirmed this site has
already built attendance-zone maps (elementary/middle/high) for Eanes ISD, Round
Rock ISD, and Leander ISD, each with additional overlay maps for
**race/ethnicity, free/reduced lunch participation, and Title I status** — i.e.,
requirements 1 and 3 already combined by a third party. No confirmed bulk
download/API; likely rendered as static/Leaflet tiles. Worth checking their page
source for an underlying tile/GeoJSON source before assuming it needs re-tracing.
URLs follow pattern:
`zipdatamaps.com/schools/texas/district/map-of-{district}-tx-high-school-attendance-zones`

**Manual fallback if no API access found:** Google My Maps or QGIS, tracing by
hand over each district's official PDF/image boundary map. Slow (~20 min/district)
but guaranteed-accurate and legal. Not automatable, but a legitimate last resort.

**Important caveat to build in, not just note:** All three districts have active
or recent rezoning:
- Leander ISD: Faubion Elementary rezoning approved Feb 2026 for 2026-27;
  secondary (MS/HS) rezoning discussions ongoing as of March 2026 board meetings.
- Eanes ISD: first HS-adjacent rezoning since 2019, approved Feb 2025 (Barton
  Creek/Valley View/Forest Trail elementary boundaries), effective 2025-26.
- Round Rock ISD: not confirmed either way — check `roundrockisd.org` news/board
  minutes.

Any tool built on this data needs a visible "boundaries as of [date], verify
before purchase" disclaimer and ideally a re-scrape cadence (annual, ahead of
enrollment season ~Feb-May).

---

## Requirement 2: Active home listing data

**Confirmed dead end:** Zillow's public API (ZWSID) was retired September 30,
2021, permanently. Current access is either Bridge Interactive (MLS-affiliated
brokerages/approved partners only, weeks-long application) or third-party scrapers
that violate Zillow's Terms of Use (explicitly prohibits "robot, spider, scraper
or other similar data gathering tools") by evading their PerimeterX bot defenses.
**Do not build a scraper against Zillow — this is a hard no**, not a "find a
cleverer workaround" — it's a ToS/anti-bot circumvention issue, not just a
technical one.

**Legitimate alternatives, in order of usefulness:**
1. **County Appraisal District open data** — Travis Central Appraisal District
   (TCAD) and Williamson CAD publish property-level records (ownership, assessed
   value, sale history) as genuinely open government data, often bulk-downloadable.
   Not "active listings" (no for-sale status) but real, free, legal, and good for
   valuation/comps.
2. **Redfin Data Center** — free aggregated CSVs (median price, inventory,
   days-on-market) by metro/zip/neighborhood. No individual listings, but good for
   zone-level trend data with zero legal risk.
3. **MLS public consumer portal** — Austin/Central Texas market runs on UnlockMLS;
   check if they have a public-facing consumer search (most MLSs do) — browsable,
   not bulk-exportable, but legitimate.
4. **Realtor.com** — similar public/legal ambiguity to Zillow; check their current
   developer terms before assuming it's cleaner, don't assume it is.

**Recommendation:** Treat "active listings" as the one requirement that doesn't
have a clean free bulk-data answer right now. Best realistic build: point the tool
at manual browsing (open Zillow/Redfin in a linked tab per zone) rather than
trying to ingest listings programmatically.

---

## Requirement 3: Demographic data

**Fully solved, free, no caveats.** This is standard public Census data:
- **American Community Survey (ACS)**, 5-year estimates, via `data.census.gov` or
  the Census API — race/ethnicity, income, education attainment down to
  block-group level.
- **TIGER/Line Shapefiles** (Census Bureau, free) — the geographic boundaries to
  join ACS data against (block groups, tracts).
- To join Census geography to school attendance zones (which don't align with
  Census boundaries), need either a spatial join (block group centroid inside zone
  polygon) once Requirement 1 is solved, or use zipdatamaps.com's pre-built
  race/ethnicity-by-zone maps directly (see above — they've already done this join
  for all three districts, down to elementary/middle/high level).

No scraping, no ToS issue, no rate limits worth worrying about at this scale.

---

## Suggested build order
1. Confirm Census API key (free, instant signup) and pull ACS demographic data for
   the Austin metro — lowest-risk, immediately productive.
2. Inspect the three ArcGIS apps above for open FeatureServer endpoints —
   highest-value if it works, cheap to test and fail fast.
3. If ArcGIS endpoints are locked, scrape zipdatamaps.com's page structure (not
   Zillow — this site has no comparable ToS/anti-bot posture found so far, but
   verify its own terms before bulk-pulling) or fall back to manual tracing for the
   3 target zones only (not all ~15+ zones in the metro).
4. Skip programmatic listings ingestion initially; link out to Zillow/Redfin
   manually per zone rather than building anything that touches their anti-bot
   systems.
