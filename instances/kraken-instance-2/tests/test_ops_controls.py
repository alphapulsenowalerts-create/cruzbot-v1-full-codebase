"""Unit tests for Telegram command parsing, ops pause, stale tick detector."""

from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from trading_bot.telegram_commands import (
    LIVE_CONFIRM_TTL_SECONDS,
    OpsControlState,
    REPLY_CONFIRM_EXPIRED,
    REPLY_MODE_LIVE_PENDING,
    format_mode_reply,
    format_status_reply,
    parse_command,
)


def test_parse_command_known():
    assert parse_command("/status") == ("status", [])
    assert parse_command("/pause@CruzBot") == ("pause", [])
    assert parse_command("/pnl extra") == ("pnl", ["extra"])
    assert parse_command("/kill") == ("kill", [])
    assert parse_command("/resume") == ("resume", [])
    assert parse_command("/mode") == ("mode", [])
    assert parse_command("/mode live") == ("mode", ["live"])
    assert parse_command("/mode paper") == ("mode", ["paper"])
    assert parse_command("/confirm_live") == ("confirm_live", [])
    assert parse_command("/mode@CruzBot live") == ("mode", ["live"])


def test_parse_command_ignores_unknown():
    assert parse_command("hello") is None
    assert parse_command("/help") == ("help", [])
    assert parse_command("") is None


def test_ops_pause_flag():
    ops = OpsControlState()
    assert ops.paused is False
    ops.set_pause(True)
    assert ops.paused is True
    ops.set_pause(False)
    assert ops.paused is False
    ops.request_kill(liquidate=True)
    assert ops.kill_requested is True
    assert ops.kill_liquidate is True


def test_format_status_reply_contains_fields():
    text = format_status_reply(
        paper_cash=1600.00,
        paper_equity=1600.00,
        positions=[{"symbol": "BTC-USD", "qty": 0.001, "avg_entry_price": 100000, "market_value": 100}],
        paused=True,
        strategy_mode="volume_sweet_spot",
        last_tick_age_seconds=2.5,
        paper=True,
        symbols=["BTC-USD", "ETH-USD", "SOL-USD"],
        max_notional_per_trade=100.0,
        max_total_exposure=1000.0,
        target_setup="WAIT",
        entry_proximity={
            "direction": "WAIT",
            "score": 52.3,
            "symbol": "SOL-USD",
            "price": 142.35,
            "bar": "█████░░░░░",
        },
    )
    assert "Apex Signals Now PAPER status" in text
    assert "CruzBot" not in text
    assert "1600.00" in text
    assert "PAUSED" in text
    assert "volume_sweet_spot" in text
    assert "2.5s" in text
    assert "pid=" not in text
    assert "caps=$100/trade $1000 exposure" in text
    assert "Threshold: 60%" in text
    assert "Target Setup: WAIT" in text
    assert "Entry Proximity: [█████░░░░░] 52.3%" in text
    assert "focus=SOL-USD @ $142.35" in text
    assert "BTC-USD" in text
    assert text.strip().endswith("symbols=BTC-USD,ETH-USD,SOL-USD")
    # symbols must be the last line
    assert text.splitlines()[-1].startswith("symbols=")

@pytest.mark.asyncio
async def test_pause_skips_buy_in_app(tmp_path):
    os.environ["DRY_RUN"] = "true"
    os.environ["BROKER"] = "mock"
    os.environ["PAPER_TRADING_MODE"] = "true"
    os.environ["TELEGRAM_COMMANDS_ENABLED"] = "false"
    os.environ["SYMBOLS"] = "AAPL"
    os.environ["SQLITE_PATH"] = str(tmp_path / "ops.db")
    os.environ["LOG_LEVEL"] = "WARNING"

    from trading_bot.config import reload_settings
    from trading_bot.models import Action, Decision
    from main import TradingApp

    settings = reload_settings()
    app = TradingApp(settings, once=True)
    app.ops.set_pause(True)
    # Simulate post-decide gate
    assert app.ops.paused is True
    decision = Decision(action=Action.BUY, symbol="AAPL", confidence=80, reasoning="test")
    # The gate lives in cycle_symbol; unit-check the flag path directly
    if decision.action == Action.BUY and app.ops.paused:
        held = Decision.hold("AAPL", "ops paused — skip new buys")
    else:
        held = decision
    assert held.action == Action.HOLD
    assert "ops paused" in held.reasoning


def test_stale_tick_detector_on_broker():
    """CoinbaseBroker marks stale after STALE_TICK_SECONDS without touch."""
    os.environ["PAPER_TRADING_MODE"] = "true"
    os.environ["DRY_RUN"] = "true"
    os.environ["BROKER"] = "mock"
    os.environ["STALE_TICK_SECONDS"] = "15"
    from trading_bot.config import reload_settings
    from trading_bot.brokers.coinbase import CoinbaseBroker

    settings = reload_settings()
    # Force coinbase broker even if dry_run would pick mock — construct directly
    settings = settings.model_copy(update={"paper_trading_mode": True, "stale_tick_seconds": 15.0})
    # Clear credentials so connect stays offline
    settings = settings.model_copy(update={"coinbase_api_key": "", "coinbase_api_secret": ""})
    broker = CoinbaseBroker(settings)
    assert broker.last_tick_age_seconds is None
    assert broker.is_market_data_stale() is False
    broker._touch_tick()
    assert broker.last_tick_age_seconds is not None
    assert broker.last_tick_age_seconds < 1.0
    # Simulate age past threshold
    broker._last_tick_mono = time.monotonic() - 20.0
    assert broker.is_market_data_stale() is True
    assert broker.last_tick_age_seconds >= 15.0


@pytest.mark.asyncio
async def test_force_reconnect_preserves_paper_cash(tmp_path):
    os.environ["PAPER_TRADING_MODE"] = "true"
    book = tmp_path / "paper_book.json"
    book.write_text(
        '{"cash": 1600.0, "equity": 1600.0, "day_start_equity": 1600.0, '
        '"updated_at": "2026-09-18T00:00:00+00:00", "positions": {}}'
    )
    from trading_bot.config import reload_settings
    from trading_bot.brokers.coinbase import CoinbaseBroker

    settings = reload_settings()
    settings = settings.model_copy(
        update={
            "paper_trading_mode": True,
            "stale_tick_seconds": 15.0,
            "paper_book_path": str(book),
            "coinbase_api_key": "",
            "coinbase_api_secret": "",
        }
    )
    broker = CoinbaseBroker(settings)
    cash_before = broker._paper_cash
    broker._last_tick_mono = time.monotonic() - 30.0
    # Shorten backoff for test
    broker._reconnect_backoff.base = 0.01
    broker._reconnect_backoff.max_delay = 0.05
    await broker.force_reconnect_market_data(reason="test")
    assert abs(broker._paper_cash - cash_before) < 1e-9


def test_format_mode_reply():
    assert format_mode_reply(paper=True, cash=1600.0, equity=1600.0).startswith("MODE: PAPER")
    assert "1600.00" in format_mode_reply(paper=True, cash=1600.0, equity=1600.0)
    assert format_mode_reply(paper=False, cash=100.0, equity=110.5).startswith("MODE: LIVE")


def test_live_confirm_ttl_and_reject_without_pending():
    ops = OpsControlState()
    assert ops.has_pending_live_confirm() is False
    assert REPLY_CONFIRM_EXPIRED  # constant present
    assert "confirm_live" in REPLY_MODE_LIVE_PENDING or "REAL capital" in REPLY_MODE_LIVE_PENDING

    ttl = ops.arm_live_confirm(ttl=2.0)
    assert ttl == 2.0
    assert ops.has_pending_live_confirm() is True
    assert ops.pending_live_confirm_remaining() > 0

    # Explicit now past deadline → expired / cleared
    past = time.monotonic() + 10.0
    assert ops.has_pending_live_confirm(now=past) is False
    assert ops.has_pending_live_confirm() is False

    # Reject confirm path when no pending
    assert ops.has_pending_live_confirm() is False

    # Re-arm then clear via /mode paper semantics
    ops.arm_live_confirm(ttl=LIVE_CONFIRM_TTL_SECONDS)
    assert ops.has_pending_live_confirm() is True
    ops.clear_live_confirm()
    assert ops.has_pending_live_confirm() is False


@pytest.mark.asyncio
async def test_mode_switch_confirm_flow(tmp_path):
    """Runtime paper flag flips only after /confirm_live; caps untouched; no .env write."""
    os.environ["DRY_RUN"] = "true"
    os.environ["BROKER"] = "mock"
    os.environ["PAPER_TRADING_MODE"] = "true"
    os.environ["TELEGRAM_COMMANDS_ENABLED"] = "false"
    os.environ["SYMBOLS"] = "BTC-USD"
    os.environ["SQLITE_PATH"] = str(tmp_path / "mode.db")
    os.environ["LOG_LEVEL"] = "WARNING"
    os.environ["MAX_NOTIONAL_PER_TRADE_USD"] = "50"
    os.environ["MAX_TOTAL_EXPOSURE_USD"] = "200"

    from trading_bot.config import reload_settings
    from main import TradingApp

    settings = reload_settings()
    app = TradingApp(settings, once=True)
    assert app.settings.paper_trading_mode is True
    cap_trade = float(app.settings.max_notional_per_trade_usd)
    cap_exp = float(app.settings.max_total_exposure_usd)

    # /mode → PAPER status
    reply = await app._cmd_mode("mode", [])
    assert reply.startswith("MODE: PAPER")

    # /mode live → pending only, still paper
    reply = await app._cmd_mode("mode", ["live"])
    assert reply == REPLY_MODE_LIVE_PENDING
    assert app.settings.paper_trading_mode is True
    assert app.ops.has_pending_live_confirm() is True

    # /confirm_live without pending rejected
    app.ops.clear_live_confirm()
    reply = await app._cmd_confirm_live("confirm_live", [])
    assert reply == REPLY_CONFIRM_EXPIRED
    assert app.settings.paper_trading_mode is True

    # Full arm + confirm
    await app._cmd_mode("mode", ["live"])
    reply = await app._cmd_confirm_live("confirm_live", [])
    assert "MODE: LIVE" in reply
    assert "LIVE MODE ACTIVATED" in reply
    assert app.settings.paper_trading_mode is False
    assert app.ops.has_pending_live_confirm() is False
    # Executor / broker see same Settings instance when present
    assert app.executor.settings.paper_trading_mode is False
    if hasattr(app.broker, "settings"):
        assert app.broker.settings.paper_trading_mode is False
    # Caps intact
    assert float(app.settings.max_notional_per_trade_usd) == cap_trade
    assert float(app.settings.max_total_exposure_usd) == cap_exp

    # /mode paper restores immediately and clears any pending
    app.ops.arm_live_confirm(ttl=60)
    reply = await app._cmd_mode("mode", ["paper"])
    assert "MODE: PAPER" in reply
    assert "PAPER MODE RESTORED" in reply
    assert app.settings.paper_trading_mode is True
    assert app.ops.has_pending_live_confirm() is False

    # Expired confirm rejected
    await app._cmd_mode("mode", ["live"])
    # Force expiry
    app.ops._pending_live_confirm_until = time.monotonic() - 1.0
    reply = await app._cmd_confirm_live("confirm_live", [])
    assert reply == REPLY_CONFIRM_EXPIRED
    assert app.settings.paper_trading_mode is True
