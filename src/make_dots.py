"""make_dots.py — pre-generate the ethnic dot-map layer (viewer draws, never computes).

One dot = PEOPLE_PER_DOT people, colored by race/ethnicity, from 2020
decennial PL-94 **P2** per-block counts (the same source as the classic
racial dot maps; block-level counts are actual enumeration, not estimates).

Placement: dots are jittered deterministically around each block's internal
point, within the block's equivalent radius (sqrt(land area / pi)) — so dots
stay roughly inside the block footprint without needing block polygons.
Placement is APPROXIMATE and the viewer legend says so; category counts are
exact. Jitter is seeded from (GEOID, category, i): re-running this script
produces byte-identical output (git-diffable like everything else).

Output: data/derived/dots.json — gitignored (bulky, fully reproducible).
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "derived" / "dots.json"

PEOPLE_PER_DOT = 25

# P2: HISPANIC OR LATINO, AND NOT HISPANIC OR LATINO BY RACE (2020 PL-94).
P2_VARS = {
    "P2_002N": "hispanic",
    "P2_005N": "white",      # NH white alone
    "P2_006N": "black",      # NH black alone
    "P2_008N": "asian",      # NH asian alone
}
# Everyone not in the four categories above renders as "other".
CATEGORIES = ["white", "hispanic", "black", "asian", "other"]
COLORS = {"white": "#4a7fd4", "hispanic": "#e8a33d", "black": "#3fa45b",
          "asian": "#d44a4a", "other": "#8a6d5c"}

PL_BASE = "https://api.census.gov/data/2020/dec/pl"
TIGERWEB_ROOT = ("https://tigerweb.geo.census.gov/arcgis/rest/services/"
                 "TIGERweb/Tracts_Blocks/MapServer")

# Reuse the .env loader convention from fetch_census.
import os  # noqa: E402
_env = ROOT / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "home-finder/0.1"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = resp.read().decode("utf-8")
        if "missing_key" in resp.url or body.lstrip().startswith("<"):
            raise RuntimeError("Census API rejected request (key missing/inactive?)")
        return json.loads(body)


def fetch_p2(counties: list[str]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    get_vars = ["P2_001N"] + list(P2_VARS)
    key = os.environ.get("CENSUS_API_KEY", "")
    for county in counties:
        state, cty = county[:2], county[2:]
        q = urllib.parse.urlencode({
            "get": ",".join(get_vars), "for": "block:*",
            "in": f"state:{state} county:{cty}", "key": key})
        print(f">> P2 blocks {county} ...", flush=True)
        rows = _get_json(f"{PL_BASE}?{q}")
        header, *data = rows
        idx = {h: i for i, h in enumerate(header)}
        for r in data:
            geoid = r[idx["state"]] + r[idx["county"]] + r[idx["tract"]] + r[idx["block"]]
            cats = {name: int(r[idx[var]] or 0) for var, name in P2_VARS.items()}
            total = int(r[idx["P2_001N"]] or 0)
            cats["other"] = max(0, total - sum(cats.values()))
            if total:
                out[geoid] = cats
    return out


def _find_blocks_layer() -> int:
    meta = _get_json(f"{TIGERWEB_ROOT}?f=json")
    for layer in meta.get("layers", []):
        name = layer["name"].lower()
        if "block" in name and "group" not in name:
            return layer["id"]
    raise RuntimeError("TIGERweb blocks layer not found")


def fetch_points_with_area(counties: list[str]) -> dict[str, tuple[float, float, float]]:
    """{GEOID: (lon, lat, land_m2)} — internal point + land area per block."""
    layer = _find_blocks_layer()
    out: dict[str, tuple[float, float, float]] = {}
    for county in counties:
        state, cty = county[:2], county[2:]
        offset = 0
        while True:
            q = urllib.parse.urlencode({
                "where": f"STATE='{state}' AND COUNTY='{cty}'",
                "outFields": "GEOID,CENTLAT,CENTLON,AREALAND",
                "returnGeometry": "false", "resultOffset": offset, "f": "json"})
            d = _get_json(f"{TIGERWEB_ROOT}/{layer}/query?{q}")
            feats = d.get("features", [])
            for f in feats:
                a = f["attributes"]
                out[a["GEOID"]] = (float(a["CENTLON"]), float(a["CENTLAT"]),
                                   float(a["AREALAND"] or 0))
            if not d.get("exceededTransferLimit") or not feats:
                break
            offset += len(feats)
        print(f">> TIGERweb points+area through {county}: {len(out)}", flush=True)
    return out


def _unit_hash(*parts) -> tuple[float, float]:
    """Two deterministic uniform [0,1) values from a hash of the parts."""
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    a = int.from_bytes(h[:8], "big") / 2**64
    b = int.from_bytes(h[8:16], "big") / 2**64
    return a, b


def make_dots(p2: dict, points: dict) -> list[list]:
    dots: list[list] = []
    for geoid, cats in sorted(p2.items()):
        pt = points.get(geoid)
        if pt is None:
            continue
        lon, lat, land_m2 = pt
        radius_m = max(40.0, math.sqrt(land_m2 / math.pi)) if land_m2 else 60.0
        # meters -> degrees at this latitude
        dlat = radius_m / 111_320.0
        dlon = radius_m / (111_320.0 * math.cos(math.radians(lat)))
        for cat in CATEGORIES:
            n = round(cats.get(cat, 0) / PEOPLE_PER_DOT)
            for i in range(n):
                u, v = _unit_hash(geoid, cat, i)
                r = math.sqrt(u)          # uniform over the disk
                theta = 2 * math.pi * v
                dots.append([
                    round(lon + r * math.cos(theta) * dlon, 5),
                    round(lat + r * math.sin(theta) * dlat, 5),
                    CATEGORIES.index(cat),
                ])
    return dots


def main() -> int:
    cfg = tomllib.loads((ROOT / "zones.toml").read_text(encoding="utf-8"))
    counties: list[str] = []
    for zone in cfg.values():
        for c in zone.get("counties", []):
            if c not in counties:
                counties.append(c)
    print(f">> counties: {counties}")
    p2 = fetch_p2(counties)
    points = fetch_points_with_area(counties)
    dots = make_dots(p2, points)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "people_per_dot": PEOPLE_PER_DOT,
        "source": "2020 decennial PL-94 P2 per block; placement approximate "
                  "(jittered within block-equivalent radius)",
        "categories": CATEGORIES,
        "colors": COLORS,
        "dots": dots,
    }, separators=(",", ":")), encoding="utf-8", newline="\n")
    print(f">> wrote {OUT} ({len(dots)} dots = "
          f"~{len(dots) * PEOPLE_PER_DOT:,} people)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
