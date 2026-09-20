"""Unit tests: perp sweep, funding block/boost, optimizer floors, failure blacklist."""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from trading_bot.derivatives_feed import (
    PerpLeadLagEngine,
    detect_buy_sweep,
    detect_liq_cascade,
    funding_long_trap,
    funding_short_squeeze_boost,
    bars_stagnant_or_down,
    FundingOIFilter,
)
from trading_bot.failure_postmortem import (
    TAG_LIQUIDITY_GRAB,
    TAG_LOW_VOL_FAKEOUT,
    build_failure_snapshot,
    check_failure_blacklist,
    classify_failure,
    snapshot_matches_signature,
)
from trading_bot.optimizer import (
    CEILING_RSI_BUY_CAP,
    FLOOR_MIN_TP_PCT,
    FLOOR_RVOL_BREAKOUT_MULT,
    clamp_params,
    walk_forward_sweep,
    load_active_params,
    run_optimizer,
)
from trading_bot.state_store import BehavioralStateStore


def test_sweep_detector_threshold():
    assert detect_buy_sweep(301.0, 100.0, mult=3.0) is True
    assert detect_buy_sweep(299.0, 100.0, mult=3.0) is False
    assert detect_buy_sweep(100.0, 0.0, mult=3.0) is False


def test_leadlag_engine_ingest_sweep():
    eng = PerpLeadLagEngine(
        enabled=True,
        venues="binance",
        symbols=("BTC-USD",),
        sweep_mult=3.0,
        sweep_window_sec=5.0,
        signal_ttl_sec=60.0,
        avg_trade_window=50,
    )
    # Seed small trades to set average ~100
    for _ in range(20):
        eng.ingest_agg_trade("BTCUSDT", qty=1.0, price=100.0, is_buyer_maker=True)
    # Aggressive buys totaling > 3x avg in window
    for _ in range(4):
        eng.ingest_agg_trade("BTCUSDT", qty=1.0, price=100.0, is_buyer_maker=False)
    snap = eng.get_snapshot()
    assert snap.recent_buy_sweep is True
    assert "BTC-USD" in snap.sweep_symbols


def test_liq_cascade_detector():
    assert detect_liq_cascade(5, min_cluster=5) is True
    assert detect_liq_cascade(4, min_cluster=5) is False


def test_funding_block_and_boost():
    block, tag = funding_long_trap(0.0004, price_stagnant_or_down=True)
    assert block is True
    assert tag == "funding_long_trap"
    block2, _ = funding_long_trap(0.0004, price_stagnant_or_down=False)
    assert block2 is False
    boost, btag = funding_short_squeeze_boost(-0.0002, 0.05)
    assert boost is True
    assert btag == "funding_short_squeeze_boost"
    boost2, _ = funding_short_squeeze_boost(-0.0002, 0.01)
    assert boost2 is False


def test_funding_filter_evaluate():
    filt = FundingOIFilter(
        enabled=True,
        symbols=["ETH-USD"],
        funding_block_threshold=0.0003,
        funding_boost_threshold=-0.0001,
        oi_surge_pct=0.02,
    )
    from trading_bot.derivatives_feed import FundingOISnapshot
    import time

    filt._cache["ETH-USD"] = FundingOISnapshot(
        symbol="ETH-USD",
        perp="ETHUSDT",
        funding_rate=0.0005,
        open_interest=1e6,
        oi_delta_pct=0.0,
        fetched_at=time.time(),
    )
    bars = [
        {"open": 100, "close": 99},
        {"open": 99, "close": 98},
        {"open": 98, "close": 97},
    ]
    allow, delta, tag = filt.evaluate("ETH-USD", bars)
    assert allow is False
    assert tag == "funding_long_trap"

    filt._cache["ETH-USD"] = FundingOISnapshot(
        symbol="ETH-USD",
        perp="ETHUSDT",
        funding_rate=-0.0002,
        open_interest=1e6,
        oi_delta_pct=0.05,
        fetched_at=time.time(),
    )
    allow2, delta2, tag2 = filt.evaluate("ETH-USD", bars)
    assert allow2 is True
    assert tag2 == "funding_short_squeeze_boost"
    assert delta2 > 0


def test_bars_stagnant():
    assert bars_stagnant_or_down(
        [{"open": 10, "close": 9}, {"open": 9, "close": 8}, {"open": 8, "close": 7}]
    )


def test_optimizer_floor_clamps():
    c = clamp_params(rvol_breakout_mult=1.0, rsi_buy_cap=80.0, min_tp_pct=0.01)
    assert c["rvol_breakout_mult"] == FLOOR_RVOL_BREAKOUT_MULT
    assert c["rsi_buy_cap"] == CEILING_RSI_BUY_CAP
    assert c["min_tp_pct"] == FLOOR_MIN_TP_PCT


def test_optimizer_walk_forward_respects_floors():
    # Synthetic trending volume spikes
    rows = []
    px = 100.0
    for i in range(200):
        spike = 5.0 if i % 25 == 0 else 1.0
        o = px
        c = px + 0.2
        rows.append(
            {
                "open": o,
                "high": c + 0.3,
                "low": o - 0.1,
                "close": c,
                "volume": 1000.0 * spike,
            }
        )
        px = c
    df = pd.DataFrame(rows)
    params = walk_forward_sweep(
        {"BTC-USD": df},
        rvol_grid=(1.0, 1.5, 2.0),  # 1.0 must clamp
        rsi_grid=(80.0, 72.0),
        min_tp_grid=(0.01, 0.025),
    )
    assert params.rvol_breakout_mult >= FLOOR_RVOL_BREAKOUT_MULT
    assert params.rsi_buy_cap <= CEILING_RSI_BUY_CAP
    assert params.min_tp_pct >= FLOOR_MIN_TP_PCT


def test_optimizer_writes_active_params(tmp_path: Path):
    out = tmp_path / "active_params.json"
    params = run_optimizer(
        symbols=["BTC-USD"],
        days=1,
        out_path=out,
        feed_frames={
            "BTC-USD": pd.DataFrame(
                {
                    "open": [1, 2, 3],
                    "high": [2, 3, 4],
                    "low": [0.5, 1, 2],
                    "close": [1.5, 2.5, 3.5],
                    "volume": [10, 20, 30],
                }
            )
        },
        use_network=False,
    )
    assert out.exists()
    loaded = load_active_params(out)
    assert loaded is not None
    assert loaded["min_tp_pct"] >= FLOOR_MIN_TP_PCT
    assert params.min_tp_pct >= FLOOR_MIN_TP_PCT


def test_failure_snapshot_write_and_blacklist(tmp_path: Path):
    db = tmp_path / "t.db"
    store = BehavioralStateStore(str(db))
    snap = build_failure_snapshot(
        symbol="ETH-USD",
        extras={
            "l2_imbalance_ratio": 0.7,
            "volume_ratio": 0.9,
            "ema_200_slope": -0.01,
        },
        recent_bars=[
            {"open": 100, "high": 101, "low": 95, "close": 96, "volume": 50},
            {"open": 96, "high": 97, "low": 94, "close": 94.5, "volume": 40},
        ],
    )
    tag = classify_failure(snap)
    assert tag == TAG_LIQUIDITY_GRAB
    store.record_trade_failure(
        symbol="ETH-USD",
        signature_tag=tag,
        snapshot=snap,
        block_minutes=60,
    )
    active = store.get_active_failure_blacklist(lookback_hours=24)
    assert len(active) >= 1
    live = build_failure_snapshot(
        symbol="ETH-USD",
        extras={"l2_imbalance_ratio": 0.8},
        recent_bars=[],
    )
    blocked, reason = check_failure_blacklist(live, active)
    assert blocked is True
    assert "Liquidity Grab" in reason


def test_low_vol_fakeout_tag():
    snap = {
        "l2_depth_ratio": 1.5,
        "volume_ratio": 0.8,
        "candle_summary": {"avg_volume": 100, "last_volume": 20, "ret_pct": -0.01},
    }
    assert classify_failure(snap) == TAG_LOW_VOL_FAKEOUT
    assert snapshot_matches_signature(
        {"volume_ratio": 1.0, "candle_summary": {}}, TAG_LOW_VOL_FAKEOUT
    )
