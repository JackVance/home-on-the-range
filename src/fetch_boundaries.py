"""fetch_boundaries.py — attendance-zone polygons: fetch automated, promotion gated.

SPEC.md §3. Endpoint discovery was the one-time manual step (spike/FINDINGS.md);
this script queries the recorded URLs, canonicalizes the result so git-as-diff-
layer is honest, and gates promotion behind structural + semantic + area checks.

Gate philosophy: ALWAYS-WRITE / TAINT-RUN / GATE-COMMIT — when a check trips,
the file you most need to inspect is the one just fetched, so it is written to
the working tree with a .REVIEW marker beside it and the run exits non-zero.
The commit is the promotion; the config edit is the human acknowledgment.

CRS invariant: query pins outSR=4326; area is geodesic (pyproj.Geod) — a
Web-Mercator area at Austin's latitude is inflated ~1.33x and would hide
inside a naive gate.
"""
from __future__ import annotations

import json
import sys
import tomllib
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from pyproj import Geod
from shapely.geometry import mapping, shape
from shapely.geometry.polygon import orient
from shapely.ops import transform, unary_union
from shapely.validation import make_valid

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "boundaries"

AREA_TOLERANCE = 3.0   # x expected_area_km2, either direction (smoke alarm)
NDIGITS = 6            # ~11 cm; kills FeatureServer coordinate-tail jitter
KEEP_PROPS = ("school_name", "district", "effective_year")

_GEOD = Geod(ellps="WGS84")


class EndpointMoved(Exception):
    """Service unreachable — district likely republished. Re-run discovery."""


class EmptyResult(Exception):
    """Service alive, zero features — field/value renamed on republish?"""


class ReviewRequired(Exception):
    """A gate tripped. File is written; commit is blocked pending review."""


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------

def query_zone(url: str, where: str) -> list[dict]:
    params = urllib.parse.urlencode({
        "where": where, "outFields": "*", "outSR": 4326, "f": "geojson",
    })
    req = urllib.request.Request(f"{url}/query?{params}",
                                 headers={"User-Agent": "home-finder/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise EndpointMoved(f"HTTP {e.code} from {url} — district may have "
                            f"republished; re-run endpoint discovery") from e
    except urllib.error.URLError as e:
        raise EndpointMoved(f"unreachable: {url} ({e.reason})") from e
    if "error" in body:
        raise EndpointMoved(f"service error from {url}: {body['error']}")
    features = body.get("features", [])
    if not features:
        raise EmptyResult(f"0 features for where={where!r} — field/value may "
                          f"have changed on republish")
    return features


# ---------------------------------------------------------------------------
# Canonicalization — makes an unchanged re-fetch byte-identical and a real
# rezoning a small readable diff
# ---------------------------------------------------------------------------

def round_coords(geom, ndigits: int = NDIGITS):
    return transform(lambda x, y, z=None: (round(x, ndigits), round(y, ndigits)),
                     geom)


def canonicalize_rings(geom):
    """RFC7946 winding (exterior CCW, holes CW) + deterministic ordering of a
    MultiPolygon's constituent polygons by (min-x, min-y) of bounds — a pure
    geometric serialization tiebreak scoped INSIDE one feature."""
    if geom.geom_type == "Polygon":
        return orient(geom, sign=1.0)
    if geom.geom_type == "MultiPolygon":
        polys = sorted((orient(p, sign=1.0) for p in geom.geoms),
                       key=lambda p: (p.bounds[0], p.bounds[1]))
        return type(geom)(polys)
    raise ReviewRequired(f"non-polygonal geometry after union: {geom.geom_type}")


def area_km2(geom_wgs84) -> float:
    """Geodesic area on the ellipsoid. No projected-CRS choice to get wrong."""
    a, _ = _GEOD.geometry_area_perimeter(geom_wgs84)
    return abs(a) / 1e6


def whitelist_props(features: list[dict], zone_cfg: dict) -> dict:
    """Keep the handful of semantic labels that make the artifact
    self-verifying; DROP the server bookkeeping that churns every fetch
    (OBJECTID, Shape_Length/Area, last_edited_date, GlobalID...)."""
    name_field = zone_cfg.get("boundary_name_field")
    raw = features[0].get("properties", {})
    return {
        "school_name": raw.get(name_field) if name_field else zone_cfg["high_school"],
        "district": zone_cfg["district"],
        "effective_year": zone_cfg.get("district_effective_year"),
    }


def normalize_zone(features: list[dict], zone_cfg: dict) -> dict:
    """N raw features for ONE school -> one canonical (Multi)Polygon FC.
    Postcondition: valid geometry AND byte-idempotent serialization."""
    # Structural gate (schema-independent, PRIMARY).
    expected = zone_cfg["expected_features"]
    if len(features) != expected:
        raise ReviewRequired(
            f"{len(features)} features returned, expected {expected} — loose "
            f"where clause, or a real republish (bump expected_features to "
            f"acknowledge)")
    # Semantic backstop (only when the service has a usable name field).
    if zone_cfg.get("boundary_has_name"):
        field = zone_cfg["boundary_name_field"]
        names = {f.get("properties", {}).get(field) for f in features}
        if len(names) != 1 or None in names:
            raise ReviewRequired(f"mixed/unlabeled schools in result: {names} "
                                 f"(field {field!r})")
    geom = unary_union([shape(f["geometry"]) for f in features])
    geom = round_coords(geom)
    geom = make_valid(geom)          # rounding is topology-hostile
    if not geom.is_valid:
        raise ReviewRequired("normalization produced invalid geometry")
    geom = canonicalize_rings(geom)
    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": whitelist_props(features, zone_cfg),
            "geometry": json.loads(json.dumps(mapping(geom))),
        }],
    }


def serialize(fc: dict) -> str:
    return json.dumps(fc, indent=1, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# Per-zone pipeline
# ---------------------------------------------------------------------------

def fetch_zone(name: str, zone_cfg: dict) -> tuple[bool, str]:
    """Returns (ok, message). Always writes the artifact if a fetch succeeded;
    gates only block the COMMIT (via .REVIEW marker + non-zero exit)."""
    out_file = OUT_DIR / f"{name}.geojson"
    review_file = OUT_DIR / f"{name}.geojson.REVIEW"
    meta_file = OUT_DIR / f"{name}.meta.json"

    features = query_zone(zone_cfg["boundary_url"], zone_cfg["boundary_where"])

    problems: list[str] = []
    try:
        fc = normalize_zone(features, zone_cfg)
    except ReviewRequired as e:
        # Still write the raw evidence for inspection.
        out_file.write_text(serialize({"type": "FeatureCollection",
                                       "features": features}), encoding="utf-8")
        review_file.write_text(f"{e}\n", encoding="utf-8")
        return False, f"GATE: {e} (raw result written for inspection)"

    # Idempotence self-check on the PURE canonicalization (the gates apply to
    # raw server output only — the normalized feature's props are already
    # whitelisted, so re-gating it would trip on the renamed name field).
    geom1 = shape(fc["features"][0]["geometry"])
    geom2 = canonicalize_rings(make_valid(round_coords(geom1)))
    if json.dumps(mapping(geom2), sort_keys=True) != \
       json.dumps(json.loads(json.dumps(mapping(geom1))), sort_keys=True):
        problems.append("canonicalization is not byte-idempotent — investigate")

    # Geodesic area gate (smoke alarm: catches empty/whole-district/wrong-layer,
    # NOT a subtly-wrong 1.4x neighbor — verified_against covers that).
    geom = shape(fc["features"][0]["geometry"])
    actual = area_km2(geom)
    expected_area = zone_cfg["expected_area_km2"]
    if not (expected_area / AREA_TOLERANCE <= actual <= expected_area * AREA_TOLERANCE):
        problems.append(
            f"geodesic area {actual:.1f} km² vs expected {expected_area} km² "
            f"(tolerance {AREA_TOLERANCE}x) — fix boundary_where or bump "
            f"expected_area_km2 to acknowledge")

    out_file.write_text(serialize(fc), encoding="utf-8")

    prior_meta = {}
    if meta_file.exists():
        prior_meta = json.loads(meta_file.read_text(encoding="utf-8"))
    meta = {
        "source_url": zone_cfg["boundary_url"],
        "source_type": zone_cfg["boundary_source"],
        "boundary_where": zone_cfg["boundary_where"],
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "district_effective_year": zone_cfg.get("district_effective_year"),
        "geodesic_area_km2": round(actual, 1),
        # Human fields survive re-fetches; git can't know these.
        "verified_against": prior_meta.get("verified_against"),
        "verified_by_human": prior_meta.get("verified_by_human", False),
        "known_rezoning": prior_meta.get("known_rezoning"),
    }
    meta_file.write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n",
                         encoding="utf-8")

    if problems:
        review_file.write_text("\n".join(problems) + "\n", encoding="utf-8")
        return False, f"GATE: {'; '.join(problems)} (file written)"
    if review_file.exists():
        review_file.unlink()
    return True, f"OK — {actual:.1f} km², {len(features)} feature(s) -> {out_file.name}"


def main(only: list[str] | None = None) -> int:
    cfg = tomllib.loads((ROOT / "zones.toml").read_text(encoding="utf-8"))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    failures = 0
    for name, zone_cfg in cfg.items():
        if only and name not in only:
            continue
        if zone_cfg.get("boundary_source") == "manual_trace":
            print(f">> {name}: manual_trace — polygon is authored by hand, skipping")
            continue
        try:
            ok, msg = fetch_zone(name, zone_cfg)
        except (EndpointMoved, EmptyResult) as e:
            ok, msg = False, f"{type(e).__name__}: {e}"
        print(f">> {name}: {msg}")
        if not ok:
            failures += 1
    # One zone tripping must not abort the others; report and exit non-zero.
    if failures:
        print(f">> {failures} zone(s) need review — commit is blocked for them",
              file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or None))
