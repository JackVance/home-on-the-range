"""audit_zones.py — accuracy & completeness audit across all registered zones.

Codifies the checks that were previously ad-hoc terminal scripts, so they can
run before/after any zone buildout (and must, once multi-level zones arrive):

  1. OVERLAPS   — pairwise intersection area between zones of the same level;
                  anything above a sliver threshold is a boundary bug.
  2. COVERAGE   — per multi-HS district: the union of its zones vs its TIGER
                  district polygon; uncovered area means a missing zone
                  (this is the check that would have caught Glenn HS on day
                  one) or a legitimate carve-out to acknowledge.
  3. CONTAINS   — every zone must contain its own campus's coordinates (from
                  TEA); the scalable replacement for per-zone eyeballing.
                  Catches wrong-layer / wrong-name fetches automatically.
  4. ROSTER     — TEA's statewide campus list vs registered zones per
                  district: campuses serving the level that have no zone are
                  reported by name for human adjudication (magnets and
                  alternative campuses legitimately have none).

Exit non-zero if OVERLAPS or CONTAINS fail (hard errors); COVERAGE and
ROSTER findings are reported for judgment (carve-outs like Sun City are
legitimate). Run: python src/audit_zones.py
"""
from __future__ import annotations

import gzip
import json
import sys
import tomllib
import urllib.request

# Windows consoles default to cp1252, which can't print ∩ / km².
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path

from pyproj import Geod
from shapely.geometry import Point, shape
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parent.parent
BOUNDARIES = ROOT / "data" / "boundaries"
SCHOOLS = ROOT / "data" / "schools"

OVERLAP_SLIVER_KM2 = 0.05     # below this, digitization noise; above, a bug
COVERAGE_NOTE_KM2 = 2.0       # district holes above this get reported

# Known, adjudicated overlaps (still reported, not failed). Hays CISD's
# official boundary map is hand-drawn in Google My Maps; its HS zones
# overlap each other by ~0.2 km2 total at internal seams.
ALLOWED_OVERLAPS = {
    frozenset({"hayscisd-hays", "hayscisd-johnson"}),
    frozenset({"hayscisd-hays", "hayscisd-lehman"}),
}

# Campuses adjudicated as legitimately outside their own zone (verified
# against source maps 2026-07-04; distances shown). Districts do site
# buildings outside their attendance zones, and AISD's planning app carves
# reassignment areas out of existing polygons.
ALLOWED_OUTSIDE = {
    "eanes-forest-trail-es":       "162 m — boundary-adjacent after Eanes 2025 rezoning",
    "austin-pecan-springs-es-es":  "552 m — inside an AISD reassignment carve",
    "austin-langford-es-es":       "1.7 km — inside an AISD reassignment carve",
    "hayscisd-carpenter-hill-es":  "2.3 km — outside own zone in BOTH year folders of the district map",
    "hayscisd-simon-ms":           "9.7 km — building sited outside own zone per district map, both years",
    "liberty-hill-louine-noble-es": "zone is the 2026-27 Saddleback-area assignment; "
                                    "current Louine Noble building sits elsewhere (renames 2027-28)",
    # TEA coordinate errors (verified 2026-07-04): the TEA point for each of
    # these campuses lands in a DIFFERENT district entirely, so it's the TEA
    # lat/lon that's wrong, not the zone polygon.
    "sanmarcos-de-zavala-es":      "TEA point (29.973, -97.997) is in Hays CISD; shared verbatim with Rodriguez Elem",
    "sanmarcos-rodriguez-es":      "TEA point (29.973, -97.997) is in Hays CISD; shared verbatim with Dezavala Elem",
    "delvalle-hornsby-dunlap-es":  "TEA point (30.475, -97.767) is in Round Rock ISD, 25 km from the campus",
    "taylor-pasemann-es":          "TEA point (30.590, -97.305) is in Thrall ISD, not Taylor",
}

_geod = Geod(ellps="WGS84")


def km2(geom) -> float:
    if geom.is_empty:
        return 0.0
    return abs(_geod.geometry_area_perimeter(geom)[0]) / 1e6


def load_zones() -> dict[str, dict]:
    cfg = tomllib.loads((ROOT / "zones.toml").read_text(encoding="utf-8"))
    zones = {}
    for name, z in cfg.items():
        f = BOUNDARIES / f"{name}.geojson"
        if not f.exists():
            print(f"!! {name}: no boundary file — unfetched zone")
            continue
        fc = json.loads(f.read_text(encoding="utf-8"))
        zones[name] = {
            "cfg": z,
            "geom": shape(fc["features"][0]["geometry"]),
            "level": z.get("level", "high"),   # ready for multi-level
        }
    return zones


def check_overlaps(zones) -> int:
    print("\n== 1. OVERLAPS (same level) ==")
    bad = 0
    names = sorted(zones)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if zones[a]["level"] != zones[b]["level"]:
                continue
            ga, gb = zones[a]["geom"], zones[b]["geom"]
            if not ga.envelope.intersects(gb.envelope):
                continue
            area = km2(ga.intersection(gb))
            if area > OVERLAP_SLIVER_KM2:
                if (zones[a]["cfg"].get("grade_band")
                        and zones[b]["cfg"].get("grade_band")
                        and zones[a]["cfg"]["district"]
                        == zones[b]["cfg"]["district"]):
                    # Grade-banded campuses (e.g. PK-2 primary + 3-5 elem)
                    # legitimately serve the same territory.
                    continue
                if frozenset({a, b}) in ALLOWED_OVERLAPS:
                    print(f"  known {a} ∩ {b} = {area:.3f} km² (adjudicated: "
                          f"hand-drawn source)")
                else:
                    print(f"  FAIL {a} ∩ {b} = {area:.3f} km²")
                    bad += 1
    print(f"  {'ok — no overlaps above sliver threshold' if not bad else str(bad) + ' failing pair(s)'}")
    return bad


def check_coverage(zones) -> None:
    print("\n== 2. COVERAGE (multi-zone districts vs TIGER district) ==")
    by_district: dict[str, list] = {}
    for name, z in zones.items():
        geoid = z["cfg"].get("district_geoid")
        if geoid:
            by_district.setdefault(geoid, []).append((name, z))
    for geoid, members in sorted(by_district.items()):
        levels = {m[1]["level"] for m in members}
        for level in levels:
            union = unary_union([m[1]["geom"] for m in members
                                 if m[1]["level"] == level])
            district = _district(geoid)
            hole = district.difference(union)
            a = km2(hole)
            dist_name = members[0][1]["cfg"]["district"]
            if a > COVERAGE_NOTE_KM2:
                print(f"  NOTE {dist_name} [{level}]: {a:.1f} km² of district "
                      f"not covered by its zones — missing zone or carve-out?")
            else:
                print(f"  ok   {dist_name} [{level}]: fully tiled "
                      f"(residual {a:.2f} km²)")


_district_cache: dict[str, object] = {}


def _district(geoid: str):
    if geoid not in _district_cache:
        import urllib.parse
        p = urllib.parse.urlencode({
            "where": f"GEOID='{geoid}'", "outFields": "GEOID",
            "outSR": 4326, "f": "geojson"})
        url = ("https://tigerweb.geo.census.gov/arcgis/rest/services/"
               f"TIGERweb/School/MapServer/0/query?{p}")
        req = urllib.request.Request(url, headers={"User-Agent": "home-finder/0.1"})
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.loads(r.read().decode())
        _district_cache[geoid] = unary_union(
            [shape(f["geometry"]) for f in d["features"]])
    return _district_cache[geoid]


def check_contains(zones) -> int:
    print("\n== 3. CONTAINS (zone must contain its own campus point) ==")
    bad = checked = 0
    for name, z in zones.items():
        f = SCHOOLS / f"{name}.school.json"
        if not f.exists():
            continue
        sch = json.loads(f.read_text(encoding="utf-8"))
        lat, lon = sch.get("latitude"), sch.get("longitude")
        if not lat or not lon:
            continue
        checked += 1
        if not z["geom"].contains(Point(float(lon), float(lat))):
            if name in ALLOWED_OUTSIDE:
                print(f"  known {name}: {ALLOWED_OUTSIDE[name]}")
            else:
                print(f"  FAIL {name}: campus {sch.get('campus_name')} at "
                      f"({lat}, {lon}) is OUTSIDE its zone — wrong layer/name?")
                bad += 1
    print(f"  {checked} campuses checked; "
          f"{'all inside their zones' if not bad else str(bad) + ' FAILURES'}")
    return bad


def check_roster(zones) -> None:
    print("\n== 4. ROSTER (TEA campuses vs registered zones, per district) ==")
    req = urllib.request.Request("https://txschools.gov/data/schools.json",
                                 headers={"User-Agent": "home-finder/0.1",
                                          "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
    rows = json.loads(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)

    def norm(s): return "".join(c for c in (s or "").lower()
                                if c.isalnum() or c == " ").strip()
    def level_of(r):
        ct = r.get("campus_type") or ""
        if ct == "Elementary":
            return "elementary"
        if "Middle" in ct or ct == "Junior High":
            return "middle"
        if r.get("high_grade_cd") == "12":
            return "high"
        # campus_type is BLANK for many TEA rows (e.g. every Hutto MS/ES) —
        # fall back to the grade span so those campuses aren't silently
        # skipped. EE/PK/KG sort before "01" as strings, handled explicitly.
        lo, hi = r.get("low_grade_cd") or "", r.get("high_grade_cd") or ""
        if hi in ("", "EE", "PK", "KG"):
            return "elementary" if hi else None
        if not hi.isdigit():
            return None
        if int(hi) <= 6:
            return "elementary"
        if int(hi) <= 8 and lo.isdigit() and int(lo) >= 5:
            return "middle"
        return None   # 9th-grade centers etc. belong to the HS zone
    # Only audit levels for districts where we actually built that level
    # (sub-level sources exist for 8 districts so far).
    built = {(norm(z["cfg"]["district"]), z["level"]) for z in zones.values()}
    matched_ids = set()
    for name in zones:
        f = SCHOOLS / f"{name}.school.json"
        if f.exists():
            matched_ids.add(json.loads(f.read_text(encoding="utf-8"))
                            .get("campus_id"))
    findings = 0
    for r in rows:
        lvl = level_of(r)
        if lvl is None or (norm(r.get("district_name")), lvl) not in built:
            continue
        if (r.get("entity_cd") != "C"
                or r.get("alt_standards") == "Yes"
                or r.get("online_school") == "Yes"
                or r.get("enrollment_type") == "Special Assignment School"
                or r.get("id") in matched_ids):
            continue
        print(f"  NOTE {r.get('district_name')} [{lvl}]: campus "
              f"'{r.get('name')}' [{r['id']}] has no registered zone "
              f"(magnet/choice/new — adjudicate)")
        findings += 1
    if not findings:
        print("  ok — every zoned-level campus in covered districts is "
              "either zoned or excluded")


def main() -> int:
    zones = load_zones()
    print(f">> auditing {len(zones)} zones")
    overlap_fails = check_overlaps(zones)
    check_coverage(zones)
    contains_fails = check_contains(zones)
    check_roster(zones)
    print(f"\n== RESULT: {'PASS' if not (overlap_fails or contains_fails) else 'FAIL'} "
          f"(overlaps={overlap_fails}, containment={contains_fails}; "
          f"coverage/roster notes are judgment calls)")
    return 1 if (overlap_fails or contains_fails) else 0


if __name__ == "__main__":
    sys.exit(main())
