"""fetch_tea.py — TEA A-F accountability ratings per zone campus.

Source: txschools.gov/data/schools.json — the static dataset behind the
state's official Texas School Report Cards site (TEA). One record per campus
with the current accountability year's overall rating/score and domain
grades: D1 Student Achievement (D1a STAAR, D1b CCMR college/career/military
readiness, D1c graduation), D2 School Progress (growth), D3 Closing the Gaps.

Campus matching: zones.toml district + high-school name against TEA campus
records (grade-12 campuses in the same district), with explicit overrides for
TEA's formal names (e.g. "Jack C Hays HS"). Unmatched zones are reported
loudly, never guessed.

Output: data/schools/<zone>.school.json (committed — snapshot the viewer
reads; ratings vintage travels in as_of/source).
"""
from __future__ import annotations

import gzip
import json
import sys
import tomllib
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "schools"
URL = "https://txschools.gov/data/schools.json"

# Zone key -> exact TEA campus name, where token matching would misfire.
# Overrides bypass the level/grade filters (e.g. Legacy Ranch HS carries
# high_grade 09 while it grows a grade per year).
OVERRIDES = {
    "hayscisd-hays": "Jack C Hays HS",
    "hayscisd-johnson": "Moe and Gene Johnson HS",
    "johnsoncity-johnsoncity": "Lyndon B Johnson HS",
    "austin-travis-heights-es-es": "Travis Hts Elem",
    "austin-norman-sims-es-es": "Norman-Sims Elem",
    "austin-s-nchez-es-es": "Sanchez Elem",
    "pflugerville-spring-hill-elementary-es": "Springhill Elem",
    "libertyhill-legacy-ranch": "Legacy Ranch HS",
}


def norm(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    # hyphens split words ('Norman-Sims' -> 'norman sims')
    s = s.replace("-", " ")
    return " ".join("".join(c for c in s.lower()
                            if c.isalnum() or c == " ").split())


def fetch_schools() -> list[dict]:
    req = urllib.request.Request(URL, headers={"User-Agent": "home-finder/0.1",
                                               "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw)


# Suffix noise stripped before name matching, per level.
_SUFFIXES = {"school", "elementary", "middle", "high", "hs", "ms", "es",
             "elem", "attendance", "zone", "echs", "int"}


def _base(name: str) -> str:
    words = [w for w in norm(name).split() if w not in _SUFFIXES]
    return " ".join(words)


def _level_filter(r: dict, level: str) -> bool:
    ct = r.get("campus_type") or ""
    if level == "middle":
        return "Middle" in ct or ct == "Junior High"
    if level == "elementary":
        return ct == "Elementary"
    return r.get("high_grade_cd") == "12"   # high (default)


def match_campus(zone_key: str, hs: str, district: str,
                 rows: list[dict], level: str = "high") -> dict | None:
    cands = [r for r in rows
             if norm(r.get("district_name")) == norm(district)
             and _level_filter(r, level)
             and r.get("entity_cd") == "C"
             # Exclude alternative-standards/online campuses: without this,
             # 'Travis ECHS' matched 'Travis County JJAEP' (caught by the
             # audit's zone-contains-campus check).
             and r.get("alt_standards") != "Yes"
             and r.get("online_school") != "Yes"
             # JJAEP/DAEP campuses carry alt_standards=No but are Special
             # Assignment (Travis County JJAEP, 7 students, nearly hijacked
             # the Travis ECHS zone).
             and r.get("enrollment_type") != "Special Assignment School"]
    want = OVERRIDES.get(zone_key)
    if want:
        # Search the district unfiltered by level/grade (new campuses grow
        # a grade per year and would fail the grade-12 filter).
        for r in rows:
            if norm(r.get("district_name")) == norm(district) and \
               r.get("entity_cd") == "C" and norm(r["name"]) == norm(want):
                return r
        return None
    # Suffix-stripped prefix match: 'Oak Hill' matches 'Oak Hill Elem' but
    # not 'Oak Springs Elem' (first-token matching would hit both).
    base = _base(hs)
    hits = [r for r in cands if _base(r["name"]) == base]
    if not hits:
        hits = [r for r in cands if _base(r["name"]).startswith(base)]
    if not hits:
        # TEA formal names wrap the common name ('Connally' -> 'John B
        # Connally HS'); fall back to whole-token containment.
        toks = set(base.split())
        hits = [r for r in cands if toks <= set(_base(r["name"]).split())]
    if len(hits) == 1:
        return hits[0]
    # Disambiguate multi-hits by largest enrollment — the zoned comprehensive
    # campus is always the big one (a name-length tie-break once picked a
    # 7-student JJAEP over a 1,010-student ECHS).
    return max(hits, key=lambda r: r.get("enrollment") or 0) if hits else None


def grade(r: dict, g: str, s: str) -> dict:
    return {"grade": r.get(g) or None, "score": r.get(s) if r.get(s) != "" else None}


def fetch_districts() -> list[dict]:
    req = urllib.request.Request("https://txschools.gov/data/districts.json",
                                 headers={"User-Agent": "home-finder/0.1",
                                          "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw)


def match_district(district: str, drows: list[dict]) -> dict | None:
    for r in drows:
        if norm(r.get("district_name") or r.get("name")) == norm(district):
            return r
    return None


def main() -> int:
    rows = fetch_schools()
    drows = fetch_districts()
    print(f">> TEA dataset: {len(rows)} campuses, {len(drows)} districts")
    cfg = tomllib.loads((ROOT / "zones.toml").read_text(encoding="utf-8"))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    unmatched = []
    for zone_key, z in cfg.items():
        level = z.get("level", "high")
        r = (match_district(z["district"], drows) if level == "district"
             else match_campus(zone_key, z["high_school"], z["district"],
                               rows, level))
        if r is None:
            # Absence of data must be explained, never silent: zones with a
            # configured tea_note get a stub artifact carrying the reason.
            note = z.get("tea_note")
            if note:
                (OUT_DIR / f"{zone_key}.school.json").write_text(
                    json.dumps({"rating": None, "note": note,
                                "as_of": date.today().isoformat()},
                               indent=1, sort_keys=True) + "\n",
                    encoding="utf-8", newline="\n")
                print(f">> {zone_key}: no TEA campus — noted: {note}")
            else:
                unmatched.append(f"{zone_key} ({z['high_school']}, {z['district']})")
            continue
        distinctions = r.get("distinctions") or []
        view = "district" if level == "district" else "school"
        payload = {
            "campus_id": r["id"],
            "campus_name": r.get("name") or r.get("district_name"),
            "district": r.get("district_name"),
            "rating": r.get("rating") or None,
            "score": r.get("score") if r.get("score") != "" else None,
            "achievement": grade(r, "d1g", "d1s"),
            "staar_score": r.get("d1as") or None,
            "ccmr_score": r.get("d1bs") or None,
            "graduation_score": r.get("d1cs") or None,
            "growth": grade(r, "d2g", "d2s"),
            "closing_gaps": grade(r, "d3g", "d3s"),
            "enrollment": r.get("enrollment"),
            # Campus coordinates power the audit's zone-contains-its-school
            # check (the scalable replacement for per-zone eyeballing).
            "latitude": r.get("latitude"),
            "longitude": r.get("longitude"),
            "distinctions_earned": sum(1 for d in distinctions if d == "Y"),
            "report_url": (f"https://txschools.gov/?view={view}&id={r['id']}"
                           f"&lng=en"),
            "as_of": date.today().isoformat(),
            "source": "TEA Texas School Report Cards (txschools.gov), current "
                      "accountability year at fetch time",
        }
        (OUT_DIR / f"{zone_key}.school.json").write_text(
            json.dumps(payload, indent=1, sort_keys=True) + "\n",
            encoding="utf-8", newline="\n")
        print(f">> {zone_key}: {r['name']} [{r['id']}] rating={payload['rating']}")
    if unmatched:
        print(f">> UNMATCHED ({len(unmatched)}): " + "; ".join(unmatched),
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
