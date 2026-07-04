"""fetch_census.py — pull ACS counts+MOE and PL-94 block weights (SPEC.md §2).

Fully automated: Census is the settled, versioned, government API. Writes to
data/census/ (gitignored — bulky, fully reproducible from this script).

What it pulls, per county in zones.toml:
  - ACS 5-year COUNT tables + their MOE columns, at block group AND tract
    (tract feeds the automatic CV fallback in join_zones.py):
        B03002 race/ethnicity, B19001 income brackets, B15003 education.
    B19013 (median income) is DELIBERATELY not pulled — averaging medians is
    statistically illegal; the median is derived from apportioned B19001
    brackets downstream.
  - 2020 PL-94 (dec/pl) per-block P1 (total pop), P3 (18+), H1 (occupied
    housing units) — the per-table weight universes.
  - Block representative points (internal points, CENTLAT/CENTLON) from the
    Census TIGERweb REST API, joined to the PL-94 rows by GEOID.

Design notes:
  - Block group / tract POLYGONS are not fetched: the apportionment method
    needs only block points + GEOID nesting (bg = GEOID[:12], tract = [:11]).
  - CRS: TIGERweb returns WGS84 lon/lat (EPSG:4326) for CENTLAT/CENTLON;
    blocks are stored as Point geometry in 4326. (TIGER/Line shapefiles ship
    NAD83 — by taking the API path we get 4326 directly; the sub-meter
    NAD83/WGS84 datum difference is far below block-point resolution.)
  - VINTAGE NESTING IS ASSERTED: every block's GEOID[:12] must exist in the
    ACS block-group set, else hard-fail — a mismatched vintage silently
    corrupts apportionment weights with no error otherwise.

Census API key: optional but recommended (free, instant). Set CENSUS_API_KEY.
Anonymous access works for light use but is rate-limited.
"""
from __future__ import annotations

import json
import os
import sys
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "census"

# Load ROOT/.env (gitignored) so CENSUS_API_KEY survives across sessions
# without living in the environment or the repo.
_env_file = ROOT / ".env"
if _env_file.exists():
    for line in _env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

ACS_VINTAGE = "2023"  # ACS 5-year ending year
ACS_BASE = f"https://api.census.gov/data/{ACS_VINTAGE}/acs/acs5"
PL_BASE = "https://api.census.gov/data/2020/dec/pl"
TIGERWEB_ROOT = ("https://tigerweb.geo.census.gov/arcgis/rest/services/"
                 "TIGERweb/Tracts_Blocks/MapServer")

# Count variables per table (E = estimate, M = MOE pulled for each).
TABLE_VARS = {
    "B03002": ["001", "003", "004", "006", "012"],
    "B19001": [f"{i:03d}" for i in range(1, 18)],   # _001 total + 16 brackets
    "B15003": ["001", "022", "023", "024", "025"],
}


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "home-finder/0.1"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = resp.read().decode("utf-8")
        # The API 302s to an HTML page when the key is absent/not yet active —
        # name that failure instead of letting json.loads throw a cryptic error.
        if "missing_key" in resp.url or body.lstrip().startswith("<"):
            raise RuntimeError(
                "Census API rejected the request (missing/inactive key?). "
                "Set CENSUS_API_KEY in .env — new keys can take a few minutes "
                "to activate. Signup: https://api.census.gov/data/key_signup.html")
        return json.loads(body)


def _api_url(base: str, get_vars: list[str], geo_for: str, geo_in: str | None) -> str:
    params = {"get": ",".join(get_vars), "for": geo_for}
    if geo_in:
        params["in"] = geo_in
    key = os.environ.get("CENSUS_API_KEY")
    if key:
        params["key"] = key
    return f"{base}?{urllib.parse.urlencode(params)}"


def _rows_to_dict(rows: list[list[str]]) -> list[dict]:
    header, *data = rows
    return [dict(zip(header, r)) for r in data]


# ---------------------------------------------------------------------------
# ACS counts + MOE, block group and tract level
# ---------------------------------------------------------------------------

def fetch_acs(counties: list[str]) -> dict:
    """-> {"bg": {table: {"counts": {var:{geoid:val}}, "moes": {...}}},
           "tract": {...}}   geoid: 12-char bg / 11-char tract."""
    acs = {"bg": {}, "tract": {}}
    for table, suffixes in TABLE_VARS.items():
        e_vars = [f"{table}_{s}E" for s in suffixes]
        m_vars = [f"{table}_{s}M" for s in suffixes]
        for level, geo_for in (("bg", "block group:*"), ("tract", "tract:*")):
            counts: dict[str, dict[str, float]] = {}
            moes: dict[str, dict[str, float]] = {}
            for county in counties:
                state, cty = county[:2], county[2:]
                geo_in = f"state:{state} county:{cty}"
                if level == "bg":
                    geo_in += " tract:*"
                url = _api_url(ACS_BASE, e_vars + m_vars, geo_for, geo_in)
                print(f">> ACS {table} {level} {county} ...", flush=True)
                for row in _rows_to_dict(_get_json(url)):
                    geoid = row["state"] + row["county"] + row["tract"]
                    if level == "bg":
                        geoid += row["block group"]
                    for s in suffixes:
                        var = f"{table}_{s}"
                        e = float(row.get(f"{var}E") or 0)
                        # Census sentinel MOEs (e.g. -555555555) mean
                        # "controlled estimate, no sampling error" -> MOE 0.
                        m_raw = float(row.get(f"{var}M") or 0)
                        m = m_raw if m_raw >= 0 else 0.0
                        counts.setdefault(var, {})[geoid] = e
                        moes.setdefault(var, {})[geoid] = m
            acs[level][table] = {"counts": counts, "moes": moes}
    return acs


# ---------------------------------------------------------------------------
# PL-94 block weights + TIGERweb block points
# ---------------------------------------------------------------------------

def fetch_pl94(counties: list[str]) -> dict[str, dict]:
    """-> {block GEOID (15 char): {"P1": int, "P3": int, "H1": int}}"""
    out: dict[str, dict] = {}
    for county in counties:
        state, cty = county[:2], county[2:]
        url = _api_url(PL_BASE, ["P1_001N", "P3_001N", "H1_002N"],
                       "block:*", f"state:{state} county:{cty}")
        print(f">> PL-94 blocks {county} ...", flush=True)
        for row in _rows_to_dict(_get_json(url)):
            geoid = row["state"] + row["county"] + row["tract"] + row["block"]
            out[geoid] = {
                "P1": int(row["P1_001N"] or 0),   # total population
                "P3": int(row["P3_001N"] or 0),   # 18+ population
                "H1": int(row["H1_002N"] or 0),   # OCCUPIED housing units
            }
    return out


def _find_blocks_layer() -> int:
    """Locate the 2020 Census Blocks layer id on TIGERweb (don't hardcode —
    layer numbering shifts across TIGERweb releases)."""
    meta = _get_json(f"{TIGERWEB_ROOT}?f=json")
    for layer in meta.get("layers", []):
        if "block" in layer["name"].lower() and "group" not in layer["name"].lower():
            return layer["id"]
    raise RuntimeError("TIGERweb: no Census Blocks layer found — service layout changed?")


def fetch_block_points(counties: list[str]) -> dict[str, tuple[float, float]]:
    """-> {block GEOID: (lon, lat)} internal points from TIGERweb, EPSG:4326.
    Paginated (TIGERweb caps ~2000 records/request); attributes only, no
    polygon geometry — the join needs points, not shapes."""
    layer_id = _find_blocks_layer()
    points: dict[str, tuple[float, float]] = {}
    for county in counties:
        state, cty = county[:2], county[2:]
        offset = 0
        while True:
            params = urllib.parse.urlencode({
                "where": f"STATE='{state}' AND COUNTY='{cty}'",
                "outFields": "GEOID,CENTLAT,CENTLON",
                "returnGeometry": "false",
                "resultOffset": offset,
                "f": "json",
            })
            d = _get_json(f"{TIGERWEB_ROOT}/{layer_id}/query?{params}")
            if "error" in d:
                raise RuntimeError(f"TIGERweb error: {d['error']}")
            feats = d.get("features", [])
            for f in feats:
                a = f["attributes"]
                points[a["GEOID"]] = (float(a["CENTLON"]), float(a["CENTLAT"]))
            print(f">> TIGERweb blocks {county}: {len(points)} cumulative", flush=True)
            if not d.get("exceededTransferLimit") or not feats:
                break
            offset += len(feats)
    return points


# ---------------------------------------------------------------------------
# Assembly + the nesting assertion
# ---------------------------------------------------------------------------

def assemble_blocks(pl94: dict[str, dict],
                    points: dict[str, tuple[float, float]],
                    acs_bg_geoids: set[str]) -> list[dict]:
    blocks = []
    missing_pts = 0
    for geoid, w in pl94.items():
        pt = points.get(geoid)
        if pt is None:
            missing_pts += 1
            continue
        # THE VINTAGE-NESTING ASSERTION (hard-fail; see module docstring).
        bg = geoid[:12]
        if bg not in acs_bg_geoids:
            raise AssertionError(
                f"vintage mismatch: block {geoid} bg {bg} not in ACS block-group "
                f"set — PL-94/TIGER/ACS vintages do not nest; apportionment "
                f"weights would be silently wrong")
        blocks.append({
            "GEOID": geoid, **w,
            "geometry": {"type": "Point", "coordinates": [pt[0], pt[1]]},
        })
    if missing_pts:
        print(f">> WARNING: {missing_pts} PL-94 blocks had no TIGERweb point "
              f"(dropped; typically zero-pop water blocks)", flush=True)
    return blocks


def counties_from_zones() -> list[str]:
    cfg = tomllib.loads((ROOT / "zones.toml").read_text(encoding="utf-8"))
    counties: list[str] = []
    for zone in cfg.values():
        for c in zone.get("counties", []):
            if c not in counties:
                counties.append(c)
    return counties


def main() -> int:
    counties = counties_from_zones()
    if not counties:
        print(">> no counties found in zones.toml", file=sys.stderr)
        return 1
    print(f">> counties: {counties}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    acs = fetch_acs(counties)
    bg_geoids = {g for table in acs["bg"].values()
                 for per_geo in table["counts"].values() for g in per_geo}
    pl94 = fetch_pl94(counties)
    points = fetch_block_points(counties)
    blocks = assemble_blocks(pl94, points, bg_geoids)

    (OUT_DIR / "acs.json").write_text(json.dumps(acs), encoding="utf-8")
    (OUT_DIR / "pl94-blocks.json").write_text(json.dumps(blocks), encoding="utf-8")
    print(f">> wrote {OUT_DIR / 'acs.json'}")
    print(f">> wrote {OUT_DIR / 'pl94-blocks.json'} ({len(blocks)} blocks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
