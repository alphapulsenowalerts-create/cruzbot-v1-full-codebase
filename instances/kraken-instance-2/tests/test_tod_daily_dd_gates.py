"""Time-of-day + UTC-day SQLite PnL circuit breaker gates."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trading_bot.models import (
    Action,
    AgentObservation,
    IndicatorSnapshot,
)
from trading_bot.strategy_volume_sweet_spot import (
    VolumeSweetSpotEngine,
    check_daily_drawdown_circuit,
    check_time_of_day_gate,
)


def test_tod_blocks_2300_to_0030_utc():
    ok, detail = check_time_of_day_gate(
        datetime(2026, 9, 19, 23, 0, tzinfo=timezone.utc), enabled=True
    )
    assert ok is False
    assert "tod_gate" in detail

    ok, _ = check_time_of_day_gate(
        datetime(2026, 9, 19, 23, 45, tzinfo=timezone.utc), enabled=True
    )
    assert ok is False

    ok, _ = check_time_of_day_gate(
        datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc), enabled=True
    )
    assert ok is False

    ok, _ = check_time_of_day_gate(
        datetime(2026, 9, 20, 0, 29, tzinfo=timezone.utc), enabled=True
    )
    assert ok is False

    ok, detail = check_time_of_day_gate(
        datetime(2026, 9, 20, 0, 30, tzinfo=timezone.utc), enabled=True
    )
    assert ok is True
    assert "tod_gate ok" in detail

    ok, _ = check_time_of_day_gate(
        datetime(2026, 9, 19, 22, 59, tzinfo=timezone.utc), enabled=True
    )
    assert ok is True


def test_daily_dd_trips_at_3_percent_sqlite_pnl():
    ok, detail = check_daily_drawdown_circuit(-48.0, 1600.0, limit_pct=0.03, enabled=True)
    assert ok is False
    assert "daily_dd_circuit" in detail

    ok, _ = check_daily_drawdown_circuit(-47.9, 1600.0, limit_pct=0.03, enabled=True)
    assert ok is True

    ok, _ = check_daily_drawdown_circuit(None, 1600.0, enabled=True)
    assert ok is True


def _sweet_extras(**over):
    ex = {
        "volume_ratio": 2.5,
        "retest_ok": True,
        "breakout_volume": 1000.0,
        "pullback_volume": 100.0,
        "swing_low": 2450.0,
        "delta_proxy": "close>open",
        "breakout_rvol": 2.5,
        "resistance": 2600.0,
        "entry_reason": "RVOL Breakout + Low-Volume VWAP Retest",
        "now_utc": "2026-09-19T12:00:00+00:00",
        "utc_day_pnl": 0.0,
        "day_start_equity": 1600.0,
    }
    ex.update(over)
    return ex


def _obs(extras):
    ind = IndicatorSnapshot(
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
        extras=extras,
    )
    return AgentObservation(symbol="ETH-USD", indicators=ind, account=None, position=None, quote=None)


def test_engine_holds_on_tod_when_enabled():
    eng = VolumeSweetSpotEngine(tod_gate_enabled=True, daily_dd_sqlite_enabled=False)
    d = eng.reason(_obs(_sweet_extras(now_utc="2026-09-19T23:15:00+00:00")))
    assert d.action == Action.HOLD
    assert "tod_gate" in d.reasoning


def test_engine_holds_on_sqlite_dd_when_enabled():
    eng = VolumeSweetSpotEngine(tod_gate_enabled=False, daily_dd_sqlite_enabled=True)
    d = eng.reason(
        _obs(_sweet_extras(utc_day_pnl=-56.0, day_start_equity=1600.0))
    )
    assert d.action == Action.HOLD
    assert "daily_dd_circuit" in d.reasoning


def test_engine_buy_when_gates_pass():
    eng = VolumeSweetSpotEngine(tod_gate_enabled=True, daily_dd_sqlite_enabled=True)
    d = eng.reason(_obs(_sweet_extras()))
    assert d.action == Action.BUY


def test_engine_skips_tod_when_disable_switch_true():
    eng = VolumeSweetSpotEngine(
        tod_gate_enabled=True,
        disable_tod_gate=True,
        daily_dd_sqlite_enabled=False,
    )
    d = eng.reason(_obs(_sweet_extras(now_utc="2026-09-19T23:15:00+00:00")))
    assert d.action == Action.BUY
    assert "tod_gate" not in d.reasoning
