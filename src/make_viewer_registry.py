"""make_viewer_registry.py — regenerate viewer/zones.json from zones.toml.

The viewer's registry is a machine dump of zones.toml keeping only the fields
the viewer reads (SPEC §viewer). Run after any zones.toml change:
    python src/make_viewer_registry.py
"""
from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    cfg = tomllib.loads((ROOT / "zones.toml").read_text(encoding="utf-8"))
    out = {}
    for key, z in cfg.items():
        entry = {
            "boundary_source": z["boundary_source"],
            "district": z["district"],
            "high_school": z["high_school"],
            "level": z.get("level", "high"),
            "listings_links": z.get("listings_links", []),
        }
        if z.get("grade_band"):
            entry["grade_band"] = z["grade_band"]
        out[key] = entry
    path = ROOT / "viewer" / "zones.json"
    path.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n",
                    encoding="utf-8", newline="\n")
    print(f">> wrote {path.relative_to(ROOT)}: {len(out)} zones")
    return 0


if __name__ == "__main__":
    sys.exit(main())
