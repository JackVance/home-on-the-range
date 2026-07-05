"""fetch_counties.py — county boundaries for the viewer's reference overlay.

Queries TIGERweb (generalized ACS service — plenty for a reference overlay,
~10x smaller than full TIGER) for every county intersecting the bounding box
of all committed zone boundaries, and writes ONE FeatureCollection to
data/boundaries/_counties.geojson (underscore prefix = not a zone; the viewer
loads it for the black county-line overlay).

Run: python src/fetch_counties.py
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOUNDARIES = ROOT / "data" / "boundaries"
OUT = BOUNDARIES / "_counties.geojson"

SERVICE = ("https://tigerweb.geo.census.gov/arcgis/rest/services/"
           "Generalized_ACS2023/State_County/MapServer")


def get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "home-finder/0.1"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def counties_layer_ids() -> list[int]:
    # NB: the service lists "Counties 500K" twice (one copy 400s on envelope
    # queries) and the un-suffixed "Counties" layer 400s too — so return every
    # candidate and let the caller use the first that actually answers.
    svc = get_json(f"{SERVICE}?f=json")
    ids = [l["id"] for l in svc["layers"] if l["name"] == "Counties 500K"]
    if not ids:
        raise SystemExit(f"no 'Counties 500K' layer in {SERVICE}: "
                         f"{[l['name'] for l in svc['layers']]}")
    return ids


def zones_bbox() -> tuple[float, float, float, float]:
    """Bounding box over every committed zone boundary (not _-prefixed)."""
    xmin, ymin, xmax, ymax = 180.0, 90.0, -180.0, -90.0

    def walk(coords):
        nonlocal xmin, ymin, xmax, ymax
        if isinstance(coords[0], (int, float)):
            x, y = coords[0], coords[1]
            xmin, xmax = min(xmin, x), max(xmax, x)
            ymin, ymax = min(ymin, y), max(ymax, y)
        else:
            for c in coords:
                walk(c)

    n = 0
    for p in BOUNDARIES.glob("*.geojson"):
        if p.name.startswith("_"):
            continue
        fc = json.loads(p.read_text(encoding="utf-8"))
        for f in fc.get("features", []):
            walk(f["geometry"]["coordinates"])
        n += 1
    if not n:
        raise SystemExit("no zone boundaries found — run fetch_boundaries first")
    print(f">> bbox over {n} zone files: "
          f"({xmin:.3f}, {ymin:.3f}) - ({xmax:.3f}, {ymax:.3f})")
    return xmin, ymin, xmax, ymax


def main() -> int:
    xmin, ymin, xmax, ymax = zones_bbox()
    params = urllib.parse.urlencode({
        "geometry": f"{xmin},{ymin},{xmax},{ymax}",
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": 4326, "outSR": 4326,
        "where": "1=1", "outFields": "NAME,GEOID", "f": "geojson",
    })
    feats = []
    for lid in counties_layer_ids():
        fc = get_json(f"{SERVICE}/{lid}/query?{params}")
        feats = fc.get("features", [])
        if feats:
            break
    if not feats:
        raise SystemExit("county query returned no features")
    feats.sort(key=lambda f: f["properties"]["GEOID"])
    OUT.write_text(json.dumps(
        {"type": "FeatureCollection", "features": feats},
        separators=(",", ":")) + "\n", encoding="utf-8")
    names = ", ".join(f["properties"]["NAME"] for f in feats)
    print(f">> wrote {OUT.name}: {len(feats)} counties — {names}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
