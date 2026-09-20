"""Lean unit tests for local-tree speed/intelligence filters."""

from __future__ import annotations

from trading_bot.utils.decision_filters import (
    atr_bracket_levels,
    format_post_stop_cooldown_skip,
    check_htf_ema200_long,
    effective_entry_threshold,
    l2_top_n_imbalance,
    proximity_l2_boost,
)
from trading_bot.utils.entry_proximity import get_entry_proximity


def test_proximity_plus10_on_imbalance():
    base = get_entry_proximity(
        close=100.0,
        vwap=100.0,
        extras={"retest_ok": True, "breakout_volume": 1000, "pullback_volume": 200},
    )
    boosted = get_entry_proximity(
        close=100.0,
        vwap=100.0,
        extras={
            "retest_ok": True,
            "breakout_volume": 1000,
            "pullback_volume": 200,
            "l2_top5_ratio": 1.8,
        },
    )
    assert boosted["components"]["l2_boost"] == 10.0
    assert boosted["score"] == min(100.0, round(base["score"] + 10.0, 1))
    # ratio below threshold → no boost
    flat = get_entry_proximity(
        close=100.0,
        vwap=100.0,
        extras={
            "retest_ok": True,
            "breakout_volume": 1000,
            "pullback_volume": 200,
            "l2_top5_ratio": 1.2,
        },
    )
    assert flat["components"]["l2_boost"] == 0.0

    pts, _ = proximity_l2_boost(1.51)
    assert pts == 10.0
    # top-5 helper
    ratio = l2_top_n_imbalance(
        [{"price": 1, "size": 30}] * 5,
        [{"price": 1.1, "size": 10}] * 5,
        n=5,
    )
    assert ratio == 3.0


def test_adx_raises_threshold():
    thresh, raised = effective_entry_threshold(35.0, 15.0)
    assert raised is True
    assert thresh == 50.0
    thresh2, raised2 = effective_entry_threshold(35.0, 25.0)
    assert raised2 is False
    assert thresh2 == 35.0
    thresh3, raised3 = effective_entry_threshold(35.0, None)
    assert raised3 is False
    assert thresh3 == 35.0


def test_atr_sl_tp_math():
    # Large ATR path: 1.8*ATR=3.6 > 1.2% floor (1.2) → use ATR
    sl, tp = atr_bracket_levels(100.0, 2.0, sl_mult=1.8, tp_mult=3.0, sl_min_pct=0.012)
    assert abs(sl - 96.4) < 1e-9  # 100 - 1.8*2
    assert abs(tp - 106.0) < 1e-9  # 100 + 3.0*2


def test_atr_sl_floor_tiny_atr():
    """Tiny ATR still yields SL >= 1.2% below entry."""
    entry = 100.0
    atr = 0.1  # 1.8*ATR=0.18 << 1.2
    sl, tp = atr_bracket_levels(entry, atr, sl_mult=1.8, tp_mult=3.0, sl_min_pct=0.012)
    assert abs(sl - (entry - entry * 0.012)) < 1e-9
    assert (entry - sl) >= entry * 0.012 - 1e-12
    # TP must not sit inside SL distance
    assert (tp - entry) >= (entry - sl) - 1e-12


def test_atr_sl_uses_atr_when_large():
    entry = 100.0
    atr = 5.0  # 1.8*5=9.0 > 1.2
    sl, tp = atr_bracket_levels(entry, atr, sl_mult=1.8, tp_mult=3.0, sl_min_pct=0.012)
    assert abs(sl - (entry - 1.8 * atr)) < 1e-9
    assert abs(tp - (entry + 3.0 * atr)) < 1e-9


def test_htf_suppress_when_below_ema200():
    ok, detail = check_htf_ema200_long(99.0, 100.0)
    assert ok is False
    assert "htf_ema200" in detail
    ok2, detail2 = check_htf_ema200_long(101.0, 100.0)
    assert ok2 is True
    assert "ok" in detail2
    # missing data does not invent a hard block at helper layer
    ok3, _ = check_htf_ema200_long(None, 100.0)
    assert ok3 is True
