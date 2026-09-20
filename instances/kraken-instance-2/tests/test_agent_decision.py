"""Agent decision JSON shape + HOLD on bad input + VWAP scalp engine."""

from __future__ import annotations

from trading_bot.agent_core import (
    AgentCore,
    RuleBasedSignalEngine,
    VwapMomentumScalpEngine,
    parse_decision,
)
from trading_bot.models import Action, AgentObservation, Decision, IndicatorSnapshot, Position


def test_parse_valid_decision_shape():
    raw = {
        "action": "BUY",
        "symbol": "aapl",
        "confidence": 72.5,
        "stop_loss": 99.0,
        "take_profit": 105.0,
        "reasoning": "EMA cross + RSI rebound",
    }
    d = parse_decision(raw, "AAPL")
    assert isinstance(d, Decision)
    assert d.action == Action.BUY
    assert d.symbol == "AAPL"
    assert d.confidence == 72.5
    assert d.stop_loss == 99.0
    assert d.take_profit == 105.0


def test_parse_json_string():
    raw = '{"action":"HOLD","symbol":"MSFT","confidence":0,"stop_loss":null,"take_profit":null,"reasoning":"wait"}'
    d = parse_decision(raw, "MSFT")
    assert d.action == Action.HOLD
    assert d.symbol == "MSFT"


def test_hold_on_bad_json():
    d = parse_decision("not-json-at-all", "SPY")
    assert d.action == Action.HOLD
    assert "parse failure" in d.reasoning.lower() or "failure" in d.reasoning.lower()


def test_hold_on_invalid_confidence():
    d = parse_decision(
        {"action": "BUY", "symbol": "SPY", "confidence": 150, "reasoning": "bad"},
        "SPY",
    )
    assert d.action == Action.HOLD


def test_hold_on_unknown_action():
    d = parse_decision(
        {"action": "YOLO", "symbol": "SPY", "confidence": 50, "reasoning": "nope"},
        "SPY",
    )
    assert d.action == Action.HOLD


def test_rule_engine_emits_valid_decision():
    engine = RuleBasedSignalEngine(min_confidence=10, fee_clear_mult=0.1)
    ind = IndicatorSnapshot(
        symbol="AAPL",
        close=100.0,
        volume=1_000_000,
        vwap=99.0,
        rsi=55.0,
        macd=0.5,
        macd_signal=0.2,
        macd_hist=0.3,
        ema_fast=101.0,
        ema_slow=99.0,
        atr=1.2,
        ema_cross="bullish",
        extras={"volume_ratio": 1.4},
    )
    obs = AgentObservation(symbol="AAPL", indicators=ind)
    d = engine.reason(obs)
    assert d.action in (Action.BUY, Action.SELL, Action.HOLD)
    assert 0 <= d.confidence <= 100
    # round-trip validation
    assert parse_decision(d.model_dump(), "AAPL").action == d.action


def test_vwap_scalp_buy_above_vwap():
    """Bullish-above-VWAP observation → BUY with 'VWAP scalp' in reasoning."""
    # ATR large enough to clear ~3.6% fee gate (1.5*ATR/price >= 3.6%)
    engine = VwapMomentumScalpEngine(min_confidence=50)
    ind = IndicatorSnapshot(
        symbol="BTC-USD",
        close=50_100.0,
        volume=2_500.0,
        vwap=50_000.0,
        rsi=58.0,
        macd=12.0,
        macd_signal=8.0,
        macd_hist=4.0,
        ema_fast=50_050.0,
        ema_slow=49_900.0,
        atr=1_500.0,
        ema_cross="bullish",
        extras={"volume_ratio": 1.35, "macd_hist_prev": -1.0},
    )
    obs = AgentObservation(symbol="BTC-USD", indicators=ind)
    d = engine.reason(obs)
    assert d.action == Action.BUY
    assert "VWAP scalp" in d.reasoning
    assert d.stop_loss is not None
    assert d.take_profit is not None
    assert d.take_profit > d.stop_loss


def test_vwap_scalp_no_buy_below_vwap():
    """Below-VWAP must not BUY even with otherwise bullish momentum."""
    engine = VwapMomentumScalpEngine(min_confidence=50)
    ind = IndicatorSnapshot(
        symbol="ETH-USD",
        close=3_400.0,
        volume=8_000.0,
        vwap=3_450.0,  # close < VWAP — hard filter
        rsi=52.0,
        macd=5.0,
        macd_signal=2.0,
        macd_hist=3.0,
        ema_fast=3_420.0,
        ema_slow=3_380.0,
        atr=25.0,
        ema_cross="bullish",
        extras={"volume_ratio": 1.5},
    )
    obs = AgentObservation(symbol="ETH-USD", indicators=ind)
    d = engine.reason(obs)
    assert d.action != Action.BUY
    assert "VWAP scalp" in d.reasoning


def test_agent_default_uses_vwap_engine():
    agent = AgentCore()
    assert isinstance(agent.signal_engine, VwapMomentumScalpEngine)


def test_agent_suppresses_sell_when_flat():
    agent = AgentCore(signal_engine=RuleBasedSignalEngine(min_confidence=10), require_prefilter=False)
    ind = IndicatorSnapshot(
        symbol="AAPL",
        close=100.0,
        volume=1_000_000,
        vwap=101.0,
        rsi=70.0,
        macd=-0.5,
        macd_signal=-0.2,
        macd_hist=-0.3,
        ema_fast=98.0,
        ema_slow=100.0,
        atr=1.2,
        ema_cross="bearish",
        extras={"volume_ratio": 0.6},
    )
    obs = AgentObservation(symbol="AAPL", indicators=ind, position=None)
    d = agent.decide_sync(obs)
    # Strong sell score but flat → HOLD
    assert d.action == Action.HOLD


def test_agent_allows_sell_with_position():
    agent = AgentCore(signal_engine=RuleBasedSignalEngine(min_confidence=10), require_prefilter=False)
    ind = IndicatorSnapshot(
        symbol="AAPL",
        close=100.0,
        volume=1_000_000,
        vwap=101.0,
        rsi=72.0,
        macd=-0.5,
        macd_signal=-0.1,
        macd_hist=-0.4,
        ema_fast=97.0,
        ema_slow=101.0,
        atr=1.5,
        ema_cross="bearish",
        extras={"volume_ratio": 0.5},
    )
    pos = Position(symbol="AAPL", qty=5, avg_entry_price=98.0, market_value=500.0)
    obs = AgentObservation(symbol="AAPL", indicators=ind, position=pos)
    d = agent.decide_sync(obs)
    assert d.action in (Action.SELL, Action.HOLD)  # sell if score strong enough
    if d.action == Action.SELL:
        assert d.symbol == "AAPL"
        assert "VWAP scalp" in d.reasoning
