"""join_zones.py — apportion ACS block-group counts into attendance zones.

THE trust artifact (SPEC.md §4). Owns ALL statistics; the viewer does none.

Method: block-population apportionment. The centroid test runs at the 2020
PL-94 *block* level (small enough that the simple test stops lying), and each
ACS block-group count is weighted into the zone by the share of the block
group's weight-universe that sits inside the zone.

The weight universe is a PER-TABLE decision and must approximate the ACS
table's own universe (person-weighting a household table is bias correlated
with the comparison):

    B03002  race/ethnicity   universe: persons      -> weight by P1 (total pop)
    B15003  education (25+)  universe: persons 25+  -> weight by P3 (18+, closest free proxy)
    B19001  income brackets  universe: households   -> weight by H1 (occupied units)

Stopping rule (adopted in the design debate): correct *bias* down to the
resolution the free PL-94 data supports, then disclose the residual and quit.
Variance (MOE) is combined in quadrature and committed, never hidden.

CRS invariant: all geometry handed to this module is EPSG:4326. Enforced by
the fetchers; assumed-and-documented here.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

from shapely.geometry import shape
from shapely.prepared import prep

# ---------------------------------------------------------------------------
# Module constants — cached judgment calls live in CODE, not zones.toml,
# so they cannot rot silently as per-zone state. Change in review.
# ---------------------------------------------------------------------------
CV_FALLBACK = 0.30   # combined CV above this -> recompute variable at tract
MIN_BLOCKS = 30      # zones under this get the "coarse geography" flag
Z_90 = 1.645         # ACS MOEs are published at the 90% confidence level

# Per-table weight universe (see module docstring).
UNIVERSE = {
    "B03002": "P1",   # persons
    "B15003": "P3",   # persons 25+ -> 18+ proxy (terminus per stopping rule)
    "B19001": "H1",   # households  -> occupied housing units
}

# B19001 income brackets, in table order: (variable suffix, lower, upper).
# upper=None marks the open top bracket — its median is NEVER interpolated.
B19001_BRACKETS = [
    ("002", 0, 10_000), ("003", 10_000, 15_000), ("004", 15_000, 20_000),
    ("005", 20_000, 25_000), ("006", 25_000, 30_000), ("007", 30_000, 35_000),
    ("008", 35_000, 40_000), ("009", 40_000, 45_000), ("010", 45_000, 50_000),
    ("011", 50_000, 60_000), ("012", 60_000, 75_000), ("013", 75_000, 100_000),
    ("014", 100_000, 125_000), ("015", 125_000, 150_000),
    ("016", 150_000, 200_000), ("017", 200_000, None),
]


# ---------------------------------------------------------------------------
# Apportionment core
# ---------------------------------------------------------------------------

def _block_xy(b: dict) -> tuple[float, float]:
    """Cached block point coords (blocks are Point geometries from TIGERweb)."""
    xy = b.get("_xy")
    if xy is None:
        g = b["geometry"]
        if g["type"] == "Point":
            xy = tuple(g["coordinates"][:2])
        else:
            c = shape(g).centroid
            xy = (c.x, c.y)
        b["_xy"] = xy
    return xy


def block_weights(zone_geom, blocks: list[dict], weight_field: str,
                  geo_chars: int = 12) -> dict[str, float]:
    """Per-geography share of `weight_field` inside the zone.

    blocks: [{"GEOID": 15-char block id, "P1": int, "P3": int, "H1": int,
              "geometry": GeoJSON geometry}], all EPSG:4326.
    geo_chars slices the containing geography off the block GEOID (2020
    vintage nesting, asserted at ingest): 12 = block group, 11 = tract.
    Bbox prefilter + prepared geometry: ~200 zones x ~50k blocks is minutes,
    not an hour.
    """
    from shapely.geometry import Point
    prepared = prep(zone_geom)
    minx, miny, maxx, maxy = zone_geom.bounds
    zone_w: dict[str, float] = {}
    total_w: dict[str, float] = {}
    for b in blocks:
        geo = b["GEOID"][:geo_chars]
        w = float(b.get(weight_field, 0) or 0)
        total_w[geo] = total_w.get(geo, 0.0) + w
        x, y = _block_xy(b)
        if minx <= x <= maxx and miny <= y <= maxy and \
           prepared.contains(Point(x, y)):
            zone_w[geo] = zone_w.get(geo, 0.0) + w
    return {
        geo: (zone_w.get(geo, 0.0) / tw) if tw else 0.0
        for geo, tw in total_w.items()
    }


def apportion(weights: dict[str, float], counts: dict[str, dict[str, float]],
              moes: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    """Weight geography-level COUNTS (never medians) into the zone.

    counts/moes: {variable: {geo_id: value}}. Returns
    {variable: {"value": ..., "moe": ...}} with MOE combined in quadrature:
    MOE_zone = sqrt(sum((w_i * MOE_i)^2)) — standard Census practice
    (ignores the weights' own variance; disclosed, not hidden).
    """
    out: dict[str, dict[str, float]] = {}
    for var, per_geo in counts.items():
        est = 0.0
        moe_sq = 0.0
        for geo, n in per_geo.items():
            w = weights.get(geo, 0.0)
            if w == 0.0:
                continue
            est += w * float(n)
            m = float(moes.get(var, {}).get(geo, 0) or 0)
            moe_sq += (w * m) ** 2
        out[var] = {"value": est, "moe": math.sqrt(moe_sq)}
    return out


def cv(value: float, moe: float) -> float | None:
    """Coefficient of variation. SE = MOE/1.645 (90% level). None if value==0."""
    if not value:
        return None
    return (moe / Z_90) / abs(value)


# ---------------------------------------------------------------------------
# Derived statistics — computed AFTER apportionment, committed, never in JS
# ---------------------------------------------------------------------------

def derive_median_income(brackets: dict[str, dict[str, float]]) -> dict:
    """Median household income from apportioned B19001 bracket counts.

    Linear interpolation within the containing bracket. OPEN-TOP GUARD: if the
    median falls in the open top bracket ($200k+), do NOT interpolate a
    false-precision number — report display=">$200,000", value=None, flagged.
    (Median MOE is not computed in v1 — a documented limitation; deriving it
    honestly needs the Census design-factor method. Disclosure over precision.)
    """
    totals = [(suffix, lo, hi, brackets.get(f"B19001_{suffix}", {}).get("value", 0.0))
              for suffix, lo, hi in B19001_BRACKETS]
    n_households = sum(t[3] for t in totals)
    if n_households <= 0:
        return {"value": None, "display": "no households", "flag": "empty",
                "moe": None, "cv": None}
    half = n_households / 2.0
    cum = 0.0
    for _suffix, lo, hi, n in totals:
        if cum + n >= half and n > 0:
            if hi is None:
                # Open top bracket holds the median. Honest beats precise.
                return {"value": None, "display": ">$200,000",
                        "flag": "median_in_open_top_bracket",
                        "moe": None, "cv": None}
            median = lo + (half - cum) / n * (hi - lo)
            return {"value": round(median), "display": f"${round(median):,}",
                    "flag": None, "moe": None, "cv": None}
        cum += n
    return {"value": None, "display": "no households", "flag": "empty",
            "moe": None, "cv": None}


def derive_shares(apportioned: dict[str, dict[str, float]],
                  numerators: dict[str, str], denominator: str) -> dict[str, dict]:
    """Shares (e.g. pct_hispanic) from apportioned counts, with the standard
    Census proportion-MOE formula:
        MOE_p = sqrt(MOE_num^2 - p^2 * MOE_den^2) / den   (ratio fallback if
        the radicand is negative, per ACS handbook)."""
    den = apportioned.get(denominator, {"value": 0.0, "moe": 0.0})
    out = {}
    for name, num_var in numerators.items():
        num = apportioned.get(num_var, {"value": 0.0, "moe": 0.0})
        if not den["value"]:
            out[name] = {"value": None, "moe": None, "cv": None}
            continue
        p = num["value"] / den["value"]
        radicand = num["moe"] ** 2 - (p ** 2) * (den["moe"] ** 2)
        if radicand < 0:  # ratio formula fallback
            radicand = num["moe"] ** 2 + (p ** 2) * (den["moe"] ** 2)
        moe_p = math.sqrt(radicand) / den["value"]
        out[name] = {"value": round(p, 4), "moe": round(moe_p, 4),
                     "cv": round(cv(p, moe_p), 4) if cv(p, moe_p) is not None else None}
    return out


# ---------------------------------------------------------------------------
# Resolution fallback — derived per variable, never configured per zone
# ---------------------------------------------------------------------------

def apportion_with_fallback(zone_geom, blocks: list[dict], table: str,
                            bg_counts, bg_moes, tract_counts, tract_moes) -> dict:
    """Apportion at block group; auto-recompute a variable at tract when its
    combined CV blows past CV_FALLBACK. Stamps resolution + reason — these are
    COMMITTED, DIFFED fields: they are what stop a future vintage's fallback
    from reading as a real value change ("geography moved, not the neighborhood").
    """
    wfield = UNIVERSE[table]
    bg_weights = block_weights(zone_geom, blocks, wfield, geo_chars=12)
    # Tract weights reuse the same blocks one level up: tract GEOID = block[:11].
    tr_weights = block_weights(zone_geom, blocks, wfield, geo_chars=11)

    at_bg = apportion(bg_weights, bg_counts, bg_moes)
    at_tract = apportion(tr_weights, tract_counts, tract_moes)

    out = {}
    for var, est in at_bg.items():
        c = cv(est["value"], est["moe"])
        if c is not None and c > CV_FALLBACK and var in at_tract:
            t = at_tract[var]
            tc = cv(t["value"], t["moe"])
            out[var] = {
                "value": t["value"], "moe": t["moe"],
                "cv": round(tc, 4) if tc is not None else None,
                "resolution": "tract",
                "reason": f"block_group CV {c:.2f} > {CV_FALLBACK}",
            }
        else:
            out[var] = {
                "value": est["value"], "moe": est["moe"],
                "cv": round(c, 4) if c is not None else None,
                "resolution": "block_group", "reason": None,
            }
    return out


def count_blocks_in_zone(zone_geom, blocks: list[dict]) -> int:
    from shapely.geometry import Point
    prepared = prep(zone_geom)
    minx, miny, maxx, maxy = zone_geom.bounds
    n = 0
    for b in blocks:
        x, y = _block_xy(b)
        if minx <= x <= maxx and miny <= y <= maxy and \
           prepared.contains(Point(x, y)):
            n += 1
    return n


# ---------------------------------------------------------------------------
# Zone pipeline
# ---------------------------------------------------------------------------

def join_zone(zone_geom, blocks, acs: dict) -> dict:
    """Full demographics for one zone.

    acs: {"bg":    {table: {"counts": {var:{geo:val}}, "moes": {...}}},
          "tract": {table: {...}}}
    Returns the committed demographics dict (SPEC.md §4 output shape).
    """
    result: dict = {}
    per_table: dict[str, dict] = {}
    for table in UNIVERSE:
        if table not in acs.get("bg", {}):
            continue
        per_table[table] = apportion_with_fallback(
            zone_geom, blocks, table,
            acs["bg"][table]["counts"], acs["bg"][table]["moes"],
            acs.get("tract", {}).get(table, {}).get("counts", {}),
            acs.get("tract", {}).get(table, {}).get("moes", {}),
        )

    # Race/ethnicity shares (B03002: _001 total, _003 NH white, _004 NH black,
    # _006 NH asian, _012 hispanic).
    if "B03002" in per_table:
        counts = per_table["B03002"]
        result["race_ethnicity"] = {
            "counts": counts,
            "shares": derive_shares(
                {v: {"value": d["value"], "moe": d["moe"]} for v, d in counts.items()},
                numerators={
                    "pct_nh_white": "B03002_003", "pct_nh_black": "B03002_004",
                    "pct_nh_asian": "B03002_006", "pct_hispanic": "B03002_012",
                },
                denominator="B03002_001",
            ),
        }

    # Income: brackets + derived median (never a pulled median — B19013 is
    # deliberately excluded upstream; averaging medians is statistically illegal).
    if "B19001" in per_table:
        brackets = per_table["B19001"]
        result["income"] = {
            "brackets": brackets,
            "median_household_income": derive_median_income(brackets),
        }

    # Education (25+): attainment rates from apportioned counts.
    # B15003: _001 total 25+, _022 bachelor's, _023 master's, _024 professional,
    # _025 doctorate.
    if "B15003" in per_table:
        counts = per_table["B15003"]
        ba_plus_vars = ["B15003_022", "B15003_023", "B15003_024", "B15003_025"]
        present = [v for v in ba_plus_vars if v in counts]
        if present and "B15003_001" in counts:
            ba_val = sum(counts[v]["value"] for v in present)
            ba_moe = math.sqrt(sum(counts[v]["moe"] ** 2 for v in present))
            result["education"] = {
                "counts": counts,
                "shares": derive_shares(
                    {"ba_plus": {"value": ba_val, "moe": ba_moe},
                     "B15003_001": {"value": counts["B15003_001"]["value"],
                                    "moe": counts["B15003_001"]["moe"]}},
                    numerators={"pct_bachelors_or_higher": "ba_plus"},
                    denominator="B15003_001",
                ),
            }

    # Disclosure fields (the number tells you how much to trust it).
    n_blocks = count_blocks_in_zone(zone_geom, blocks)
    result["block_count"] = n_blocks
    result["coarse_geography"] = n_blocks < MIN_BLOCKS
    return result


# ---------------------------------------------------------------------------
# CLI: join all zones with committed boundaries + fetched census data
# ---------------------------------------------------------------------------

def main() -> int:
    root = Path(__file__).resolve().parent.parent
    boundaries = sorted((root / "data" / "boundaries").glob("*.geojson"))
    census_dir = root / "data" / "census"
    if not boundaries:
        print(">> no boundaries in data/boundaries/ — run fetch_boundaries.py "
              "(or hand-trace) first", file=sys.stderr)
        return 1
    blocks_file = census_dir / "pl94-blocks.json"
    acs_file = census_dir / "acs.json"
    if not (blocks_file.exists() and acs_file.exists()):
        print(">> census inputs missing — run fetch_census.py first", file=sys.stderr)
        return 1

    blocks = json.loads(blocks_file.read_text(encoding="utf-8"))
    acs = json.loads(acs_file.read_text(encoding="utf-8"))
    out_dir = root / "data" / "joined"
    out_dir.mkdir(parents=True, exist_ok=True)

    for bf in boundaries:
        fc = json.loads(bf.read_text(encoding="utf-8"))
        zone_geom = shape(fc["features"][0]["geometry"])
        demo = join_zone(zone_geom, blocks, acs)
        out = out_dir / f"{bf.stem}.demographics.json"
        out.write_text(json.dumps(demo, indent=1, sort_keys=True), encoding="utf-8")
        print(f">> {bf.stem}: {demo['block_count']} blocks -> {out.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
