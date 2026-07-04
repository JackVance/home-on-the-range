"""One-off generator: emit zones.toml entries for district/middle/elementary
levels from live source name lists. Run once; output is committed TOML."""
import json
import re
import tomllib
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UA = {"User-Agent": "Mozilla/5.0"}

def get(url, params=None):
    u = f"{url}?{urllib.parse.urlencode(params)}" if params else url
    req = urllib.request.Request(u, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()

def slug(s):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", s.lower())).strip("-")

cfg = tomllib.loads((ROOT / "zones.toml").read_text(encoding="utf-8"))
# district -> (counties, geoid) from existing HS entries
dmeta = {}
for z in cfg.values():
    d = z["district"]
    geoid = z.get("district_geoid")
    if not geoid and "GEOID=" in z.get("boundary_where", ""):
        geoid = z["boundary_where"].split("'")[1]
    if d not in dmeta or (geoid and not dmeta[d][1]):
        dmeta[d] = (z["counties"], geoid)

entries = []
def add(key, district, school, level, source, url, where, name_field,
        layer_title=None, clip=True):
    counties, geoid = dmeta[district]
    e = [f"[{key}]",
         f'level             = "{level}"',
         f'district          = "{district}"',
         f'high_school       = "{school}"',
         f"counties          = {counties!r}".replace("'", '"'),
         f'boundary_source   = "{source}"']
    if clip and geoid and level != "district":
        e.append(f'district_geoid    = "{geoid}"')
    e += [f'boundary_url      = "{url}"',
          f'boundary_where    = "{where}"',
          f'boundary_name_field = "{name_field}"',
          "boundary_has_name = true",
          "expected_features = 1",
          "expected_area_km2 = 0           # bootstrap: pin from first fetch",
          f'district_effective_year = "{YEAR}"']
    entries.append("\n".join(e))

# ---- District level: every district, TIGERweb ----
YEAR = "current (TIGER district boundary)"
TW = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/School/MapServer/0"
for d, (counties, geoid) in sorted(dmeta.items()):
    if not geoid:
        print(f"!! no geoid for {d}")
        continue
    add(f"{slug(d.replace(' ISD','').replace(' CISD',''))}-district", d,
        f"{d} (district)", "district", "arcgis_query_url", TW,
        f"GEOID='{geoid}'", "NAME", clip=False)

# ---- LISD ----
YEAR = "2024-25"
LISD = "https://utility.arcgis.com/usrsvcs/servers/09a49c137df944e3b8399905ef8b3d8b/rest/services/Leander_AZ_Finder_24_25_WFL1/FeatureServer"
for lyr, level, suf in ((4, "middle", "ms"), (3, "elementary", "es")):
    d = json.loads(get(f"{LISD}/{lyr}/query", {"where": "1=1",
        "outFields": "DisplayName", "returnGeometry": "false", "f": "json"}))
    for n in sorted({f["attributes"]["DisplayName"] for f in d["features"]}):
        add(f"leander-{slug(n.replace(' School',''))}-{suf}", "Leander ISD", n,
            level, "arcgis_query_url", f"{LISD}/{lyr}",
            f"DisplayName='{n}'", "DisplayName")

# ---- RRISD ----
YEAR = "2025-26?"
RR = "https://server.metrostudygis.com/arcgis/rest/services/Round_Rock_ISD_2526/MapServer"
for lyr, level, suf, field in ((3, "middle", "ms", "MID_NAME"),
                               (4, "elementary", "es", "ELEM_NAME")):
    d = json.loads(get(f"{RR}/{lyr}/query", {"where": "1=1",
        "outFields": field, "returnGeometry": "false",
        "returnDistinctValues": "true", "f": "json"}))
    for n in sorted({(f["attributes"][field] or "").strip()
                     for f in d["features"]} - {""}):
        add(f"roundrock-{slug(n)}-{suf}", "Round Rock ISD", n.title(),
            level, "arcgis_query_url", f"{RR}/{lyr}",
            f"{field}='{n}'", field)

# ---- AISD ----
YEAR = "2025-26 (existing layer, SY26-27 planning app)"
AISD = "https://services2.arcgis.com/5HXFPHsCivNoDso0/arcgis/rest/services/Reassignments/FeatureServer"
for lyr, level, suf, field in ((3, "middle", "ms", "MID_DESC"),
                               (9, "elementary", "es", "Campus")):
    d = json.loads(get(f"{AISD}/{lyr}/query", {"where": "1=1",
        "outFields": field, "returnGeometry": "false", "f": "json"}))
    for n in sorted({(f["attributes"][field] or "").strip()
                     for f in d["features"]} - {""}):
        add(f"austin-{slug(n)}-{suf}", "Austin ISD", n,
            level, "arcgis_query_url", f"{AISD}/{lyr}",
            f"{field}='{n}'", field)

# ---- PfISD (embedded webmap) ----
YEAR = "2025-26"
PF = "https://www.arcgis.com/sharing/rest/content/items/bb587c1043a949cca04f1b1904c235e3/data?f=json"
wm = json.loads(get(PF))
for title, level, suf, field in (("Middle Schools 2025-26", "middle", "ms", "MID_NAME"),
                                 ("Elementary Schools 2025-26", "elementary", "es", "ELEM_NAME")):
    for l in wm["operationalLayers"]:
        if l.get("title") != title:
            continue
        for sub in l.get("featureCollection", {}).get("layers", []):
            names = sorted({(f["attributes"].get(field) or "").strip()
                            for f in sub.get("featureSet", {}).get("features", [])} - {""})
            for n in names:
                if "OPPORTUNITY" in n:
                    continue
                add(f"pflugerville-{slug(n.replace(' SCHOOL',''))}-{suf}",
                    "Pflugerville ISD", n.title(), level,
                    "arcgis_webmap_embedded", PF.replace("?f=json", "") + "?f=json",
                    f"{field}='{n}'", field)
                entries[-1] = entries[-1].replace(
                    'boundary_has_name = true',
                    f'boundary_layer_title = "{title}"\nboundary_has_name = true')

# ---- MyPlanWare (Eanes, Georgetown, Liberty Hill) ----
MPW = {
    ("Eanes ISD", "40669a54-a33b-43e1-ab74-8ce7364ec1c9"): [
        ("afaff545-ade1-4754-8f2e-7321b312a994", "elementary", "es", "2026-27"),
        ("c9a4d64c-5359-4c0d-8bae-fa7816275999", "middle", "ms", "2026-27")],
    ("Georgetown ISD", "65021521-b0d6-4590-a14c-882d415961ba"): [
        ("be1c4ba0-b17c-48cd-9b49-a2a20e5f2df3", "elementary", "es", "2026-27"),
        ("3add9899-5b1a-438a-af08-d545c91430f1", "middle", "ms", "2026-27")],
    ("Liberty Hill ISD", "64c78a13-a79a-427c-b4c3-f2f40e91ff42"): [
        ("a86fc61f-e7da-4fff-ac58-49d3c420c39b", "elementary", "es", "2026-27"),
        ("92a9cd70-a1c5-46cc-ad30-3ab6d5bc10b8", "middle", "ms", "2026-27")],
}
for (district, cid), items in MPW.items():
    dslug = slug(district.replace(" ISD", ""))
    for iid, level, suf, year in items:
        YEAR = year
        url = (f"https://apps.myplanware.com/api/schoolsearch/data?kind=collection"
               f"&clientId={cid}&collectionItemId={iid}")
        d = json.loads(get(url))
        d = d[0] if isinstance(d, list) else d
        for f in d.get("features", []):
            n = (f.get("properties", {}).get("name") or "").strip()
            if not n or "Over 55" in n or "District Office" in n or "(" in n:
                continue
            add(f"{dslug}-{slug(n.replace(' School','').replace(' Elementary','').replace(' Middle',''))}-{suf}",
                district, n, level, "myplanware_geojson", url,
                f"name='{n}'", "name")

# ---- Hays CISD (KML) ----
YEAR = "2024-25 map (verify vintage)"
KML = "https://www.google.com/maps/d/kml?mid=1sL-9kjFP69cxCbdggELeSl05aMPx2Q4&forcekml=1"
ns = {"k": "http://www.opengis.net/kml/2.2"}
root = ET.fromstring(get(KML))
clean = lambda s: " ".join((s or "").replace("\xa0", " ").split())
names = sorted({clean(p.findtext("k:name", "", ns))
                for p in root.iter(f"{{{ns['k']}}}Placemark")
                if p.find(".//k:Polygon", ns) is not None})
for n in names:
    if n.endswith("Middle School Zone"):
        level, suf = "middle", "ms"
    elif n.endswith("Elementary Attendance Zone"):
        level, suf = "elementary", "es"
    else:
        continue
    school = n.replace(" Attendance Zone", "").replace(" Zone", "")
    add(f"hayscisd-{slug(school.replace(' Elementary','').replace(' Middle School',''))}-{suf}",
        "Hays CISD", school, level, "google_mymaps_kml", KML,
        f"name='{n}'", "name")

# dedupe keys (nbsp dupes collapse to same key/where after clean)
seen, out = set(), []
for e in entries:
    key = e.split("]")[0]
    if key not in seen:
        seen.add(key)
        out.append(e)

header = ("\n# " + "=" * 74 +
          "\n# MULTI-LEVEL BUILDOUT (generated 2026-07-04 from live source name"
          "\n# lists): district / middle / elementary zones. expected_area_km2 = 0"
          "\n# is the bootstrap sentinel — pinned from first fetch; accuracy is"
          "\n# guarded by audit_zones.py (campus containment + coverage + roster)."
          "\n# " + "=" * 74 + "\n\n")
with open(ROOT / "zones.toml", "a", encoding="utf-8", newline="\n") as f:
    f.write(header + "\n\n".join(out) + "\n")
print(f"emitted {len(out)} entries")
cfg2 = tomllib.loads((ROOT / "zones.toml").read_text(encoding="utf-8"))
from collections import Counter
print(Counter(z.get("level", "high") for z in cfg2.values()))
