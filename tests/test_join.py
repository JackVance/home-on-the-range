"""test_join.py — the one required test (SPEC.md §5).

Pins the un-eyeballable apportionment/median/MOE math to a hand-computed
4-block fixture. Every future refactor of join_zones.py either matches this
fixture or is a bug.

Required cases (all present):
  1. Blocks straddling the zone edge (C: centroid in, D: centroid out) —
     pins the geometry test and documents the knowingly-accepted error.
  2. A zone whose median lands in the open top bracket — pins the
     ">$200,000" guard FIRING (the load-bearing branch).
  3. The bg->tract fallback trigger — pins that a high-CV variable recomputes
     at tract and stamps resolution/reason.
Plus: MOE-in-quadrature asserted against the hand-worked value, and the
per-table weight universe (P1 vs P3 vs H1) asserted via distinct weights.

Runs under pytest or directly: `python tests/test_join.py`.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shapely.geometry import shape  # noqa: E402

import join_zones as jz  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load():
    zone_fc = json.loads((FIXTURES / "toy_zone.geojson").read_text(encoding="utf-8"))
    zone = shape(zone_fc["features"][0]["geometry"])
    blocks = json.loads((FIXTURES / "toy_blocks.geojson").read_text(encoding="utf-8"))["blocks"]
    acs = json.loads((FIXTURES / "toy_acs.json").read_text(encoding="utf-8"))
    return zone, blocks, acs


def test_straddling_blocks_centroid_rule():
    """Case 1: C (straddles, centroid IN) counts; D (straddles, centroid OUT)
    does not. block_count == 2 (A and C) and the coarse-geography flag fires."""
    zone, blocks, acs = _load()
    assert jz.count_blocks_in_zone(zone, blocks) == 2
    demo = jz.join_zone(zone, blocks, acs)
    assert demo["block_count"] == 2
    assert demo["coarse_geography"] is True  # 2 < MIN_BLOCKS(30)


def test_per_table_universe_weights():
    """Weights differ per table because the universes differ:
    bg1: P1 2/3, P3 2/3, H1 0.8 ; bg2: P1 0.6, P3 0.625, H1 0.5."""
    zone, blocks, _ = _load()
    p1 = jz.block_weights(zone, blocks, "P1")
    p3 = jz.block_weights(zone, blocks, "P3")
    h1 = jz.block_weights(zone, blocks, "H1")
    assert math.isclose(p1["480019501001"], 2 / 3)
    assert math.isclose(p1["480019501002"], 0.6)
    assert math.isclose(p3["480019501002"], 0.625)
    assert math.isclose(h1["480019501001"], 0.8)
    assert math.isclose(h1["480019501002"], 0.5)
    # Tract level (geo_chars=11): P1 = 160/250 = 0.64
    tr = jz.block_weights(zone, blocks, "P1", geo_chars=11)
    assert math.isclose(tr["48001950100"], 0.64)


def test_apportionment_and_moe_quadrature():
    """Hand-worked: B03002_012 zone = 2/3*30 + 0.6*55 = 53.
    MOE = sqrt((2/3*10)^2 + (0.6*8)^2) = sqrt(67.4844) = 8.2149."""
    zone, blocks, acs = _load()
    demo = jz.join_zone(zone, blocks, acs)
    hisp = demo["race_ethnicity"]["counts"]["B03002_012"]
    assert math.isclose(hisp["value"], 53.0, rel_tol=1e-9)
    assert math.isclose(hisp["moe"], math.sqrt((2 / 3 * 10) ** 2 + (0.6 * 8) ** 2),
                        rel_tol=1e-9)
    assert hisp["resolution"] == "block_group"
    assert hisp["reason"] is None
    total = demo["race_ethnicity"]["counts"]["B03002_001"]
    assert math.isclose(total["value"], 2 / 3 * 130 + 0.6 * 110, rel_tol=1e-9)
    # Derived share: 53 / 152.6667 = 0.3472
    assert math.isclose(demo["race_ethnicity"]["shares"]["pct_hispanic"]["value"],
                        0.3472, abs_tol=1e-4)


def test_bg_to_tract_fallback_trigger():
    """Case 3: B03002_006 at bg has CV ~3.43 > 0.30 -> auto-recomputes at
    tract (0.64*3 = 1.92, MOE 0.64*4 = 2.56) and stamps resolution/reason."""
    zone, blocks, acs = _load()
    demo = jz.join_zone(zone, blocks, acs)
    asian = demo["race_ethnicity"]["counts"]["B03002_006"]
    assert asian["resolution"] == "tract"
    assert asian["reason"] is not None and "block_group CV" in asian["reason"]
    assert math.isclose(asian["value"], 0.64 * 3, rel_tol=1e-9)
    assert math.isclose(asian["moe"], 0.64 * 4, rel_tol=1e-9)


def test_open_top_bracket_guard_fires():
    """Case 2: apportioned brackets 4 / 2 / 36 (total 42); half = 21 lands in
    the open $200k+ bracket -> NO interpolated number, display '>$200,000'."""
    zone, blocks, acs = _load()
    demo = jz.join_zone(zone, blocks, acs)
    med = demo["income"]["median_household_income"]
    assert med["value"] is None
    assert med["display"] == ">$200,000"
    assert med["flag"] == "median_in_open_top_bracket"


def test_median_interpolation_path():
    """The closed-bracket branch, hand-worked: brackets 10 / 30 / 20,
    total 60, half 30 -> median = 50000 + (30-10)/30 * 10000 = 56667."""
    brackets = {
        "B19001_002": {"value": 10.0},   # <10k
        "B19001_011": {"value": 30.0},   # 50-60k
        "B19001_013": {"value": 20.0},   # 75-100k
    }
    med = jz.derive_median_income(brackets)
    assert med["value"] == 56667
    assert med["flag"] is None


def test_education_uses_p3_weights():
    """B15003 weighted by P3 (2/3, 0.625), not P1: _001 zone = 103.75;
    ba_plus = 48.75; pct = 0.4699."""
    zone, blocks, acs = _load()
    demo = jz.join_zone(zone, blocks, acs)
    total = demo["education"]["counts"]["B15003_001"]
    assert math.isclose(total["value"], 2 / 3 * 90 + 0.625 * 70, rel_tol=1e-9)  # 103.75
    pct = demo["education"]["shares"]["pct_bachelors_or_higher"]
    assert math.isclose(pct["value"], 48.75 / 103.75, abs_tol=1e-4)


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
