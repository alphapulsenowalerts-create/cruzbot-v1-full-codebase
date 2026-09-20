"""Entry quality gates: fee_clearance, RSI cap, post-stop cooldown, volume spike."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trading_bot.agent_core import (
    SetupPreFilter,
    VwapMomentumScalpEngine,
    fee_clearance_ok,
)
from trading_bot.config import Settings
from trading_bot.models import (
    AccountState,
    Action,
    AgentObservation,
    Decision,
    IndicatorSnapshot,
    Position,
)
from trading_bot.risk_manager import RiskManager
from trading_bot.state_store import BehavioralStateStore


def _bull_ind(**kwargs) -> IndicatorSnapshot:
    base = dict(
        symbol="BTC-USD",
        close=100.0,
        volume=5_000.0,
        vwap=99.9,
        rsi=55.0,
        macd=1.0,
        macd_signal=0.5,
        macd_hist=0.4,
        ema_fast=100.5,
        ema_slow=99.0,
        atr=3.0,  # 1.5*3/100 = 4.5% > 3.6% fee gate
        ema_cross="bullish",
        extras={"volume_ratio": 3.0},
    )
    base.update(kwargs)
    return IndicatorSnapshot(**base)


def test_fee_clearance_ok_when_tp_clears_fees():
    ok, detail = fee_clearance_ok(
        100.0,
        3.0,
        take_profit_atr_mult=1.5,
        taker_fee_rate=0.009,
        fee_clear_mult=2.0,
    )
    assert ok is True
    assert "fee_clearance ok" in detail


def test_fee_clearance_skip_tiny_atr():
    ok, detail = fee_clearance_ok(
        100.0,
        0.5,  # 1.5*0.5/100 = 0.75% << 3.6%
        take_profit_atr_mult=1.5,
        taker_fee_rate=0.009,
        fee_clear_mult=2.0,
    )
    assert ok is False
    assert detail.startswith("fee_clearance")


def test_engine_holds_on_fee_clearance():
    engine = VwapMomentumScalpEngine(min_confidence=50)
    ind = _bull_ind(atr=0.4)  # tiny ATR → structural -EV
    d = engine.reason(AgentObservation(symbol="BTC-USD", indicators=ind))
    assert d.action == Action.HOLD
    assert "fee_clearance" in d.reasoning


def test_engine_buy_when_fee_clears():
    engine = VwapMomentumScalpEngine(min_confidence=50)
    d = engine.reason(AgentObservation(symbol="BTC-USD", indicators=_bull_ind()))
    assert d.action == Action.BUY
    assert "fee_clearance ok" in d.reasoning


def test_rsi_block_overbought():
    engine = VwapMomentumScalpEngine(min_confidence=50, rsi_buy_cap=72)
    ind = _bull_ind(rsi=75.0)
    d = engine.reason(AgentObservation(symbol="BTC-USD", indicators=ind))
    assert d.action == Action.HOLD
    assert "RSI overbought" in d.reasoning or "overbought" in d.reasoning.lower()


def test_momentum_requires_ema_and_macd():
    engine = VwapMomentumScalpEngine(min_confidence=50)
    ind = _bull_ind(ema_fast=98.0, ema_slow=100.0, macd_hist=0.4)
    d = engine.reason(AgentObservation(symbol="BTC-USD", indicators=ind))
    assert d.action == Action.HOLD
    assert "EMA" in d.reasoning

    ind2 = _bull_ind(macd_hist=-0.2)
    d2 = engine.reason(AgentObservation(symbol="BTC-USD", indicators=ind2))
    assert d2.action == Action.HOLD
    assert "MACD" in d2.reasoning


def test_volume_spike_prefilter_default_2_75():
    pf = SetupPreFilter(vwap_boundary_pct=0.002, volume_spike_mult=2.75)
    # vol 2.5 < 2.75 → skip
    obs = AgentObservation(
        symbol="BTC-USD",
        indicators=_bull_ind(close=100.05, vwap=100.0, extras={"volume_ratio": 2.5}),
    )
    ok, detail = pf.evaluate(obs)
    assert ok is False
    assert "volume spike" in detail

    obs2 = AgentObservation(
        symbol="BTC-USD",
        indicators=_bull_ind(close=100.05, vwap=100.0, extras={"volume_ratio": 2.8}),
    )
    ok2, detail2 = pf.evaluate(obs2)
    assert ok2 is True
    assert "vol_ratio" in detail2


def test_prefilter_requires_vwap_reclaim():
    pf = SetupPreFilter(vwap_boundary_pct=0.002, volume_spike_mult=2.0)
    # below VWAP → skip
    obs = AgentObservation(
        symbol="BTC-USD",
        indicators=_bull_ind(close=99.5, vwap=100.0, extras={"volume_ratio": 3.0}),
    )
    ok, detail = pf.evaluate(obs)
    assert ok is False
    assert "reclaim" in detail.lower() or "VWAP" in detail

    # recent reclaim cross even if slightly farther (still above)
    obs2 = AgentObservation(
        symbol="BTC-USD",
        indicators=_bull_ind(
            close=100.5,
            vwap=100.0,
            extras={
                "volume_ratio": 3.0,
                "vwap_reclaim_bars": 2,
                "vwap_crossed_up": True,
            },
        ),
    )
    ok2, _ = pf.evaluate(obs2)
    assert ok2 is True


def test_post_stop_cooldown(tmp_path: Path):
    db = str(tmp_path / "state.db")
    store = BehavioralStateStore(db, post_stop_cooldown_min=15)
    now = datetime(2026, 9, 18, 14, 0, tzinfo=timezone.utc)
    assert store.in_post_stop_cooldown("SOL-USD", now=now) is False
    store.record_post_stop_cooldown("SOL-USD", now=now, minutes=15)
    assert store.in_post_stop_cooldown("SOL-USD", now=now) is True
    rem = store.post_stop_cooldown_remaining("SOL-USD", now=now)
    assert rem > 0
    assert rem <= 15 * 60 + 1
    # Still blocked before expiry
    almost = now + timedelta(minutes=14, seconds=59)
    assert store.in_post_stop_cooldown("SOL-USD", now=almost) is True
    later = now + timedelta(minutes=15, seconds=1)
    assert store.in_post_stop_cooldown("SOL-USD", now=later) is False


def test_post_stop_cooldown_skip_log_shape():
    from trading_bot.utils.decision_filters import format_post_stop_cooldown_skip

    msg = format_post_stop_cooldown_skip("XRP-USD", 900.0)
    assert msg == "[SKIP] XRP-USD: Signal active but cooling down for another 15 minutes."
    msg2 = format_post_stop_cooldown_skip("BTC-USD", 61.0)
    assert msg2 == "[SKIP] BTC-USD: Signal active but cooling down for another 2 minutes."
    msg3 = format_post_stop_cooldown_skip("ETH-USD", 30.0)
    assert msg3 == "[SKIP] ETH-USD: Signal active but cooling down for another 1 minutes."


def test_max_concurrent_positions_blocks_buy():
    settings = Settings(
        PAPER_TRADING_MODE=True,
        ACCOUNT_EQUITY=1600.0,
        MAX_NOTIONAL_PER_TRADE_USD=100.0,
        MAX_TOTAL_EXPOSURE_USD=1000.0,
        MAX_POSITION_PCT=1.0,
        MAX_CONCURRENT_POSITIONS=2,
        QTY_PRECISION=8,
    )
    rm = RiskManager(settings)
    account = AccountState(equity=1600.0, cash=1000.0, buying_power=1000.0)
    decision = Decision(
        action=Action.BUY,
        symbol="ETH-USD",
        confidence=80,
        reasoning="test",
    )
    # ATR sized so fee clearance not relevant here (risk path)
    verdict = rm.evaluate(
        decision,
        account,
        entry_price=100.0,
        atr=3.0,
        open_exposure_usd=80.0,
        open_position_count=2,
    )
    assert verdict.approved is False
    assert "max concurrent" in verdict.reason
