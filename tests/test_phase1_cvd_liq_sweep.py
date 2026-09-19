"""Phase 1: CVD divergence + short-liquidation sweep gates."""

from __future__ import annotations

import time

import pandas as pd
import pytest

from trading_bot.cvd import (
    REASON_CVD_ABSORPTION,
    REASON_CVD_BYPASS,
    REASON_CVD_COLD,
    REASON_LIQ_BYPASS,
    REASON_LIQ_COLD,
    REASON_LIQ_NO_SPIKE,
    CvdTracker,
    check_cvd_divergence,
    check_short_liq_sweep,
    evaluate_phase1_long_gates,
    is_short_liquidation,
    last_closed_5m_candle,
    period_start_ts,
    signed_cvd_delta,
)
from trading_bot.derivatives_feed import PerpLeadLagEngine
from trading_bot.models import Action, AgentObservation, HigherTimeframeContext, IndicatorSnapshot
from trading_bot.strategy_volume_sweet_spot import VolumeSweetSpotEngine


def test_signed_cvd_taker_buy_vs_sell():
    # Buyer-maker=False → taker buy → positive
    assert signed_cvd_delta(1.0, 100.0, is_buyer_maker=False) == 100.0
    # Buyer-maker=True → taker sell → negative
    assert signed_cvd_delta(2.0, 50.0, is_buyer_maker=True) == -100.0
    assert signed_cvd_delta(0.0, 100.0, is_buyer_maker=False) == 0.0


def test_short_liquidation_side():
    assert is_short_liquidation("BUY") is True
    assert is_short_liquidation("Buy") is True
    assert is_short_liquidation("SELL") is False
    assert is_short_liquidation(None) is False


def test_cvd_tracker_period_roll_and_cumulative():
    tr = CvdTracker(period_sec=300, keep_periods=4)
    t0 = 1_700_000_000.0
    p0 = period_start_ts(t0, 300)
    # Taker buys in first period
    tr.ingest("BTC-USD", qty=1.0, price=100.0, is_buyer_maker=False, ts=p0 + 1)
    tr.ingest("BTC-USD", qty=1.0, price=50.0, is_buyer_maker=True, ts=p0 + 2)
    snap = tr.snapshot("BTC-USD")
    assert snap.warm is True
    assert snap.trade_count == 2
    assert snap.cumulative == pytest.approx(50.0)
    assert tr.period_delta("BTC-USD", p0) == pytest.approx(50.0)

    # Next period rolls the first into completed
    p1 = p0 + 300
    tr.ingest("BTC-USD", qty=1.0, price=10.0, is_buyer_maker=True, ts=p1 + 1)
    assert tr.period_delta("BTC-USD", p0) == pytest.approx(50.0)
    assert tr.period_delta("BTC-USD", p1) == pytest.approx(-10.0)
    assert tr.snapshot("BTC-USD").cumulative == pytest.approx(40.0)


def test_cvd_ingest_is_o1_non_blocking():
    """WS-callback path: 20k updates stay bounded and finish quickly (no I/O)."""
    tr = CvdTracker(period_sec=300, keep_periods=8)
    t0 = 1_700_000_000.0
    started = time.perf_counter()
    for i in range(20_000):
        tr.ingest(
            "ETH-USD",
            qty=0.01,
            price=2000.0,
            is_buyer_maker=(i % 2 == 0),
            ts=t0 + i * 0.05,
        )
    elapsed = time.perf_counter() - started
    assert elapsed < 0.75
    snap = tr.snapshot("ETH-USD")
    assert snap.trade_count == 20_000
    # Keep-periods bound: completed map never grows without bound
    assert len(tr._completed.get("ETH-USD", {})) <= 8


def test_cvd_divergence_blocks_green_negative():
    ok, reason = check_cvd_divergence(
        100.0, 101.0, -25.0, enabled=True, feed_warm=True
    )
    assert ok is False
    assert reason == REASON_CVD_ABSORPTION


def test_cvd_divergence_allows_green_positive():
    ok, reason = check_cvd_divergence(
        100.0, 101.0, 12.0, enabled=True, feed_warm=True
    )
    assert ok is True
    assert reason == ""


def test_cvd_divergence_ignores_red_candle():
    ok, _ = check_cvd_divergence(100.0, 99.0, -80.0, enabled=True, feed_warm=True)
    assert ok is True


def test_cvd_cold_fail_closed_and_warmup_bypass():
    ok, reason = check_cvd_divergence(
        None, None, None, enabled=True, feed_warm=False, fail_closed=True
    )
    assert ok is False
    assert reason == REASON_CVD_COLD

    ok2, reason2 = check_cvd_divergence(
        None,
        None,
        None,
        enabled=True,
        feed_warm=False,
        fail_closed=True,
        allow_cold_feed=True,
    )
    assert ok2 is True
    assert reason2 == REASON_CVD_BYPASS

    ok3, _ = check_cvd_divergence(
        None, None, None, enabled=True, feed_warm=False, fail_closed=False
    )
    assert ok3 is True


def test_liq_sweep_requires_short_notional_spike():
    ok, reason = check_short_liq_sweep(
        10_000.0, threshold=50_000.0, enabled=True, feed_warm=True
    )
    assert ok is False
    assert reason == REASON_LIQ_NO_SPIKE

    ok2, _ = check_short_liq_sweep(
        50_000.0, threshold=50_000.0, enabled=True, feed_warm=True
    )
    assert ok2 is True

    ok3, _ = check_short_liq_sweep(
        1_000.0, threshold=50_000.0, enabled=True, feed_warm=True, spike_latched=True
    )
    assert ok3 is True


def test_liq_sweep_cold_fail_closed_and_bypass():
    ok, reason = check_short_liq_sweep(
        None, enabled=True, feed_warm=False, fail_closed=True
    )
    assert ok is False
    assert reason == REASON_LIQ_COLD
    ok2, reason2 = check_short_liq_sweep(
        None, enabled=True, feed_warm=False, allow_cold_feed=True
    )
    assert ok2 is True
    assert reason2 == REASON_LIQ_BYPASS


def test_phase1_compose_divergence_wins():
    ok, reason = evaluate_phase1_long_gates(
        candle_open=100.0,
        candle_close=101.0,
        period_cvd_delta=-5.0,
        short_liq_notional=80_000.0,
        cvd_feed_warm=True,
        liq_feed_warm=True,
    )
    assert ok is False
    assert reason == REASON_CVD_ABSORPTION


def test_last_closed_5m_skips_forming_bar():
    period = 300
    p0 = period_start_ts(1_700_000_000, period)
    p1 = p0 + period
    now = p1 + 10  # second bar still forming
    df = pd.DataFrame(
        {
            "open": [100.0, 101.0],
            "close": [101.0, 101.5],
        },
        index=pd.to_datetime([p0, p1], unit="s", utc=True),
    )
    candle = last_closed_5m_candle(df, now=now, period_sec=period)
    assert candle is not None
    o, c, ps = candle
    assert o == 100.0
    assert c == 101.0
    assert ps == p0


def _engine() -> PerpLeadLagEngine:
    return PerpLeadLagEngine(
        enabled=True,
        venues="binance",
        symbols=("BTC-USD",),
        sweep_mult=3.0,
        sweep_window_sec=5.0,
        signal_ttl_sec=30.0,
        short_liq_window_sec=60.0,
        short_liq_notional_threshold=50_000.0,
        short_liq_ttl_sec=30.0,
        cvd_period_sec=300.0,
    )


def test_engine_cvd_from_agg_trades_and_liq_notional():
    eng = _engine()
    p0 = period_start_ts(1_700_000_000.0, 300)
    # Taker sells dominate → negative period CVD
    for _ in range(5):
        eng.ingest_agg_trade(
            "BTCUSDT", qty=1.0, price=100.0, is_buyer_maker=True, ts=p0 + 1
        )
    snap = eng.get_cvd_snapshot("BTC-USD")
    assert snap.warm is True
    assert snap.period_delta == pytest.approx(-500.0)

    # Long liq (SELL) must not count toward short-liq spike
    eng.ingest_liquidation(
        "BTCUSDT", side="SELL", qty=10.0, price=10_000.0, ts=p0 + 2
    )
    assert eng.short_liq_notional("BTC-USD", now=p0 + 3) == 0.0

    # Short liq (BUY) $60k
    eng.ingest_liquidation(
        "BTCUSDT", side="BUY", qty=1.0, price=60_000.0, ts=p0 + 4
    )
    assert eng.short_liq_notional("BTC-USD", now=p0 + 5) == pytest.approx(60_000.0)
    assert eng.has_short_liq_spike("BTC-USD", now=p0 + 5) is True

    # Rolling window evicts after 60s
    assert eng.short_liq_notional("BTC-USD", now=p0 + 70) == pytest.approx(0.0)


def test_engine_legacy_ingest_liquidation_still_cascades():
    eng = PerpLeadLagEngine(
        enabled=True,
        venues="binance",
        symbols=("ETH-USD",),
        liq_window_sec=10.0,
        liq_min_cluster=5,
        signal_ttl_sec=60.0,
    )
    t = time.time()
    for i in range(5):
        eng.ingest_liquidation("ETHUSDT", ts=t + i * 0.1)
    snap = eng.get_snapshot()
    assert snap.recent_liq_cascade is True
    assert "ETH-USD" in snap.liq_symbols


def test_engine_phase1_blocks_absorption_even_with_liq_spike():
    eng = _engine()
    p0 = period_start_ts(1_700_100_000.0, 300)
    # Negative CVD on green 5m
    eng.ingest_agg_trade(
        "BTCUSDT", qty=2.0, price=100.0, is_buyer_maker=True, ts=p0 + 1
    )
    eng.ingest_liquidation(
        "BTCUSDT", side="BUY", qty=1.0, price=60_000.0, ts=p0 + 2
    )
    ok, reason = eng.evaluate_phase1_long(
        "BTC-USD",
        candle_open=100.0,
        candle_close=101.0,
        candle_period_start=p0,
        cvd_enabled=True,
        liq_enabled=True,
        fail_closed=True,
        now=p0 + 3,
    )
    assert ok is False
    assert reason == REASON_CVD_ABSORPTION


def test_engine_phase1_allows_when_cvd_ok_and_short_liq_spike():
    eng = _engine()
    p0 = period_start_ts(1_700_200_000.0, 300)
    eng.ingest_agg_trade(
        "BTCUSDT", qty=1.0, price=100.0, is_buyer_maker=False, ts=p0 + 1
    )
    eng.ingest_liquidation(
        "BTCUSDT", side="BUY", qty=2.0, price=30_000.0, ts=p0 + 2
    )
    ok, reason = eng.evaluate_phase1_long(
        "BTC-USD",
        candle_open=100.0,
        candle_close=101.0,
        candle_period_start=p0,
        now=p0 + 3,
    )
    assert ok is True
    assert reason == ""


def test_engine_phase1_blocks_missing_short_liq_spike():
    eng = _engine()
    p0 = period_start_ts(1_700_300_000.0, 300)
    eng.ingest_agg_trade(
        "BTCUSDT", qty=1.0, price=100.0, is_buyer_maker=False, ts=p0 + 1
    )
    ok, reason = eng.evaluate_phase1_long(
        "BTC-USD",
        candle_open=100.0,
        candle_close=101.0,
        candle_period_start=p0,
        now=p0 + 2,
    )
    assert ok is False
    assert reason == REASON_LIQ_NO_SPIKE


def test_engine_phase1_cold_bypass_for_paper_warmup():
    eng = _engine()
    ok, reason = eng.evaluate_phase1_long(
        "BTC-USD",
        candle_open=None,
        candle_close=None,
        candle_period_start=None,
        fail_closed=True,
        allow_cold_feed=True,
    )
    assert ok is True
    assert "bypass" in reason


def test_engine_phase1_cold_fail_closed_default():
    eng = _engine()
    ok, reason = eng.evaluate_phase1_long(
        "BTC-USD",
        candle_open=None,
        candle_close=None,
        candle_period_start=None,
        fail_closed=True,
        allow_cold_feed=False,
    )
    assert ok is False
    assert "cold" in reason


def test_sweet_spot_engine_honors_phase1_extras():
    eng = VolumeSweetSpotEngine(
        l2_imbalance_enabled=False,
        regime_filter_enabled=False,
        mtf_align_enabled=False,
        phase1_gate_enabled=True,
    )
    extras = {
        "retest_ok": True,
        "breakout_volume": 1000.0,
        "pullback_volume": 100.0,
        "swing_low": 2450.0,
        "resistance": 2600.0,
        "phase1_allow": False,
        "phase1_reason": REASON_CVD_ABSORPTION,
    }
    ind = IndicatorSnapshot(
        symbol="ETH-USD",
        close=2500.0,
        volume=100.0,
        vwap=2495.0,
        rsi=55.0,
        ema_fast=2498.0,
        ema_slow=2490.0,
        atr=10.0,
        extras=extras,
    )
    htf = HigherTimeframeContext(ema_200=2400.0, ema_200_1h=2400.0, ema_200_4h=2300.0)
    d = eng.reason(AgentObservation(symbol="ETH-USD", indicators=ind, htf=htf))
    assert d.action == Action.HOLD
    assert REASON_CVD_ABSORPTION in d.reasoning


def test_binance_force_order_short_liq_parse_fields():
    """Mock Binance forceOrder payload fields (S=BUY → short liq)."""
    eng = _engine()
    o = {"s": "BTCUSDT", "S": "BUY", "q": "0.5", "ap": "120000", "T": 1_700_400_000_000}
    eng.ingest_liquidation(
        str(o["s"]),
        ts=float(o["T"]) / 1000.0,
        side=o["S"],
        qty=float(o["q"]),
        price=float(o["ap"]),
    )
    assert eng.short_liq_notional("BTC-USD", now=1_700_400_001.0) == pytest.approx(60_000.0)


def test_bybit_all_liquidation_short_liq_parse_fields():
    eng = _engine()
    row = {"s": "BTCUSDT", "S": "Buy", "v": "1.0", "p": "55000", "T": 1_700_500_000_000}
    eng.ingest_liquidation(
        str(row["s"]),
        ts=float(row["T"]) / 1000.0,
        side=row["S"],
        qty=float(row["v"]),
        price=float(row["p"]),
    )
    assert eng.short_liq_notional("BTC-USD", now=1_700_500_001.0) == pytest.approx(55_000.0)
    assert eng.has_short_liq_spike("BTC-USD", now=1_700_500_001.0) is True


def test_settings_phase1_defaults():
    from trading_bot.config import Settings

    s = Settings(
        COINBASE_API_KEY="",
        COINBASE_API_SECRET="",
        CVD_GATE_ENABLED=True,
        LIQ_SWEEP_GATE_ENABLED=True,
        PHASE1_FAIL_CLOSED=True,
        PHASE1_ALLOW_COLD_FEED=False,
    )
    assert s.cvd_gate_enabled is True
    assert s.cvd_period_sec == 300.0
    assert s.liq_sweep_gate_enabled is True
    assert s.liq_sweep_window_sec == 60.0
    assert s.liq_sweep_notional_usd == 50_000.0
    assert s.phase1_fail_closed is True
    assert s.phase1_allow_cold_feed is False


def test_trading_app_phase1_gate_fail_closed_and_warmup(tmp_path, monkeypatch):
    """Blocked BUY never reaches the executor: cold tape fail-closed + warmup bypass."""
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("BROKER", "mock")
    monkeypatch.setenv("PAPER_TRADING_MODE", "true")
    monkeypatch.setenv("TELEGRAM_COMMANDS_ENABLED", "false")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "p1.db"))
    monkeypatch.setenv("CVD_GATE_ENABLED", "true")
    monkeypatch.setenv("LIQ_SWEEP_GATE_ENABLED", "true")
    monkeypatch.setenv("PHASE1_FAIL_CLOSED", "true")
    monkeypatch.setenv("PHASE1_ALLOW_COLD_FEED", "false")

    from trading_bot.config import reload_settings
    from main import TradingApp

    settings = reload_settings()
    app = TradingApp(settings, once=True)
    ok, reason = app._phase1_allow_buy("BTC-USD")
    assert ok is False
    assert "cold" in reason

    object.__setattr__(app.settings, "phase1_allow_cold_feed", True)
    ok2, reason2 = app._phase1_allow_buy("BTC-USD")
    assert ok2 is True
    assert "bypass" in reason2

    # Seed tape + short-liq spike + matching 5m CVD → allow
    object.__setattr__(app.settings, "phase1_allow_cold_feed", False)
    now = time.time()
    p0 = period_start_ts(now - 400.0, 300)
    app.leadlag.ingest_agg_trade(
        "BTCUSDT", qty=1.0, price=100.0, is_buyer_maker=False, ts=p0 + 1
    )
    app.leadlag.ingest_liquidation(
        "BTCUSDT", side="BUY", qty=1.0, price=60_000.0, ts=now - 5.0
    )
    app._phase1_closed_5m = lambda _sym: (100.0, 101.0, p0)  # type: ignore[method-assign]
    ok3, reason3 = app._phase1_allow_buy("BTC-USD")
    assert ok3 is True, reason3
    assert reason3 == ""
