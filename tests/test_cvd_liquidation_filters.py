"""Phase 1: live CVD deltas + liquidation-sweep gates (no network)."""

from __future__ import annotations

import time

import pytest

from trading_bot.derivatives_feed import PerpLeadLagEngine
from trading_bot.models import Action, AgentObservation, IndicatorSnapshot
from trading_bot.strategy_volume_sweet_spot import VolumeSweetSpotEngine
from trading_bot.utils.indicators import check_cvd_divergence, check_liq_sweep


def _sweet_extras(**overrides) -> dict:
    base = {
        "volume_ratio": 2.5,
        "retest_ok": True,
        "breakout_volume": 1000.0,
        "pullback_volume": 100.0,
        "swing_low": 2400.0,
        "delta_proxy": "close>open",
        "breakout_rvol": 2.5,
        "resistance": 2700.0,
        "entry_reason": "RVOL Breakout + Low-Volume VWAP Retest",
        # Phase 1: green 5m setup bar + ready snapshots (tests override)
        "setup_bar_open": 2490.0,
        "setup_bar_close": 2500.0,  # green
        "cvd_snapshot": {"cvd_1m": 1000.0, "cvd_5m": 5000.0, "ready": True, "updated_at": time.time()},
        "liq_snapshot": {
            "short_liq_1m_usd": 60_000.0,
            "long_liq_1m_usd": 0.0,
            "ready": True,
            "updated_at": time.time(),
        },
    }
    base.update(overrides)
    return base


def _ind(**kwargs) -> IndicatorSnapshot:
    extras = kwargs.pop("extras", None)
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
        extras=_sweet_extras() if extras is None else extras,
    )
    base.update(kwargs)
    return IndicatorSnapshot(**base)


def _engine(**kwargs) -> VolumeSweetSpotEngine:
    defaults = dict(
        min_tp_pct=0.025,
        tp2_rr=2.5,
        swing_sl_buffer_pct=0.002,
        cvd_divergence_enabled=True,
        liq_sweep_required=True,
        liq_sweep_short_usd=50_000.0,
        cvd_warmup_fail_closed=True,
        # Keep intel gates off so Phase 1 tests isolate CVD/liq
        l2_imbalance_enabled=False,
        regime_filter_enabled=False,
        mtf_align_enabled=False,
    )
    defaults.update(kwargs)
    return VolumeSweetSpotEngine(**defaults)


# ---------------------------------------------------------------------------
# A) CVD from mock trade ticks
# ---------------------------------------------------------------------------


def test_cvd_sign_and_delta_from_agg_trades():
    eng = PerpLeadLagEngine(enabled=True, venues="binance", symbols=("BTC-USD",))
    now = time.time()
    # Sell aggressor (buyer is maker) → negative
    eng.ingest_agg_trade(
        "BTCUSDT", qty=1.0, price=100.0, is_buyer_maker=True, ts=now - 10
    )
    # Buy aggressor → positive
    eng.ingest_agg_trade(
        "BTCUSDT", qty=2.0, price=100.0, is_buyer_maker=False, ts=now - 5
    )
    # Another sell aggressor
    eng.ingest_agg_trade(
        "BTCUSDT", qty=0.5, price=100.0, is_buyer_maker=True, ts=now - 1
    )
    snap = eng.get_cvd_snapshot("BTC-USD")
    assert snap["ready"] is True
    # -100 + 200 - 50 = +50
    assert snap["cvd_1m"] == pytest.approx(50.0)
    assert snap["cvd_5m"] == pytest.approx(50.0)
    assert "updated_at" in snap


def test_cvd_1m_vs_5m_window_trim():
    eng = PerpLeadLagEngine(enabled=True, venues="binance", symbols=("ETH-USD",))
    now = time.time()
    # Old trade outside 1m but inside 5m
    eng.ingest_agg_trade(
        "ETHUSDT", qty=1.0, price=1000.0, is_buyer_maker=False, ts=now - 120
    )
    # Recent sell aggressor inside 1m
    eng.ingest_agg_trade(
        "ETHUSDT", qty=1.0, price=500.0, is_buyer_maker=True, ts=now - 10
    )
    snap = eng.get_cvd_snapshot("ETH-USD")
    assert snap["cvd_1m"] == pytest.approx(-500.0)
    assert snap["cvd_5m"] == pytest.approx(1000.0 - 500.0)


def test_cvd_snapshot_not_ready_before_trades():
    eng = PerpLeadLagEngine(enabled=True, venues="binance", symbols=("BTC-USD",))
    snap = eng.get_cvd_snapshot("BTC-USD")
    assert snap["ready"] is False
    assert snap["cvd_1m"] == 0.0
    assert snap["cvd_5m"] == 0.0


# ---------------------------------------------------------------------------
# B) Liquidation notional short vs long
# ---------------------------------------------------------------------------


def test_liq_short_notional_from_forced_buy():
    eng = PerpLeadLagEngine(enabled=True, venues="binance", symbols=("BTC-USD",))
    now = time.time()
    # Binance forceOrder S=BUY → short liquidated
    eng.ingest_liquidation(
        "BTCUSDT", side="BUY", qty=1.0, price=60_000.0, ts=now - 5
    )
    snap = eng.get_liq_snapshot("BTC-USD")
    assert snap["ready"] is True
    assert snap["short_liq_1m_usd"] == pytest.approx(60_000.0)
    assert snap["long_liq_1m_usd"] == pytest.approx(0.0)


def test_liq_long_notional_from_forced_sell():
    eng = PerpLeadLagEngine(enabled=True, venues="binance", symbols=("ETH-USD",))
    now = time.time()
    eng.ingest_liquidation(
        "ETHUSDT", side="SELL", qty=10.0, price=2_500.0, ts=now - 2
    )
    snap = eng.get_liq_snapshot("ETH-USD")
    assert snap["short_liq_1m_usd"] == pytest.approx(0.0)
    assert snap["long_liq_1m_usd"] == pytest.approx(25_000.0)


def test_liq_legacy_count_still_works_without_side():
    """Existing cascade path: side-less ingest still counts events."""
    eng = PerpLeadLagEngine(
        enabled=True,
        venues="binance",
        symbols=("BTC-USD",),
        liq_min_cluster=3,
        liq_window_sec=10.0,
        signal_ttl_sec=60.0,
    )
    now = time.time()
    for i in range(3):
        eng.ingest_liquidation("BTCUSDT", ts=now - i)
    snap = eng.get_snapshot()
    assert snap.recent_liq_cascade is True
    # No side/notional → liq notional not marked ready
    liq = eng.get_liq_snapshot("BTC-USD")
    assert liq["ready"] is False
    assert liq["short_liq_1m_usd"] == 0.0


def test_short_liq_spike_meets_sweep_threshold():
    eng = PerpLeadLagEngine(enabled=True, venues="binance", symbols=("BTC-USD",))
    now = time.time()
    eng.ingest_liquidation(
        "BTCUSDT", side="BUY", qty=1.0, price=55_000.0, ts=now - 1
    )
    snap = eng.get_liq_snapshot("BTC-USD")
    ok, detail = check_liq_sweep(
        snap["short_liq_1m_usd"],
        min_short_usd=50_000.0,
        enabled=True,
        ready=snap["ready"],
    )
    assert ok is True
    assert "liq_sweep ok" in detail


# ---------------------------------------------------------------------------
# C) Strategy gates
# ---------------------------------------------------------------------------


def test_green_candle_negative_cvd_blocks_entry():
    engine = _engine()
    extras = _sweet_extras(
        setup_bar_open=2490.0,
        setup_bar_close=2500.0,  # green
        cvd_snapshot={
            "cvd_1m": -100.0,
            "cvd_5m": -5000.0,
            "ready": True,
            "updated_at": time.time(),
        },
        liq_snapshot={
            "short_liq_1m_usd": 80_000.0,
            "long_liq_1m_usd": 0.0,
            "ready": True,
        },
    )
    d = engine.reason(AgentObservation(symbol="ETH-USD", indicators=_ind(extras=extras)))
    assert d.action == Action.HOLD
    assert d.reasoning == "cvd_divergence: green candle / negative CVD"


def test_below_short_liq_threshold_blocks_entry():
    engine = _engine()
    extras = _sweet_extras(
        setup_bar_open=2490.0,
        setup_bar_close=2480.0,  # red — CVD divergence N/A
        cvd_snapshot={"cvd_1m": 1.0, "cvd_5m": 1.0, "ready": True},
        liq_snapshot={
            "short_liq_1m_usd": 10_000.0,  # < 50k
            "long_liq_1m_usd": 0.0,
            "ready": True,
        },
    )
    d = engine.reason(AgentObservation(symbol="ETH-USD", indicators=_ind(extras=extras)))
    assert d.action == Action.HOLD
    assert d.reasoning == "liq_sweep: short liq 1m < $50k"


def test_short_liq_above_threshold_and_positive_cvd_allows_buy():
    engine = _engine()
    extras = _sweet_extras(
        setup_bar_open=2490.0,
        setup_bar_close=2500.0,  # green
        cvd_snapshot={"cvd_1m": 1000.0, "cvd_5m": 5000.0, "ready": True},
        liq_snapshot={
            "short_liq_1m_usd": 50_000.0,
            "long_liq_1m_usd": 1_000.0,
            "ready": True,
        },
    )
    d = engine.reason(AgentObservation(symbol="ETH-USD", indicators=_ind(extras=extras)))
    assert d.action == Action.BUY
    assert d.stop_loss is not None


def test_cvd_warmup_fail_closed_blocks_when_not_ready():
    engine = _engine(cvd_warmup_fail_closed=True)
    extras = _sweet_extras(
        cvd_snapshot={"cvd_1m": 0.0, "cvd_5m": 0.0, "ready": False},
        liq_snapshot={"short_liq_1m_usd": 80_000.0, "long_liq_1m_usd": 0.0, "ready": True},
    )
    d = engine.reason(AgentObservation(symbol="ETH-USD", indicators=_ind(extras=extras)))
    assert d.action == Action.HOLD
    assert "CVD not ready" in d.reasoning


def test_gates_disabled_preserve_legacy_buy():
    """Defaults False on engine keep existing sweet-spot tests green."""
    engine = VolumeSweetSpotEngine(
        min_tp_pct=0.025,
        tp2_rr=2.5,
        swing_sl_buffer_pct=0.002,
        cvd_divergence_enabled=False,
        liq_sweep_required=False,
    )
    extras = _sweet_extras(
        cvd_snapshot={"cvd_5m": -999999.0, "ready": True},
        liq_snapshot={"short_liq_1m_usd": 0.0, "ready": True},
    )
    d = engine.reason(AgentObservation(symbol="ETH-USD", indicators=_ind(extras=extras)))
    assert d.action == Action.BUY


def test_engine_reads_leadlag_snapshots_live():
    """Strategy can read non-blocking snapshots from wired PerpLeadLagEngine."""
    ll = PerpLeadLagEngine(enabled=True, venues="binance", symbols=("ETH-USD",))
    now = time.time()
    ll.ingest_agg_trade(
        "ETHUSDT", qty=1.0, price=100.0, is_buyer_maker=False, ts=now - 1
    )
    ll.ingest_liquidation(
        "ETHUSDT", side="BUY", qty=20.0, price=3_000.0, ts=now - 1
    )  # short liq $60k
    engine = _engine(leadlag=ll)
    # No extras snapshots — must come from leadlag
    extras = _sweet_extras()
    extras.pop("cvd_snapshot", None)
    extras.pop("liq_snapshot", None)
    extras["setup_bar_open"] = 2490.0
    extras["setup_bar_close"] = 2500.0  # green; CVD positive → ok
    d = engine.reason(AgentObservation(symbol="ETH-USD", indicators=_ind(extras=extras)))
    assert d.action == Action.BUY


def test_helper_skip_reason_strings():
    ok, detail = check_cvd_divergence(100.0, 101.0, -1.0, enabled=True, ready=True)
    assert ok is False
    assert detail == "cvd_divergence: green candle / negative CVD"
    ok2, detail2 = check_liq_sweep(1.0, min_short_usd=50_000.0, enabled=True, ready=True)
    assert ok2 is False
    assert detail2 == "liq_sweep: short liq 1m < $50k"
