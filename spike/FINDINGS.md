# Spike Findings — Step 0 GO/NO-GO (run 2026-07-03)

The spike per docs/SPEC.md §0: does an **anonymous** request return real polygon
**geometry** (not merely count/metadata) from each district's boundary service?

## Verdict

| District | Verdict | Service |
|---|---|---|
| Leander ISD | **GO** | ArcGIS FeatureServer (proxied via `utility.arcgis.com/usrsvcs`) |
| Round Rock ISD | **GO** | Self-hosted ArcGIS MapServer (`server.metrostudygis.com`) |
| Eanes ISD | **NO-GO** (for API fetch) | MyPlanWare Next.js SPA — no anonymous endpoint found in page HTML |

## Leander ISD — GO

Instant App `68baf4729b254d0e9c281f78bf62db67` → webmap
`7ee3f8924ccc4477ad89684cf3ea2025` ("Leander ISD **2024-25** Attendance Zone
Finder", by Population and Survey Analysts / pasatx).

Layers (all anonymous-queryable, `outSR=4326&f=geojson` honored):

```
.../Leander_AZ_Finder_24_25_WFL1/FeatureServer/5   High School (9th-10th) Zones
.../Leander_AZ_Finder_24_25_WFL1/FeatureServer/7   High School (11th-12th) Zones
.../Leander_AZ_Finder_24_25_WFL1/FeatureServer/4   Middle School Zones
.../Leander_AZ_Finder_24_25_WFL1/FeatureServer/3   Elementary School Zones
```
Full base URL: `https://utility.arcgis.com/usrsvcs/servers/09a49c137df944e3b8399905ef8b3d8b/rest/services/Leander_AZ_Finder_24_25_WFL1/FeatureServer`

Probe result: HTTP 200, `Polygon` with EPSG:4326 coordinates, anonymous.

Schema notes:
- **Name field: `DisplayName`** (e.g. "Cedar Park High School"). Zone field
  `HSAZ_2425` is vintage-stamped — do not key on it.
- `Grades` attribute says "9th-12th" even on the 9th-10th layer — LISD splits
  some feeder patterns across two layers; verify which layer holds Vandegrift
  at first real fetch.
- Volatile props confirmed churning: `Shape__Area`, `Shape__Length`, `OBJECTID`.
- **Vintage caveat:** the app is labeled 2024-25. LISD approved rezoning Feb 2026
  for 2026-27; check for a newer Instant App before each refresh season.

## Round Rock ISD — GO

The arcgis.com item `4349a7b2dd0249c58c66ac946b8ec2a9` is **org-restricted
(403)** — but the district's self-hosted portal app is public:
`portal.metrostudygis.com` webappviewer `664b1d4bdb2448aa86307343b34ba9d0` →
webmap `0842a9153b0249f4b45f1f742cf439f0` →

```
https://server.metrostudygis.com/arcgis/rest/services/Round_Rock_ISD_2526/MapServer
  layer 2 = HIGH BOUNDARIES   (also: 3 MIDDLE, 4 ELEMENTARY, 0 SCHOOLS)
```

Probe result: HTTP 200, `Polygon` with EPSG:4326 coordinates, anonymous.
Capabilities: `Map,Query,Data`.

Schema notes:
- **Name fields: `HIGH_NAME`** ("CEDAR RIDGE") / `CAMPUS` ("CEDAR RIDGE HIGH") —
  uppercase.
- **Vintage ambiguity to resolve at the one-time eyeball:** service is named
  `Round_Rock_ISD_2526` (2025-26) but the probed feature's `SCHL_YEAR` attribute
  says `2024-25`. Record whichever the district's official page confirms.
- Volatile props: `created_date`, `last_edited_date`, `last_edited_user`,
  `SHAPE_Length`, `SHAPE_Area`, `OBJECTID`.

## Eanes ISD — NO-GO (manual-trace fallback, per spec)

`eanesisd.net/district/maps` links to `apps.myplanware.com/schoolsearch/eanes` —
a **MyPlanWare** Next.js SPA, not ArcGIS. No API/GeoJSON endpoint is visible in
the served HTML (data loads client-side from webpack bundles).

Fallback per SPEC §0: hand-trace Westlake HS zone in QGIS/Google My Maps over the
district's official boundary map (~20 min, one-time), feed the GeoJSON in as
`boundary_source = "manual_trace"`.

Possible future upgrade: inspect the SPA's network traffic in a real browser to
find MyPlanWare's internal boundary endpoint — but verify MyPlanWare's ToS before
automating against it. Not required for v1.

## Consequences for the build

- `fetch_boundaries.py` proceeds (build-order step 2) for Leander + RRISD.
- RRISD is a **MapServer**, not FeatureServer — same query API, keep
  `boundary_source` generic (`arcgis_query_url`).
- Eanes/Westlake enters `zones.toml` with `boundary_source = "manual_trace"`;
  its polygon is authored, not fetched, and the fetch gates don't apply (the
  one-time eyeball IS its provenance).
- Raw probe payloads in this directory are throwaway evidence; this FINDINGS
  file is the durable record.
