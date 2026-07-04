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
    "legal_acreage": (1660, 1675),
    "appraised_val": (1916, 1930),
    "assessed_val":  (1946, 1960),
    "deed_dt":       (2034, 2058),
    "hs_exempt":     (2609, 2609),
    "imprv_state_cd": (2732, 2741),
    "land_state_cd": (2742, 2751),
    "market_value":  (4214, 4227),
    "situs_num":     (4460, 4474),
}

# IMP_DET.TXT / IMP_ATR.TXT offsets (same layout doc, lengths accumulated).
IMPDET_FIELDS = {"prop_id": (1, 12), "type_desc": (51, 75),
                 "yr_built": (86, 89), "area": (94, 108)}
IMPATR_FIELDS = {"prop_id": (1, 12), "attr_desc": (53, 77), "attr_cd": (78, 87)}
TCAD_ATTRS = {"Foundation": "foundation", "Roof Covering": "roof",
              "Roof Style": "roof_style"}


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

    # Values come from the Property export dataset (the parcels dataset's
    # value columns are published zeroed). Same rule as Hays: real (R-prefix)
    # improved (improvement value > 0) property only.
    WCAD_PROPERTY = "https://data.wcad.org/resource/ij43-xknu.json"
    recs: dict[str, dict] = {}
    skipped_type = 0
    ids = sorted(deeds)
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        in_list = ",".join(f"'{x}'" for x in chunk)
        q = urllib.parse.urlencode({
            "$select": "propertyid,quickrefid,currmarketvalue,"
                       "currassessedvalue,currimprovmentvalue,situs",
            "$where": f"propertyid in({in_list})",
            "$limit": BATCH,
        })
        for p in _get_json(f"{WCAD_PROPERTY}?{q}"):
            if not (p.get("quickrefid") or "").startswith("R") or \
               not float(p.get("currimprovmentvalue") or 0):
                skipped_type += 1
                continue
            pid = p["propertyid"]
            mv = p.get("currmarketvalue")
            av = p.get("currassessedvalue")
            recs[pid] = {
                "address": (p.get("situs") or "").strip(),
                "deed_date": deeds.get(pid),
                "value": int(float(mv)) if mv else None,
                "assessed": int(float(av)) if av else None,
                "value_kind": "market (WCAD)",
            }
        if (i // BATCH) % 40 == 0:
            print(f">> WCAD values joined: {len(recs)}/{len(deeds)}", flush=True)
    print(f">> WCAD: {len(recs)} improved-residential transfers "
          f"({skipped_type} mobile/personal/unimproved skipped)", flush=True)

    # Segment (MA = main area) -> year built, living sqft, construction
    # extras. Enriches `recs` BEFORE the geometry pass copies them out.
    WCAD_SEGMENT = "https://data.wcad.org/resource/4kxj-e8c3.json"
    ids = sorted(recs)
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        in_list = ",".join(f"'{x}'" for x in chunk)
        q = urllib.parse.urlencode({
            "$select": "propertyid,type,actyrbuilt,area,roof,foundation,extfinish",
            "$where": f"propertyid in({in_list}) AND type = 'MA'",
            "$limit": BATCH * 4,
        })
        for s in _get_json(f"{WCAD_SEGMENT}?{q}"):
            rec = recs.get(s.get("propertyid", ""))
            if not rec:
                continue
            if s.get("area"):
                rec["sqft"] = int(rec.get("sqft") or 0) + int(float(s["area"]))
            if s.get("actyrbuilt") and float(s["actyrbuilt"]) > 1800:
                rec["year_built"] = min(rec.get("year_built") or 9999,
                                        int(float(s["actyrbuilt"])))
            extras = rec.setdefault("extras", {})
            for src, dst in (("roof", "roof"), ("foundation", "foundation"),
                             ("extfinish", "exterior")):
                if s.get(src):
                    extras.setdefault(dst, s[src])
        if (i // BATCH) % 40 == 0:
            print(f">> WCAD segments joined: {i}/{len(ids)}", flush=True)

    # Land dataset -> state class; Exemptions dataset -> homestead flag.
    WCAD_LAND = "https://data.wcad.org/resource/2ckt-cqwj.json"
    WCAD_EXEMPT = "https://data.wcad.org/resource/nbn7-h4pp.json"
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        in_list = ",".join(f"'{x}'" for x in chunk)
        q = urllib.parse.urlencode({
            "$select": "propertyid,statecode",
            "$where": f"propertyid in({in_list})", "$limit": BATCH * 4})
        for s in _get_json(f"{WCAD_LAND}?{q}"):
            rec = recs.get(s.get("propertyid", ""))
            if rec and s.get("statecode"):
                rec.setdefault("class", s["statecode"])
        q2 = urllib.parse.urlencode({
            "$select": "propertyid,exemptiontypedescription,exemptionstatuscode",
            "$where": f"propertyid in({in_list})", "$limit": BATCH * 6})
        for e in _get_json(f"{WCAD_EXEMPT}?{q2}"):
            rec = recs.get(e.get("propertyid", ""))
            if rec and e.get("exemptionstatuscode") == "A" and \
               "homestead" in (e.get("exemptiontypedescription") or "").lower():
                rec["homestead"] = True
        if (i // BATCH) % 40 == 0:
            print(f">> WCAD class/exemptions joined: {i}/{len(ids)}", flush=True)
    for rec in recs.values():
        rec.setdefault("homestead", False)

    from pyproj import Geod
    _geod = Geod(ellps="WGS84")
    out: list[dict] = []
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        in_list = ",".join(f"'{x}'" for x in chunk)
        q = urllib.parse.urlencode({
            "$select": "propertyid,geometry",
            "$where": f"propertyid in({in_list})",
            "$limit": BATCH,
        })
        for p in _get_json(f"{WCAD_PARCELS}?{q}"):
            geom = p.get("geometry")
            rec = recs.get(p.get("propertyid", ""))
            if not geom or not rec:
                continue
            g = shape(geom)
            c = g.centroid
            # Acres from the parcel polygon itself (no acreage column is
            # published); geodesic area, EPSG:4326 in.
            a, _ = _geod.geometry_area_perimeter(g)
            out.append({
                "lon": round(c.x, 6), "lat": round(c.y, 6), **rec,
                "acres": round(abs(a) * 0.000247105, 3) or None,
            })
        if (i // BATCH) % 40 == 0:
            print(f">> WCAD geometry joined: {len(out)}/{len(recs)}", flush=True)
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
            # Future-dated deeds are CAD entry errors — they'd inflate every
            # rate window, so drop them.
            if deed is None or deed < cut or deed > date.today():
                continue
            # Normalize: the roll zero-pads prop_id ('000000100008') but the
            # GIS layer returns integers — key both sides unpadded.
            pid = _slice(raw, "prop_id").lstrip("0")
            mv = _slice(raw, "market_value")
            av = _slice(raw, "assessed_val")
            acres = _slice(raw, "legal_acreage")
            out[pid] = {
                "deed_date": deed.isoformat(),
                "value": int(mv) if mv.isdigit() else None,
                "assessed": int(av) if av.isdigit() else None,
                # legal_acreage carries 3 implied decimals (6456 = 6.456 ac).
                "acres": round(float(acres) / 1000, 3) if acres and
                         acres.replace(".", "").isdigit() and float(acres) else None,
                "value_kind": "market (TCAD appraisal)",
                # State property classification (A1 single-family, C1 vacant,
                # F1 commercial...) and the homestead-exemption flag.
                "class": _slice(raw, "imprv_state_cd") or
                         _slice(raw, "land_state_cd") or None,
                "homestead": _slice(raw, "hs_exempt").upper() in ("T", "Y", "1"),
                "address": " ".join(x for x in (
                    _slice(raw, "situs_num"), _slice(raw, "situs_street"),
                    _slice(raw, "situs_city"), _slice(raw, "situs_zip")) if x),
            }
    print(f">> TCAD roll: scanned {scanned} rows, "
          f"{len(out)} recent real-property transfers", flush=True)
    _enrich_travis(out)
    return out


def _fw(line: str, fields: dict, name: str) -> str:
    a, b = fields[name]
    return line[a - 1:b].strip()


def _enrich_travis(transfers: dict[str, dict]) -> None:
    """Stream IMP_DET (living sqft = summed floor areas, year built) and
    IMP_ATR (foundation/roof) for just the transferred prop_ids."""
    z = zipfile.ZipFile(TCAD_ROLL)
    pids = set(transfers)
    print(">> TCAD: enriching from IMP_DET (2GB stream) ...", flush=True)
    with z.open("IMP_DET.TXT") as fh:
        for raw in io.TextIOWrapper(fh, encoding="latin-1", errors="replace"):
            pid = _fw(raw, IMPDET_FIELDS, "prop_id").lstrip("0")
            if pid not in pids:
                continue
            rec = transfers[pid]
            desc = _fw(raw, IMPDET_FIELDS, "type_desc").lower()
            if "floor" in desc:
                area = _fw(raw, IMPDET_FIELDS, "area")
                try:
                    rec["sqft"] = int(rec.get("sqft") or 0) + int(float(area))
                except ValueError:
                    pass
                yr = _fw(raw, IMPDET_FIELDS, "yr_built")
                if yr.isdigit() and int(yr) > 1800:
                    rec["year_built"] = min(rec.get("year_built") or 9999, int(yr))
    print(">> TCAD: enriching from IMP_ATR ...", flush=True)
    with z.open("IMP_ATR.TXT") as fh:
        for raw in io.TextIOWrapper(fh, encoding="latin-1", errors="replace"):
            pid = _fw(raw, IMPATR_FIELDS, "prop_id").lstrip("0")
            if pid not in pids:
                continue
            key = TCAD_ATTRS.get(_fw(raw, IMPATR_FIELDS, "attr_desc"))
            if key:
                extras = transfers[pid].setdefault("extras", {})
                extras.setdefault(key, _fw(raw, IMPATR_FIELDS, "attr_cd"))


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
# Hays — CSV export (no coordinates) + 911 address-point join
# ---------------------------------------------------------------------------

HAYS_INNER = ROOT / "data" / "census" / "hays-inner"
HAYS_ADDRESS_POINTS = ("https://services5.arcgis.com/bVphnK8rPe5MHUSr/arcgis/"
                       "rest/services/Address_Points/FeatureServer/0")
DSISD_ENTITY = "SDS"   # Dripping Springs ISD taxing-entity code in Hays CAD


def _addr_key(num: str, street: str) -> tuple[str, str] | None:
    """Join key: (street number, first street-name token), normalized."""
    num = (num or "").strip().lstrip("0")
    tok = (street or "").strip().upper().split()
    return (num, tok[0]) if num and tok else None


def _hays_table(kind: str) -> Path:
    """Locate an extracted export table by sniffing its header."""
    for p in sorted(HAYS_INNER.glob("*.txt")):
        with open(p, encoding="latin-1") as f:
            header = f.readline()
        if kind == "sales" and '"SaleDate","DeedDate"' in header:
            return p
        if kind == "property" and '"TaxingUnitList"' in header and '"Situs"' in header:
            return p
        if kind == "segment" and '"ActYrBuilt"' in header and '"Bedrooms"' in header:
            return p
        if kind == "land" and '"LandType"' in header and '"Acres"' in header:
            return p
        if kind == "owner" and '"ExemptionList"' in header and '"OwnerName"' in header:
            return p
    raise FileNotFoundError(f"Hays {kind} table not found in {HAYS_INNER} — "
                            f"download + extract the Hays CAD export first")


def fetch_hays() -> list[dict]:
    """County-wide Hays transfers. Pin locations come from an address join
    against the county 911 address points (match rate printed — unmatched
    transfers are dropped from pins; zone stats still count matched only)."""
    import csv
    cut = cutoff_date().isoformat()

    deeds: dict[str, str] = {}
    with open(_hays_table("sales"), encoding="latin-1") as f:
        for row in csv.DictReader(f):
            raw = (row.get("DeedDate") or "").split()
            if not raw:
                continue
            try:
                d = datetime.strptime(raw[0], "%m/%d/%Y").date().isoformat()
            except ValueError:
                continue
            pid = row["PropertyID"]
            if d >= cut and d >= deeds.get(pid, ""):
                deeds[pid] = d
    print(f">> Hays: {len(deeds)} properties with deeds since {cut}", flush=True)

    recs: dict[tuple, dict] = {}
    pid_to_rec: dict[str, dict] = {}
    unmatched = 0
    skipped_type = 0
    with open(_hays_table("property"), encoding="latin-1") as f:
        for row in csv.DictReader(f):
            pid = row["PropertyID"]
            if pid not in deeds:
                continue
            # Real, improved property only: QuickRefID prefix R = real (M =
            # mobile home, P = personal), and SquareFootage>0 excludes vacant
            # builder lots whose land-only values swamp growth-zone medians.
            if not (row.get("QuickRefID") or "").startswith("R") or \
               not float(row.get("SquareFootage") or 0):
                skipped_type += 1
                continue
            key = _addr_key(row.get("SitusStreetNumber"),
                            row.get("SitusStreetName"))
            mv = row.get("CurrMarketValue") or ""
            av = row.get("CurrAssessedValue") or ""
            sqft = row.get("SquareFootage") or ""
            rec = {
                "address": (row.get("Situs") or "").strip(),
                "deed_date": deeds[pid],
                "value": int(float(mv)) if mv else None,
                "assessed": int(float(av)) if av else None,
                "value_kind": "market (Hays CAD)",
                "sqft": int(float(sqft)) if sqft else None,
            }
            pid_to_rec[pid] = rec
            if key:
                recs[key] = rec
            else:
                unmatched += 1
    print(f">> Hays: {len(recs)} improved-residential transfers to geolocate "
          f"({unmatched} unusable situs; {skipped_type} mobile/personal/"
          f"unimproved skipped)", flush=True)

    # SEGMENT (MA rows): year built + the construction/bedroom extras — Hays
    # is the only county publishing bedrooms/fireplace/HVAC.
    with open(_hays_table("segment"), encoding="latin-1") as f:
        for row in csv.DictReader(f):
            rec = pid_to_rec.get(row.get("PropertyID", ""))
            if not rec or row.get("Type") != "MA":
                continue
            yr = row.get("ActYrBuilt") or ""
            if yr and float(yr) > 1800:
                rec["year_built"] = min(rec.get("year_built") or 9999,
                                        int(float(yr)))
            extras = rec.setdefault("extras", {})
            for src, dst in (("Bedrooms", "bedrooms"), ("Fireplace", "fireplace"),
                             ("HeatAC", "heat_ac"), ("Roof", "roof"),
                             ("Foundation", "foundation"),
                             ("ExtFinish", "exterior")):
                if row.get(src):
                    extras.setdefault(dst, row[src])
    # LAND: lot acres (summed) + state class.
    with open(_hays_table("land"), encoding="latin-1") as f:
        for row in csv.DictReader(f):
            rec = pid_to_rec.get(row.get("PropertyID", ""))
            if not rec:
                continue
            acres = row.get("Acres") or ""
            if acres:
                rec["acres"] = round((rec.get("acres") or 0) + float(acres), 3)
            if row.get("StateCode"):
                rec.setdefault("class", row["StateCode"])
    # OWNER: homestead flag from the exemption list (HS code).
    with open(_hays_table("owner"), encoding="latin-1") as f:
        for row in csv.DictReader(f):
            rec = pid_to_rec.get(row.get("PropertyID", ""))
            if rec is not None:
                codes = (row.get("ExemptionList") or "").upper()
                rec["homestead"] = rec.get("homestead", False) or \
                    any(c.strip() == "HS" for c in codes.split(","))
    for rec in pid_to_rec.values():
        rec.setdefault("homestead", False)

    # County-wide 911 address points (paginated).
    points: dict[tuple, tuple[float, float]] = {}
    offset = 0
    while True:
        params = urllib.parse.urlencode({
            "where": "1=1", "outFields": "ADDR_NUM,FULL_NAME",
            "returnGeometry": "true", "outSR": 4326,
            "resultOffset": offset, "f": "geojson",
        })
        d = _get_json(f"{HAYS_ADDRESS_POINTS}/query?{params}")
        feats = d.get("features", [])
        for f in feats:
            p = f.get("properties", {})
            key = _addr_key(str(p.get("ADDR_NUM") or ""), p.get("FULL_NAME"))
            if key and f.get("geometry"):
                points.setdefault(key, tuple(f["geometry"]["coordinates"][:2]))
        if len(feats) < 2000:
            break
        offset += len(feats)
        if offset % 20000 == 0:
            print(f">> Hays: {offset} address points scanned ...", flush=True)
    print(f">> Hays: {len(points)} 911 address points county-wide", flush=True)

    out = []
    misses = 0
    for key, rec in recs.items():
        pt = points.get(key)
        if pt is None:
            misses += 1
            continue
        out.append({"lon": round(pt[0], 6), "lat": round(pt[1], 6), **rec})
    total = len(recs)
    if total:
        print(f">> Hays: matched {len(out)}/{total} "
              f"({len(out) / total:.0%}) transfers to address points", flush=True)
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
    counties = counties or ["williamson", "travis", "hays"]
    all_transfers: list[dict] = []
    for county, fetch in (("williamson", fetch_williamson),
                          ("travis", fetch_travis),
                          ("hays", fetch_hays)):
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
    # Universal guard: future-dated deeds are CAD entry errors.
    today = date.today().isoformat()
    n_future = sum(1 for t in all_transfers if (t.get("deed_date") or "") > today)
    if n_future:
        print(f">> dropping {n_future} future-dated deeds (CAD entry errors)",
              flush=True)
        all_transfers = [t for t in all_transfers
                         if (t.get("deed_date") or "") <= today]
    print(f">> total transfers with location: {len(all_transfers)}", flush=True)

    zones = {}
    for bf in sorted(BOUNDARIES.glob("*.geojson")):
        fc = json.loads(bf.read_text(encoding="utf-8"))
        zones[bf.stem] = prep(shape(fc["features"][0]["geometry"]))

    from shapely.geometry import Point
    per_zone: dict[str, list[dict]] = {name: [] for name in zones}
    outside: list[dict] = []
    for t in all_transfers:
        pt = Point(t["lon"], t["lat"])
        hit = False
        for name, geom in zones.items():
            if geom.contains(pt):
                per_zone[name].append(t)
                hit = True
        if not hit:
            outside.append(t)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    joined_dir = ROOT / "data" / "joined"
    for name, transfers in per_zone.items():
        transfers.sort(key=lambda t: t.get("deed_date") or "", reverse=True)
        # Transfer-rate windows vs ACS population — derived stats belong in
        # the build step, committed, never computed in the viewer.
        pop = None
        demo_file = joined_dir / f"{name}.demographics.json"
        if demo_file.exists():
            demo = json.loads(demo_file.read_text(encoding="utf-8"))
            pop = (demo.get("race_ethnicity", {}).get("counts", {})
                   .get("B03002_001", {}).get("value"))
        windows = {}
        for label, days in (("3mo", 91), ("6mo", 182), ("12mo", 365)):
            since = (date.today() - timedelta(days=days)).isoformat()
            in_window = [t for t in transfers
                         if (t.get("deed_date") or "") >= since]
            # Median CAD value of transferred homes — the honest "average
            # home price" proxy (appraisal, not price; TX non-disclosure).
            # WCAD + Hays are improved-residential at ingest; Travis includes
            # all real property (roll lacks a cheap improved flag — caveat).
            vals = sorted(t["value"] for t in in_window if t.get("value"))
            median_value = vals[len(vals) // 2] if vals else None
            windows[label] = {
                "count": len(in_window),
                "pct_of_population": round(len(in_window) / pop * 100, 2) if pop else None,
                "median_value": median_value,
            }
        payload = {
            "as_of": stamp,
            "months_back": MONTHS_BACK,
            "disclaimer": ("Deed-transfer dates + CAD appraised/assessed "
                           "values. NOT sale prices — Texas is a "
                           "non-disclosure state."),
            "count": len(transfers),
            "population_acs": round(pop) if pop else None,
            "windows": windows,
            "transfers": transfers,
        }
        (OUT_DIR / f"{name}.transfers.json").write_text(
            json.dumps(payload, indent=1, sort_keys=True) + "\n",
            encoding="utf-8", newline="\n")
        print(f">> {name}: {len(transfers)} transfers")

    # County-wide remainder — everything located but outside every registered
    # zone, so the map can show full tri-county coverage on demand.
    outside.sort(key=lambda t: t.get("deed_date") or "", reverse=True)
    (OUT_DIR / "_outside.transfers.json").write_text(
        json.dumps({
            "as_of": stamp, "months_back": MONTHS_BACK,
            "disclaimer": ("Transfers outside all registered zones (rest of "
                           "Travis/Williamson/Hays). CAD values, not prices."),
            "count": len(outside), "transfers": outside,
        }, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    print(f">> _outside: {len(outside)} transfers (tri-county remainder)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or None))
