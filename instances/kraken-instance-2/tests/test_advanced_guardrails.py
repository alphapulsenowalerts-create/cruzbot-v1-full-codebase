"""Unit tests for hybrid pre-filter, revenge lockout, macro pause, notifier, memory."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trading_bot.agent_core import AgentCore, SetupPreFilter, VwapMomentumScalpEngine
from trading_bot.macro_calendar import (
    MacroEvent,
    MacroGuard,
    MockCalendarAdapter,
    in_pause_window,
)
from trading_bot.models import (
    Action,
    AgentObservation,
    IndicatorSnapshot,
    Position,
)
from trading_bot.notifier import Notifier
from trading_bot.state_store import BehavioralStateStore, DEFAULT_MEMORY_SIZE


def _ind(
    *,
    close: float = 100.0,
    vwap: float = 100.0,
    volume_ratio: float = 2.5,
    **kwargs,
) -> IndicatorSnapshot:
    base = dict(
        symbol="BTC-USD",
        close=close,
        volume=1000.0,
        vwap=vwap,
        rsi=55.0,
        macd=1.0,
        macd_signal=0.5,
        macd_hist=0.5,
        ema_fast=100.5,
        ema_slow=99.5,
        atr=1.0,
        ema_cross="bullish",
        extras={"volume_ratio": volume_ratio},
    )
    base.update(kwargs)
    return IndicatorSnapshot(**base)


# --- 6. Pre-filter gate ---


def test_prefilter_pass_near_vwap_and_volume_spike():
    pf = SetupPreFilter(vwap_boundary_pct=0.002, volume_spike_mult=2.0)
    obs = AgentObservation(symbol="BTC-USD", indicators=_ind(close=100.1, vwap=100.0, volume_ratio=2.1))
    ok, detail = pf.evaluate(obs)
    assert ok is True
    assert "vol_ratio" in detail


def test_prefilter_skip_far_from_vwap():
    pf = SetupPreFilter(vwap_boundary_pct=0.002, volume_spike_mult=2.0)
    obs = AgentObservation(symbol="BTC-USD", indicators=_ind(close=101.0, vwap=100.0, volume_ratio=3.0))
    ok, detail = pf.evaluate(obs)
    assert ok is False
    assert ("not near" in detail.lower() or "boundary" in detail.lower() or "reclaim" in detail.lower())


def test_prefilter_skip_low_volume():
    pf = SetupPreFilter(vwap_boundary_pct=0.002, volume_spike_mult=2.0)
    obs = AgentObservation(symbol="BTC-USD", indicators=_ind(close=100.05, vwap=100.0, volume_ratio=1.5))
    ok, detail = pf.evaluate(obs)
    assert ok is False
    assert "volume spike" in detail


@pytest.mark.asyncio
async def test_agent_hold_on_prefilter_skip(caplog):
    import logging

    caplog.set_level(logging.INFO)
    agent = AgentCore(
        signal_engine=VwapMomentumScalpEngine(min_confidence=10),
        require_prefilter=True,
        volume_spike_mult=2.0,
        vwap_boundary_pct=0.002,
    )
    obs = AgentObservation(
        symbol="BTC-USD",
        indicators=_ind(close=105.0, vwap=100.0, volume_ratio=3.0),
    )
    d = await agent.decide(obs)
    assert d.action == Action.HOLD
    assert "PREFILTER_SKIP" in d.reasoning
    assert any("PREFILTER_SKIP" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_agent_prefilter_pass_uses_deterministic_engine(caplog):
    import logging

    caplog.set_level(logging.INFO)
    agent = AgentCore(
        signal_engine=VwapMomentumScalpEngine(min_confidence=50),
        use_llm=False,
        require_prefilter=True,
        volume_spike_mult=2.0,
        vwap_boundary_pct=0.002,
    )
    # near VWAP + spike + bullish → BUY from engine (ATR clears ~3.6% fee gate)
    obs = AgentObservation(
        symbol="BTC-USD",
        indicators=_ind(
            close=50_050.0,
            vwap=50_000.0,  # 0.1% away
            volume_ratio=2.2,
            rsi=58.0,
            macd_hist=4.0,
            ema_fast=50_050.0,
            ema_slow=49_900.0,
            atr=1_500.0,
            ema_cross="bullish",
            extras={"volume_ratio": 2.2, "macd_hist_prev": -1.0},
        ),
    )
    d = await agent.decide(obs)
    assert any("PREFILTER_PASS" in r.message for r in caplog.records)
    assert not any("LLM_INVOKED" in r.message for r in caplog.records)
    assert d.action == Action.BUY
    assert "VWAP scalp" in d.reasoning


@pytest.mark.asyncio
async def test_llm_invoked_only_when_prefilter_passes(caplog):
    import logging

    caplog.set_level(logging.INFO)

    class FakeLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, prompt, observation):
            self.calls += 1
            return (
                '{"action":"HOLD","symbol":"BTC-USD","confidence":0,'
                '"stop_loss":null,"take_profit":null,"reasoning":"llm hold"}'
            )

    llm = FakeLLM()
    agent = AgentCore(
        llm_hook=llm,
        use_llm=True,
        require_prefilter=True,
        volume_spike_mult=2.0,
    )
    # skip path
    skip_obs = AgentObservation(
        symbol="BTC-USD",
        indicators=_ind(close=110.0, vwap=100.0, volume_ratio=3.0),
    )
    d1 = await agent.decide(skip_obs)
    assert d1.action == Action.HOLD
    assert llm.calls == 0

    # pass path
    pass_obs = AgentObservation(
        symbol="BTC-USD",
        indicators=_ind(close=100.05, vwap=100.0, volume_ratio=2.5),
    )
    d2 = await agent.decide(pass_obs)
    assert llm.calls == 1
    assert any("LLM_INVOKED" in r.message for r in caplog.records)
    assert d2.action == Action.HOLD
    assert "llm hold" in d2.reasoning.lower() or d2.reasoning


# --- 7. Macro pause window ---


def test_in_pause_window_boundaries():
    now = datetime(2026, 9, 17, 14, 0, tzinfo=timezone.utc)
    event = now + timedelta(minutes=15)
    assert in_pause_window(now, event, 15) is True
    assert in_pause_window(now, event + timedelta(seconds=1), 15) is False
    assert in_pause_window(now, now - timedelta(minutes=15), 15) is True
    assert in_pause_window(now, now - timedelta(minutes=16), 15) is False


@pytest.mark.asyncio
async def test_macro_pause_window_holds():
    now = datetime(2026, 9, 17, 14, 0, tzinfo=timezone.utc)
    adapter = MockCalendarAdapter(
        [MacroEvent("US CPI m/m", now + timedelta(minutes=5), importance="high")]
    )
    guard = MacroGuard(adapter, enabled=True, pause_minutes=15)
    paused, ev = await guard.check_pause(now=now)
    assert paused is True
    assert ev is not None
    assert "CPI" in ev.title

    # Outside window
    paused2, _ = await guard.check_pause(now=now + timedelta(minutes=30))
    assert paused2 is False


@pytest.mark.asyncio
async def test_macro_pause_disabled():
    now = datetime(2026, 9, 17, 14, 0, tzinfo=timezone.utc)
    adapter = MockCalendarAdapter([MacroEvent("NFP", now, importance="high")])
    guard = MacroGuard(adapter, enabled=False, pause_minutes=15)
    paused, ev = await guard.check_pause(now=now)
    assert paused is False
    assert ev is None


# --- 8. Revenge lockout + memory ---


def test_revenge_lockout_after_two_stops(tmp_path: Path):
    store = BehavioralStateStore(
        str(tmp_path / "state.db"),
        revenge_stop_count=2,
        revenge_lockout_minutes=30,
    )
    assert store.is_locked_out("ETH-USD") is False
    locked1 = store.record_stop_loss("ETH-USD")
    assert locked1 is False
    assert store.consecutive_stops("ETH-USD") == 1
    locked2 = store.record_stop_loss("ETH-USD")
    assert locked2 is True
    assert store.is_locked_out("ETH-USD") is True
    assert store.lockout_remaining_seconds("ETH-USD") > 0


def test_revenge_non_stop_resets_streak(tmp_path: Path):
    store = BehavioralStateStore(str(tmp_path / "state.db"), revenge_stop_count=2)
    store.record_stop_loss("SOL-USD")
    store.record_non_stop_outcome("SOL-USD")
    assert store.consecutive_stops("SOL-USD") == 0
    store.record_stop_loss("SOL-USD")
    assert store.is_locked_out("SOL-USD") is False


def test_trade_memory_buffer_size_5(tmp_path: Path):
    store = BehavioralStateStore(
        str(tmp_path / "mem.db"),
        memory_size=DEFAULT_MEMORY_SIZE,
    )
    for i in range(8):
        store.add_trade_memory(
            symbol="BTC-USD",
            action="BUY" if i % 2 == 0 else "SELL",
            outcome="entry" if i % 2 == 0 else "exit",
            reason=f"trade-{i}",
            pnl=None,
        )
    mem = store.get_trade_memory()
    assert len(mem) == 5
    assert mem[-1].reason == "trade-7"
    assert mem[0].reason == "trade-3"
    prompt = store.memory_for_prompt()
    assert len(prompt) == 5


# --- 9. Notifier no-op ---


@pytest.mark.asyncio
async def test_notifier_noop_without_urls():
    n = Notifier(discord_webhook_url="", telegram_bot_token="", telegram_chat_id="")
    assert n.configured is False
    # Must not raise
    await n.send("hello", title="TEST")
    await n.trade_entry("BTC-USD", "BUY", 0.01)
    await n.circuit_breaker("dd")
    await n.macro_pause("CPI")
    await n.kill_switch()
    await n.stop_loss("ETH-USD")


@pytest.mark.asyncio
async def test_agent_honors_macro_and_revenge_flags():
    agent = AgentCore(require_prefilter=False)
    ind = _ind()
    d1 = await agent.decide(
        AgentObservation(symbol="BTC-USD", indicators=ind, macro_paused=True)
    )
    assert d1.action == Action.HOLD
    assert "macro" in d1.reasoning.lower()

    d2 = await agent.decide(
        AgentObservation(symbol="BTC-USD", indicators=ind, revenge_locked=True)
    )
    assert d2.action == Action.HOLD
    assert "revenge" in d2.reasoning.lower()


@pytest.mark.asyncio
async def test_trade_alert_clear_pnl_format():
    """BUY/SELL alerts show $ P&L + bankroll; quiet skips duplicate stop alerts."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    n = Notifier(discord_webhook_url="", telegram_bot_token="", telegram_chat_id="", quiet=True)
    captured = []

    async def capture(message, *, title="", extra=None):
        captured.append((title, message))

    n.send = capture  # type: ignore[method-assign]
    when = datetime(2026, 9, 18, 8, 24, tzinfo=ZoneInfo("America/Chicago"))
    await n.trade_entry(
        "XRP-USD",
        "BUY",
        37.352841,
        1.3244,
        paper=True,
        fee=0.44523,
        live_cash=1363.74,
        live_equity=6486.26,
        cash_after=150.08,
        equity_after=200.0,
        when=when,
    )
    await n.trade_exit(
        "XRP-USD",
        "SELL",
        37.352841,
        reason="ATR stop",
        price=1.3213,
        paper=True,
        fee=0.44419,
        buy_cost_with_fees=49.91533,
        pnl=-0.56,
        live_cash=1363.74,
        live_equity=6486.26,
        cash_after=1600.00,
        equity_after=1600.00,
        when=when,
    )
    await n.stop_loss("XRP-USD", "dup")
    await n.take_profit("XRP-USD", "dup")
    assert len(captured) == 2
    buy_title, buy_body = captured[0]
    sell_title, sell_body = captured[1]
    # Al addendum templates: emoji header in body, empty title
    assert "🔵 BUY FILLED | XRP-USD" in buy_body
    assert "Order Type: Limit Maker" in buy_body
    assert "Total Spent:" in buy_body
    assert "🔴 STOPPED OUT | XRP-USD" in sell_body
    assert "NET P&L: -$0.56 (Fees Deducted)" in sell_body
    assert "Reason: SL Hit" in sell_body
    assert "Current Cash: $1600.00" in sell_body

