"""Spike helper: pull service URLs out of an ArcGIS app/webmap config JSON."""
import json
import re
import sys

raw = open(sys.argv[1], encoding="utf-8").read()
d = json.loads(raw)
map_item = d.get("map", {}).get("itemId")
if map_item:
    print("map itemId:", map_item)
pattern = re.compile(r"https?://[^\"\s\\]+/(?:FeatureServer|MapServer)(?:/\d+)?")
for url in sorted(set(pattern.findall(raw))):
    print(url)
