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
OVERRIDES = {
    "hayscisd-hays": "Jack C Hays HS",
    "hayscisd-johnson": "Moe and Gene Johnson HS",
}


def norm(s: str) -> str:
    return "".join(c for c in (s or "").lower() if c.isalnum() or c == " ").strip()


def fetch_schools() -> list[dict]:
    req = urllib.request.Request(URL, headers={"User-Agent": "home-finder/0.1",
                                               "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw)


def match_campus(zone_key: str, hs: str, district: str,
                 rows: list[dict]) -> dict | None:
    cands = [r for r in rows
             if norm(r.get("district_name")) == norm(district)
             and r.get("high_grade_cd") == "12"
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
        for r in cands:
            if norm(r["name"]) == norm(want):
                return r
        return None
    # First distinctive token of the zone's school name must appear in the
    # TEA campus name ("Eastside ECHS" -> "Eastside Early College HS").
    token = norm(hs).split()[0]
    hits = [r for r in cands if token in norm(r["name"]).split()]
    if len(hits) == 1:
        return hits[0]
    # Disambiguate multi-hits by largest enrollment — the zoned comprehensive
    # campus is always the big one (a name-length tie-break once picked a
    # 7-student JJAEP over a 1,010-student ECHS).
    hits = [r for r in hits if norm(r["name"]).startswith(token)]
    return max(hits, key=lambda r: r.get("enrollment") or 0) if hits else None


def grade(r: dict, g: str, s: str) -> dict:
    return {"grade": r.get(g) or None, "score": r.get(s) if r.get(s) != "" else None}


def main() -> int:
    rows = fetch_schools()
    print(f">> TEA dataset: {len(rows)} campuses statewide")
    cfg = tomllib.loads((ROOT / "zones.toml").read_text(encoding="utf-8"))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    unmatched = []
    for zone_key, z in cfg.items():
        r = match_campus(zone_key, z["high_school"], z["district"], rows)
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
        payload = {
            "campus_id": r["id"],
            "campus_name": r["name"],
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
            "report_url": (f"https://txschools.gov/?view=school&id={r['id']}"
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
