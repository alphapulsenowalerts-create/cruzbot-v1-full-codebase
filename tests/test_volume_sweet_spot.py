"""Volume Sweet Spot strategy: entry gates, SL math, no ATR trail, post-only, 30m stop."""

from __future__ import annotations

from datetime import timedelta

import pytest

from trading_bot.config import Settings
from trading_bot.models import (
    Action,
    AgentObservation,
    Decision,
    IndicatorSnapshot,
    OrderRequest,
    OrderSide,
    OrderType,
    Position,
    utcnow,
)
from trading_bot.risk_manager import RiskManager
from trading_bot.strategy_volume_sweet_spot import (
    VolumeSweetSpotEngine,
    min_tp_clearance_ok,
    structural_stop,
)


def _settings(**kwargs) -> Settings:
    base = dict(
        PAPER_TRADING_MODE=True,
        STRATEGY_MODE="volume_sweet_spot",
        POST_ONLY=True,
        MAX_HOLD_MINUTES=30,
        ACCOUNT_EQUITY=200.0,
        MAX_RISK_PER_TRADE_PCT=0.015,
        MAX_RISK_PER_TRADE_PCT_CEILING=0.02,
        DAILY_DRAWDOWN_LIMIT_PCT=0.03,
        MAX_POSITION_PCT=1.0,
        MAX_NOTIONAL_PER_TRADE_USD=50.0,
        MAX_TOTAL_EXPOSURE_USD=200.0,
        QTY_PRECISION=8,
        ALLOW_PYRAMIDING=False,
        MIN_TP_PCT=0.025,
        SWING_SL_BUFFER_PCT=0.002,
        RVOL_BREAKOUT_MULT=2.0,
        PULLBACK_VOL_FRAC=0.5,
        MAKER_FEE_RATE=0.005,
        COINBASE_API_KEY="",
        COINBASE_API_SECRET="",
        BROKER="coinbase",
    )
    base.update(kwargs)
    return Settings(**base)


def _sweet_ind(**kwargs) -> IndicatorSnapshot:
    base = dict(
        symbol="ETH-USD",
        close=2500.0,
        volume=100.0,  # low pullback vol
        vwap=2495.0,
        rsi=55.0,
        macd=1.0,
        macd_signal=0.5,
        macd_hist=0.4,
        ema_fast=2498.0,
        ema_slow=2490.0,
        atr=10.0,  # ATR present but must NOT drive exits/SL
        ema_cross="none",
        extras={
            "volume_ratio": 2.5,
            "retest_ok": True,
            "breakout_volume": 1000.0,
            "pullback_volume": 100.0,
            "swing_low": 2450.0,
            "delta_proxy": "close>open",
            "breakout_rvol": 2.5,
            "resistance": 2600.0,  # ~4% clear
            "entry_reason": "RVOL Breakout + Low-Volume VWAP Retest",
        },
    )
    base.update(kwargs)
    if "extras" in kwargs:
        extras = dict(base["extras"]) if "extras" not in kwargs else dict(kwargs["extras"])
        # merge carefully when only partial extras passed via kwargs already
        pass
    return IndicatorSnapshot(**base)


def test_reject_entry_on_spike_without_retest():
    engine = VolumeSweetSpotEngine()
    ind = _sweet_ind(
        volume=1000.0,
        extras={
            "volume_ratio": 3.0,
            "on_breakout_spike": True,
            "retest_ok": False,
            "breakout_volume": 1000.0,
            "pullback_volume": 1000.0,
        },
    )
    d = engine.reason(AgentObservation(symbol="ETH-USD", indicators=ind))
    assert d.action == Action.HOLD
    assert "spike" in d.reasoning.lower() or "retest" in d.reasoning.lower()


def test_reject_if_tp_path_below_2_5_pct():
    ok, detail = min_tp_clearance_ok(100.0, 102.0, min_tp_pct=0.025)  # 2%
    assert ok is False
    assert "min_tp" in detail

    engine = VolumeSweetSpotEngine(min_tp_pct=0.025, tp2_rr=2.5)
    # swing very close → tiny risk → TP2 < 2.5%
    ind = _sweet_ind(
        close=100.0,
        vwap=99.9,
        extras={
            "retest_ok": True,
            "breakout_volume": 1000.0,
            "pullback_volume": 100.0,
            "swing_low": 99.5,  # risk ~0.7% after buffer; 2.5R ~1.75% < 2.5%
            "resistance": 110.0,
            "volume_ratio": 2.5,
            "breakout_rvol": 2.5,
        },
    )
    d = engine.reason(AgentObservation(symbol="ETH-USD", indicators=ind))
    assert d.action == Action.HOLD
    assert "min_tp" in d.reasoning


def test_sl_equals_swing_low_times_buffer():
    swing = 2450.0
    sl = structural_stop(swing, 0.002)
    assert sl == pytest.approx(swing * (1 - 0.002))

    engine = VolumeSweetSpotEngine(swing_sl_buffer_pct=0.002, min_tp_pct=0.025, tp2_rr=2.5)
    # Large enough risk for 2.5% TP: risk needs >= 1% of price for 2.5R
    # swing_low = 2400, close=2500 → risk after buffer ≈ 104.8 → TP2 dist 262 → 10.5%
    ind = _sweet_ind(
        close=2500.0,
        extras={
            "retest_ok": True,
            "breakout_volume": 1000.0,
            "pullback_volume": 100.0,
            "swing_low": 2400.0,
            "resistance": 2700.0,
            "volume_ratio": 2.5,
            "breakout_rvol": 2.5,
        },
    )
    d = engine.reason(AgentObservation(symbol="ETH-USD", indicators=ind))
    assert d.action == Action.BUY
    assert d.stop_loss == pytest.approx(2400.0 * (1 - 0.002), rel=1e-6)


def test_no_atr_trail_path_when_sweet_spot_mode():
    settings = _settings()
    assert settings.is_sweet_spot is True
    rm = RiskManager(settings)
    sl, tp, trail = rm.suggest_stops(
        Action.BUY, 2500.0, atr=50.0, explicit_sl=2450.0, explicit_tp=2625.0
    )
    assert trail is None
    assert sl == pytest.approx(2450.0)
    assert tp == pytest.approx(2625.0)

    from main import TradingApp

    app = TradingApp(settings, once=True)
    pos = Position(
        symbol="ETH-USD",
        qty=0.02,
        avg_entry_price=2500.0,
        stop_loss=2450.0,
        take_profit=2625.0,
        take_profit_1=2550.0,
        trail_distance=30.0,  # stale — must be ignored
        trail_high_water=2600.0,
        opened_at=utcnow(),
        tp1_done=False,
        initial_qty=0.02,
    )
    # Mark below trail (2600-30=2570) but above SL — must NOT fire trail
    d = app._check_hard_brackets("ETH-USD", pos, mark=2560.0)
    assert d is None or d.reasoning != "trail"
    # Hit SL
    d_sl = app._check_hard_brackets("ETH-USD", pos, mark=2440.0)
    assert d_sl is not None and d_sl.reasoning == "SL"


def test_post_only_flag_on_orders():
    settings = _settings(POST_ONLY=True)
    rm = RiskManager(settings)
    from trading_bot.models import AccountState

    account = AccountState(equity=200.0, cash=200.0, buying_power=200.0)
    decision = Decision(
        action=Action.BUY,
        symbol="ETH-USD",
        confidence=80,
        stop_loss=2400.0 * 0.998,
        take_profit=2625.0,
        reasoning="test",
    )
    verdict = rm.evaluate(decision, account, entry_price=2500.0, atr=50.0)
    assert verdict.approved is True
    assert verdict.trailing_stop_distance is None
    order = rm.to_order_request(decision, verdict, limit_price=2500.0, paper=True)
    assert order is not None
    assert order.order_type == OrderType.LIMIT
    assert order.post_only is True


def test_30m_time_stop():
    from main import TradingApp

    settings = _settings(MAX_HOLD_MINUTES=30)
    app = TradingApp(settings, once=True)
    opened = utcnow() - timedelta(minutes=31)
    pos = Position(
        symbol="SOL-USD",
        qty=0.3,
        avg_entry_price=150.0,
        market_value=45.0,
        stop_loss=140.0,
        take_profit=160.0,
        opened_at=opened,
        tp1_done=False,
    )
    decision = app._check_hard_brackets("SOL-USD", pos, mark=151.0)
    assert decision is not None
    assert decision.action == Action.SELL
    assert decision.reasoning == "time-stop"


def test_tp1_partial_sell_decision():
    from main import TradingApp

    settings = _settings()
    app = TradingApp(settings, once=True)
    pos = Position(
        symbol="ETH-USD",
        qty=0.02,
        avg_entry_price=2500.0,
        stop_loss=2450.0,
        take_profit=2625.0,
        take_profit_1=2550.0,
        opened_at=utcnow(),
        tp1_done=False,
        initial_qty=0.02,
    )
    d = app._check_hard_brackets("ETH-USD", pos, mark=2551.0)
    assert d is not None
    assert d.reasoning == "TP1"
    assert d.quantity == pytest.approx(0.01)


@pytest.mark.asyncio
async def test_notifier_buy_sell_templates():
    from trading_bot.notifier import Notifier

    sent = []

    class Capture(Notifier):
        async def send(self, message: str, *, title: str = "", extra=None) -> None:
            sent.append(message)

    n = Capture(enabled=True, telegram_bot_token="x", telegram_chat_id="1")
    # Force configured
    await n.trade_entry(
        "ETH-USD",
        "BUY",
        0.02,
        2500.0,
        stop_loss=2450.0,
        take_profit=2625.0,
    )
    assert sent and "🔵 BUY FILLED | ETH-USD" in sent[0]
    assert "Order Type: Limit Maker" in sent[0]
    assert "Total Spent: $50.00" in sent[0]

    sent.clear()
    await n.trade_exit(
        "ETH-USD",
        "SELL",
        0.02,
        reason="TP2",
        price=2600.0,
        entry_price=2500.0,
        pnl=1.50,
        cash_after=194.5,
        equity_after=194.5,
        fee_rate=0.005,
    )
    assert sent and "🟢 PROFIT TAKE | ETH-USD" in sent[0]
    assert "NET P&L: +$1.50 (Fees Deducted)" in sent[0]
    assert "Reason: TP Hit" in sent[0]

    # System noise must not call send
    sent.clear()
    await n.kill_switch("restart")
    await n.circuit_breaker("dd")
    assert sent == []
