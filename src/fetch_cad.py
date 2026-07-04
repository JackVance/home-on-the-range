"""fetch_cad.py — recent deed transfers per zone, from county appraisal districts.

HONESTY FIRST: Texas is a non-disclosure state. Actual sale prices are NOT
public record and are NOT in this data. What this layer shows per parcel:
the DEED TRANSFER date (ownership changed) and the CAD's APPRAISED/MARKET
value — good for area comparison and market-activity signal, not a price.
The viewer must label it that way.

Sources (both free, official, probed 2026-07-04):
  - Williamson: WCAD's Socrata portal (data.wcad.org) —
      "Recent Sales - Preliminary" (wdp6-f3zg): propertyid + deeddate,
      "Parcels" (an3x-cnmw): GeoJSON geometry + values, joined by propertyid.
  - Travis: TCAD bulk appraisal-roll export (fixed-width APPRAISAL_INFO.TXT,
      layout Legacy8.0.32) for deed_dt + market_value per prop_id, joined to
      parcel geometry from the public TCAD_public MapServer in ID batches.

Output: data/cad/<zone>.transfers.json (committed — snapshot the viewer reads).
"""
from __future__ import annotations

import io
import json
import sys
import urllib.parse
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from shapely.geometry import shape
from shapely.prepared import prep

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "cad"
BOUNDARIES = ROOT / "data" / "boundaries"
TCAD_ROLL = ROOT / "data" / "census" / "tcad-roll-2026.zip"

MONTHS_BACK = 12          # transfer window
BATCH = 100               # ids per join query

WCAD_SALES = "https://data.wcad.org/resource/wdp6-f3zg.json"
WCAD_PARCELS = "https://data.wcad.org/resource/an3x-cnmw.json"
TCAD_PARCELS = ("https://gis.traviscountytx.gov/server1/rest/services/"
                "Boundaries_and_Jurisdictions/TCAD_public/MapServer/0")

# Fixed-width offsets from TP_Legacy8.0.32-AppraisalExportLayout.xlsx,
# "Property" sheet (1-indexed inclusive -> python slices). Version-locked to
# the Legacy 8.0.32 export format; re-derive from the layout doc if TCAD bumps.
TCAD_FIELDS = {
    "prop_id":       (1, 12),
    "prop_type_cd":  (13, 17),
    "py_owner_name": (609, 678),
    "situs_street":  (1050, 1099),
    "situs_city":    (1110, 1139),
    "situs_zip":     (1140, 1149),
    "appraised_val": (1916, 1930),
    "assessed_val":  (1946, 1960),
    "deed_dt":       (2034, 2058),
    "market_value":  (4214, 4227),
    "situs_num":     (4460, 4474),
}


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "home-finder/0.1"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.read()


def _get_json(url: str):
    return json.loads(_get(url).decode("utf-8"))


def cutoff_date() -> date:
    return date.today() - timedelta(days=MONTHS_BACK * 30)


# ---------------------------------------------------------------------------
# Williamson — all-API
# ---------------------------------------------------------------------------

def fetch_williamson() -> list[dict]:
    """-> [{lon, lat, address, deed_date, market_value, year_built, sqft}]"""
    cut = cutoff_date().isoformat()
    q = urllib.parse.urlencode({
        "$select": "propertyid,deeddate",
        "$where": f"deeddate >= '{cut}T00:00:00' AND "
                  f"transferchangereasoncode = 'OWN'",
        "$limit": 100000,
    })
    rows = _get_json(f"{WCAD_SALES}?{q}")
    # Latest deed per property (a property can transfer twice in the window).
    deeds: dict[str, str] = {}
    for r in rows:
        pid, dt = r.get("propertyid"), (r.get("deeddate") or "")[:10]
        if pid and dt and dt >= deeds.get(pid, ""):
            deeds[pid] = dt
    print(f">> WCAD: {len(deeds)} properties transferred since {cut}", flush=True)

    out: list[dict] = []
    ids = sorted(deeds)
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        in_list = ",".join(f"'{x}'" for x in chunk)
        q = urllib.parse.urlencode({
            "$select": "propertyid,geometry,siteaddress,totprvtxto,"
                       "lndvalue,cntassdval,resyrblt,resflrarea,usedscrp",
            "$where": f"propertyid in({in_list})",
            "$limit": BATCH,
        })
        for p in _get_json(f"{WCAD_PARCELS}?{q}"):
            geom = p.get("geometry")
            if not geom:
                continue
            c = shape(geom).centroid
            # WCAD parcels dataset carries assessed value columns; use county
            # assessed as the value signal (market column names vary by export).
            val = p.get("cntassdval") or p.get("totprvtxto")
            out.append({
                "lon": round(c.x, 6), "lat": round(c.y, 6),
                "address": (p.get("siteaddress") or "").strip(),
                "deed_date": deeds.get(p.get("propertyid", ""), None),
                "value": int(float(val)) if val else None,
                "value_kind": "assessed (WCAD)",
                "year_built": int(float(p["resyrblt"])) if p.get("resyrblt") else None,
                "sqft": int(float(p["resflrarea"])) if p.get("resflrarea") else None,
                "use": p.get("usedscrp"),
            })
        if (i // BATCH) % 20 == 0:
            print(f">> WCAD parcels joined: {len(out)}/{len(deeds)}", flush=True)
    return out


# ---------------------------------------------------------------------------
# Travis — bulk roll + batched geometry
# ---------------------------------------------------------------------------

def _slice(line: str, field: str) -> str:
    a, b = TCAD_FIELDS[field]
    return line[a - 1:b].strip()


def parse_tcad_roll() -> dict[str, dict]:
    """PROP.TXT/APPRAISAL_INFO.TXT -> {prop_id: record} for recent transfers
    of real property."""
    cut = cutoff_date()
    if not TCAD_ROLL.exists():
        raise FileNotFoundError(f"{TCAD_ROLL} missing — download the TCAD "
                                f"appraisal export first")
    z = zipfile.ZipFile(TCAD_ROLL)
    name = next((n for n in z.namelist()
                 if "APPRAISAL_INFO" in n.upper() or n.upper().endswith("PROP.TXT")),
                None)
    if not name:
        raise RuntimeError(f"no APPRAISAL_INFO/PROP.TXT in roll; has: "
                           f"{z.namelist()[:10]}")
    print(f">> parsing {name} ...", flush=True)
    out: dict[str, dict] = {}
    scanned = 0
    with z.open(name) as fh:
        for raw in io.TextIOWrapper(fh, encoding="latin-1", errors="replace"):
            scanned += 1
            if _slice(raw, "prop_type_cd") != "R":     # real property only
                continue
            deed_raw = _slice(raw, "deed_dt")
            if not deed_raw:
                continue
            # deed_dt arrives as e.g. '2026-03-14 00:00:00' or 'MM/DD/YYYY'
            deed = None
            token = deed_raw.split()[0]
            for fmt in ("%m-%d-%Y", "%Y-%m-%d", "%m/%d/%Y"):
                try:
                    deed = datetime.strptime(token, fmt).date()
                    break
                except ValueError:
                    continue
            if deed is None or deed < cut:
                continue
            # Normalize: the roll zero-pads prop_id ('000000100008') but the
            # GIS layer returns integers — key both sides unpadded.
            pid = _slice(raw, "prop_id").lstrip("0")
            mv = _slice(raw, "market_value")
            out[pid] = {
                "deed_date": deed.isoformat(),
                "value": int(mv) if mv.isdigit() else None,
                "value_kind": "market (TCAD appraisal)",
                "address": " ".join(x for x in (
                    _slice(raw, "situs_num"), _slice(raw, "situs_street"),
                    _slice(raw, "situs_city"), _slice(raw, "situs_zip")) if x),
            }
    print(f">> TCAD roll: scanned {scanned} rows, "
          f"{len(out)} recent real-property transfers", flush=True)
    return out


def fetch_travis() -> list[dict]:
    transfers = parse_tcad_roll()
    ids = sorted(transfers)
    out: list[dict] = []
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        params = urllib.parse.urlencode({
            "where": f"PROP_ID IN ({','.join(chunk)})",
            "outFields": "PROP_ID",
            "returnGeometry": "true", "outSR": 4326,
            "geometryPrecision": 6, "f": "geojson",
        })
        d = _get_json(f"{TCAD_PARCELS}/query?{params}")
        for f in d.get("features", []):
            pid = str(f.get("properties", {}).get("PROP_ID", ""))
            rec = transfers.get(pid)
            if not rec or not f.get("geometry"):
                continue
            c = shape(f["geometry"]).centroid
            out.append({"lon": round(c.x, 6), "lat": round(c.y, 6), **rec})
        if (i // BATCH) % 20 == 0:
            print(f">> TCAD parcels joined: {len(out)}/{len(ids)}", flush=True)
    return out


# ---------------------------------------------------------------------------
# Zone assignment + output
# ---------------------------------------------------------------------------

def _county_cache(county: str) -> Path:
    return ROOT / "data" / "census" / f"cad-{county}.json"


def main(counties: list[str] | None = None) -> int:
    """Fetches the named counties (default both); un-named counties reuse
    their cache from a prior run so a single-county re-run doesn't wipe the
    other county's transfers out of the per-zone artifacts."""
    counties = counties or ["williamson", "travis"]
    all_transfers: list[dict] = []
    for county, fetch in (("williamson", fetch_williamson),
                          ("travis", fetch_travis)):
        cache = _county_cache(county)
        if county in counties:
            transfers = fetch()
            cache.write_text(json.dumps(transfers), encoding="utf-8")
        elif cache.exists():
            transfers = json.loads(cache.read_text(encoding="utf-8"))
            print(f">> {county}: {len(transfers)} transfers from cache", flush=True)
        else:
            transfers = []
            print(f">> {county}: skipped, no cache", flush=True)
        all_transfers += transfers
    print(f">> total transfers with location: {len(all_transfers)}", flush=True)

    zones = {}
    for bf in sorted(BOUNDARIES.glob("*.geojson")):
        fc = json.loads(bf.read_text(encoding="utf-8"))
        zones[bf.stem] = prep(shape(fc["features"][0]["geometry"]))

    from shapely.geometry import Point
    per_zone: dict[str, list[dict]] = {name: [] for name in zones}
    for t in all_transfers:
        pt = Point(t["lon"], t["lat"])
        for name, geom in zones.items():
            if geom.contains(pt):
                per_zone[name].append(t)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for name, transfers in per_zone.items():
        transfers.sort(key=lambda t: t.get("deed_date") or "", reverse=True)
        payload = {
            "as_of": stamp,
            "months_back": MONTHS_BACK,
            "disclaimer": ("Deed-transfer dates + CAD appraised/assessed "
                           "values. NOT sale prices — Texas is a "
                           "non-disclosure state."),
            "count": len(transfers),
            "transfers": transfers,
        }
        (OUT_DIR / f"{name}.transfers.json").write_text(
            json.dumps(payload, indent=1, sort_keys=True) + "\n",
            encoding="utf-8", newline="\n")
        print(f">> {name}: {len(transfers)} transfers")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or None))
