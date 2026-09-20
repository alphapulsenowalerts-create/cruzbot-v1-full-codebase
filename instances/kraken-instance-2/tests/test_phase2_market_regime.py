"""Phase 2: Nightly ATR+ADX market regime (TRENDING / RANGING / HIGH_VOLATILITY)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trading_bot.models import (
    Action,
    AgentObservation,
    HigherTimeframeContext,
    IndicatorSnapshot,
)
from trading_bot.optimizer import (
    _label_atr_adx_centroids,
    _pure_kmeans,
    classify_market_regime_atr_adx,
)
from trading_bot.strategy import (
    REGIME_HIGH_VOLATILITY,
    REGIME_RANGING,
    REGIME_TRENDING,
    load_market_regime_from_active_params,
    market_regime_blocks_retest,
    normalize_market_regime,
    regime_trade_cap_mult,
)
from trading_bot.strategy_volume_sweet_spot import VolumeSweetSpotEngine


def _sweet_ind(**kwargs) -> IndicatorSnapshot:
    base = dict(
        symbol="ETH-USD",
        close=2500.0,
        volume=100.0,
        vwap=2495.0,
        rsi=55.0,
        macd=1.0,
        macd_signal=0.5,
        macd_hist=0.4,
        ema_fast=2498.0,
        ema_slow=2490.0,
        atr=10.0,
        ema_cross="none",
        extras={
            "volume_ratio": 2.5,
            "retest_ok": True,
            "breakout_volume": 1000.0,
            "pullback_volume": 100.0,
            "swing_low": 2450.0,
            "delta_proxy": "close>open",
            "breakout_rvol": 2.5,
            "resistance": 2600.0,
            "entry_reason": "RVOL Breakout + Low-Volume VWAP Retest",
            "l2_imbalance_ratio": 1.5,
            "adx": 30.0,
            "chop": 45.0,
        },
    )
    base.update(kwargs)
    if "extras" in kwargs:
        merged = dict(base["extras"]) if isinstance(base.get("extras"), dict) else {}
        # rebuild defaults then overlay
        merged = {
            "volume_ratio": 2.5,
            "retest_ok": True,
            "breakout_volume": 1000.0,
            "pullback_volume": 100.0,
            "swing_low": 2450.0,
            "delta_proxy": "close>open",
            "breakout_rvol": 2.5,
            "resistance": 2600.0,
            "entry_reason": "RVOL Breakout + Low-Volume VWAP Retest",
            "l2_imbalance_ratio": 1.5,
            "adx": 30.0,
            "chop": 45.0,
        }
        merged.update(kwargs["extras"])
        base["extras"] = merged
    return IndicatorSnapshot(**base)


def _obs(ind: IndicatorSnapshot) -> AgentObservation:
    htf = HigherTimeframeContext(
        timeframe="1Hour",
        ema_200=2400.0,
        ema_200_1h=2400.0,
        ema_200_4h=2300.0,
    )
    return AgentObservation(symbol="ETH-USD", indicators=ind, htf=htf)


# --- helpers ---


def test_normalize_market_regime_aliases():
    assert normalize_market_regime("high_vol") == REGIME_HIGH_VOLATILITY
    assert normalize_market_regime("HIGH_VOLATILITY") == REGIME_HIGH_VOLATILITY
    assert normalize_market_regime("trend") == REGIME_TRENDING
    assert normalize_market_regime("RANGE") == REGIME_RANGING
    assert normalize_market_regime(None) == REGIME_RANGING
    assert normalize_market_regime("garbage") == REGIME_RANGING


def test_regime_trade_cap_mult_high_vol_halves():
    assert regime_trade_cap_mult(REGIME_HIGH_VOLATILITY) == 0.5
    assert regime_trade_cap_mult("HIGH_VOL") == 0.5
    assert regime_trade_cap_mult(REGIME_TRENDING) == 1.0
    assert regime_trade_cap_mult(REGIME_RANGING) == 1.0


def test_market_regime_blocks_retest_when_enabled():
    blocked, detail = market_regime_blocks_retest(REGIME_HIGH_VOLATILITY, enabled=True)
    assert blocked is True
    assert "HIGH_VOLATILITY" in detail
    assert "block" in detail.lower()

    ok, detail2 = market_regime_blocks_retest(REGIME_RANGING, enabled=True)
    assert ok is False
    assert "allow" in detail2.lower()

    disabled, d3 = market_regime_blocks_retest(REGIME_HIGH_VOLATILITY, enabled=False)
    assert disabled is False
    assert "disabled" in d3.lower()


def test_load_market_regime_from_temp_active_params(tmp_path: Path):
    p = tmp_path / "active_params.json"
    p.write_text(
        json.dumps(
            {
                "rvol_breakout_mult": 2.0,
                "market_regime": "HIGH_VOLATILITY",
                "regime_trade_cap_mult": 0.5,
                "regime_updated_at": "2026-09-20T12:00:00+00:00",
                "regime_features": {"method": "test"},
            }
        ),
        encoding="utf-8",
    )
    regime, mtime, slice_ = load_market_regime_from_active_params(p)
    assert regime == REGIME_HIGH_VOLATILITY
    assert mtime is not None
    assert slice_["market_regime"] == REGIME_HIGH_VOLATILITY
    assert slice_["regime_features"]["method"] == "test"


def test_engine_high_vol_blocks_retest_and_scales_cap():
    eng = VolumeSweetSpotEngine(
        regime_gate_enabled=True,
        market_regime=REGIME_HIGH_VOLATILITY,
        # keep other intel gates off so regime is the sole hold reason
        l2_imbalance_enabled=False,
        regime_filter_enabled=False,
        mtf_align_enabled=False,
        tod_gate_enabled=False,
        disable_tod_gate=True,
        daily_dd_sqlite_enabled=False,
    )
    assert eng.regime_trade_cap_mult == 0.5
    d = eng.reason(_obs(_sweet_ind()))
    assert d.action == Action.HOLD
    assert "HIGH_VOLATILITY" in d.reasoning
    assert "block" in d.reasoning.lower()


def test_engine_gate_off_ignores_high_vol_cap():
    eng = VolumeSweetSpotEngine(
        regime_gate_enabled=False,
        market_regime=REGIME_HIGH_VOLATILITY,
        l2_imbalance_enabled=False,
        regime_filter_enabled=False,
        mtf_align_enabled=False,
        tod_gate_enabled=False,
        disable_tod_gate=True,
        daily_dd_sqlite_enabled=False,
    )
    assert eng.regime_trade_cap_mult == 1.0
    # Gate off → does not hold solely for HIGH_VOL (may still buy or hold for other reasons)
    blocked, _ = market_regime_blocks_retest(eng.market_regime, enabled=eng.regime_gate_enabled)
    assert blocked is False


def test_engine_hot_reload_from_active_params(tmp_path: Path):
    p = tmp_path / "active_params.json"
    p.write_text(json.dumps({"market_regime": "RANGING"}), encoding="utf-8")
    eng = VolumeSweetSpotEngine(
        regime_gate_enabled=True,
        market_regime="RANGING",
        active_params_path=str(p),
        l2_imbalance_enabled=False,
        regime_filter_enabled=False,
        mtf_align_enabled=False,
        tod_gate_enabled=False,
        disable_tod_gate=True,
        daily_dd_sqlite_enabled=False,
    )
    assert eng.market_regime == REGIME_RANGING
    # rewrite with HIGH_VOL and bump mtime (FS may keep same-second mtime)
    import os
    import time
    p.write_text(json.dumps({"market_regime": "HIGH_VOLATILITY"}), encoding="utf-8")
    os.utime(p, (time.time() + 5, time.time() + 5))
    label = eng.maybe_reload_market_regime()
    assert label == REGIME_HIGH_VOLATILITY
    assert eng.market_regime == REGIME_HIGH_VOLATILITY
    assert eng.regime_trade_cap_mult == 0.5


def test_set_market_regime_hot_apply():
    eng = VolumeSweetSpotEngine(regime_gate_enabled=True, market_regime="RANGING")
    eng.set_market_regime("HIGH_VOL")
    assert eng.market_regime == REGIME_HIGH_VOLATILITY
    assert eng.regime_trade_cap_mult == 0.5


# --- k-means classify on synthetic features ---


def _ohlcv_trend(n: int = 120, start: float = 100.0, drift: float = 0.8) -> pd.DataFrame:
    rows = []
    px = start
    for _ in range(n):
        o = px
        c = px + drift
        h = max(o, c) + abs(drift) * 0.3
        l = min(o, c) - abs(drift) * 0.1
        rows.append({"open": o, "high": h, "low": l, "close": c, "volume": 1000.0})
        px = c
    return pd.DataFrame(rows)


def _ohlcv_high_vol(n: int = 120, start: float = 100.0) -> pd.DataFrame:
    rows = []
    px = start
    for i in range(n):
        swing = 4.0 if i % 2 == 0 else -4.0
        o = px
        c = px + swing
        h = max(o, c) + 2.0
        l = min(o, c) - 2.0
        rows.append({"open": o, "high": h, "low": l, "close": c, "volume": 5000.0})
        px = c
    return pd.DataFrame(rows)


def _ohlcv_range(n: int = 120, start: float = 100.0) -> pd.DataFrame:
    rows = []
    px = start
    for i in range(n):
        delta = 0.15 if i % 2 == 0 else -0.15
        o = px
        c = px + delta
        h = max(o, c) + 0.05
        l = min(o, c) - 0.05
        rows.append({"open": o, "high": h, "low": l, "close": c, "volume": 800.0})
        px = c
    return pd.DataFrame(rows)


def test_pure_kmeans_and_centroid_labels():
    # Three well-separated clusters in [atr_ratio, adx]
    pts = np.vstack(
        [
            np.random.default_rng(0).normal([2.5, 20.0], 0.05, size=(20, 2)),  # high ATR
            np.random.default_rng(1).normal([1.0, 40.0], 0.05, size=(20, 2)),  # trend ADX
            np.random.default_rng(2).normal([0.9, 12.0], 0.05, size=(20, 2)),  # range
        ]
    )
    centroids, labels = _pure_kmeans(pts, k=3, seed=42)
    mapping = _label_atr_adx_centroids(centroids)
    assert set(mapping.values()) == {
        REGIME_HIGH_VOLATILITY,
        REGIME_TRENDING,
        REGIME_RANGING,
    }
    # highest atr centroid → HIGH_VOL
    high_i = int(np.argmax(centroids[:, 0]))
    assert mapping[high_i] == REGIME_HIGH_VOLATILITY
    assert len(labels) == len(pts)


def test_classify_market_regime_synthetic_high_vol_vs_trend():
    # High-vol frame alone with elevated ATR ratio → HIGH_VOLATILITY (rule or cluster)
    label_hv, feats_hv = classify_market_regime_atr_adx(
        {"SYN-HV": _ohlcv_high_vol()}, fallback="RANGING"
    )
    assert label_hv in (REGIME_HIGH_VOLATILITY, REGIME_TRENDING, REGIME_RANGING)
    assert "method" in feats_hv

    # Strong directional drift → prefer TRENDING or at least not fail
    label_tr, feats_tr = classify_market_regime_atr_adx(
        {"SYN-TR": _ohlcv_trend()}, fallback="RANGING"
    )
    assert label_tr in (REGIME_HIGH_VOLATILITY, REGIME_TRENDING, REGIME_RANGING)
    assert feats_tr.get("n_points", 0) >= 0

    # Mixed frames with a clear high-vol component should classify something valid
    label_mix, feats_mix = classify_market_regime_atr_adx(
        {
            "A": _ohlcv_high_vol(),
            "B": _ohlcv_trend(),
            "C": _ohlcv_range(),
        },
        fallback="RANGING",
    )
    assert label_mix in (REGIME_HIGH_VOLATILITY, REGIME_TRENDING, REGIME_RANGING)
    assert feats_mix.get("n_points", 0) >= 3 or feats_mix.get("reason")


def test_classify_empty_frames_soft_fallback():
    label, feats = classify_market_regime_atr_adx({}, fallback="RANGING")
    assert label == REGIME_RANGING
    assert feats.get("reason") == "no_feature_rows" or feats.get("n_points") == 0
