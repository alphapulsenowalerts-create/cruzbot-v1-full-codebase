"""Focused tests for Telegram /reset_paper parse, paper-only guard, confirm flow."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from trading_bot.telegram_commands import (
    KNOWN_COMMANDS,
    REPLY_RESET_PAPER_CONFIRM_EXPIRED,
    REPLY_RESET_PAPER_LIVE_REFUSED,
    RESET_PAPER_CONFIRM_TTL_SECONDS,
    RESET_PAPER_USAGE,
    OpsControlState,
    ResetPaperError,
    assert_paper_mode_for_reset_paper,
    default_reset_paper_cash,
    format_help_reply,
    format_reset_paper_done_reply,
    format_reset_paper_pending_reply,
    parse_command,
    parse_reset_paper_args,
)


def test_known_commands_include_reset_paper():
    assert "reset_paper" in KNOWN_COMMANDS


def test_help_lists_reset_paper():
    text = format_help_reply()
    assert "/reset_paper" in text
    assert "confirm" in text.lower()


@pytest.mark.parametrize(
    "text,args",
    [
        ("/reset_paper", []),
        ("/reset_paper 1600", ["1600"]),
        ("/reset_paper 2500", ["2500"]),
        ("/reset_paper confirm", ["confirm"]),
        ("/reset_paper confirm 2500", ["confirm", "2500"]),
        ("/reset_paper@CruzBot 1800", ["1800"]),
    ],
)
def test_parse_command_reset_paper(text, args):
    parsed = parse_command(text)
    assert parsed is not None
    assert parsed[0] == "reset_paper"
    assert parsed[1] == args


@pytest.mark.parametrize(
    "args,expect",
    [
        ([], (False, None)),
        (["1600"], (False, 1600.0)),
        (["2500"], (False, 2500.0)),
        (["$1,800.50"], (False, 1800.50)),
        (["confirm"], (True, None)),
        (["confirm", "2500"], (True, 2500.0)),
        (["CONFIRM", "1000"], (True, 1000.0)),
    ],
)
def test_parse_reset_paper_args_ok(args, expect):
    assert parse_reset_paper_args(args) == expect


@pytest.mark.parametrize(
    "args",
    [
        ["nope"],
        ["confirm", "x"],
        ["1600", "extra"],
        ["confirm", "2500", "extra"],
        ["-100"],
        ["0"],
    ],
)
def test_parse_reset_paper_args_bad(args):
    with pytest.raises(ResetPaperError):
        parse_reset_paper_args(args)


def test_default_reset_paper_cash():
    assert default_reset_paper_cash(1600) == 1600.0
    assert default_reset_paper_cash(2500.5) == 2500.5
    assert default_reset_paper_cash(0) == 1600.0
    assert default_reset_paper_cash(None) == 1600.0


def test_paper_only_guard():
    assert_paper_mode_for_reset_paper(paper=True)
    with pytest.raises(ResetPaperError) as ei:
        assert_paper_mode_for_reset_paper(paper=False)
    assert "PAPER ONLY" in str(ei.value)
    assert str(ei.value) == REPLY_RESET_PAPER_LIVE_REFUSED


def test_ops_pending_reset_paper_ttl():
    ops = OpsControlState()
    assert ops.has_pending_reset_paper_confirm() is False
    ops.arm_reset_paper_confirm(2500.0, explicit=True, ttl=RESET_PAPER_CONFIRM_TTL_SECONDS)
    assert ops.has_pending_reset_paper_confirm() is True
    assert ops.pending_reset_paper_cash() == 2500.0
    assert ops.pending_reset_paper_explicit() is True
    ops._pending_reset_paper_until = time.monotonic() - 1.0
    assert ops.has_pending_reset_paper_confirm() is False
    assert ops.pending_reset_paper_cash() is None


def test_format_replies():
    pending = format_reset_paper_pending_reply(2500.0, ttl_seconds=60)
    assert "2500.00" in pending
    assert "/reset_paper confirm" in pending
    done = format_reset_paper_done_reply(1600.0, positions=0)
    assert "cash=$1600.00" in done
    assert "positions=0" in done
    done2 = format_reset_paper_done_reply(2500.0, account_equity_updated=True)
    assert "ACCOUNT_EQUITY=2500" in done2


@pytest.mark.asyncio
async def test_cmd_reset_paper_confirm_required_and_sets_amount(tmp_path):
    os.environ["DRY_RUN"] = "true"
    os.environ["BROKER"] = "mock"
    os.environ["PAPER_TRADING_MODE"] = "true"
    os.environ["TELEGRAM_COMMANDS_ENABLED"] = "false"
    os.environ["SYMBOLS"] = "BTC-USD"
    os.environ["SQLITE_PATH"] = str(tmp_path / "reset.db")
    os.environ["LOG_LEVEL"] = "WARNING"
    os.environ["ACCOUNT_EQUITY"] = "1600"
    # Isolate .env writes to tmp (TradingApp uses PROJECT_ROOT/.env — we monkeypatch below)

    from trading_bot.config import reload_settings
    from main import TradingApp
    import main as main_mod

    settings = reload_settings()
    app = TradingApp(settings, once=True)
    env_file = tmp_path / ".env"
    env_file.write_text("ACCOUNT_EQUITY=1600\n", encoding="utf-8")
    # Point PROJECT_ROOT used by handler upsert at tmp_path
    monkey_root = tmp_path
    real_project_root = main_mod.PROJECT_ROOT
    main_mod.PROJECT_ROOT = monkey_root
    try:
        # First call arms only
        reply = await app._cmd_reset_paper("reset_paper", ["2500"])
        assert "Confirm" in reply or "confirm" in reply.lower()
        assert app.ops.has_pending_reset_paper_confirm() is True
        assert float(app.broker._cash) == 1600.0  # unchanged until confirm

        # Confirm without amount uses pending 2500
        reply = await app._cmd_reset_paper("reset_paper", ["confirm"])
        assert "Paper book reset" in reply
        assert "cash=$2500.00" in reply
        assert float(app.broker._cash) == 2500.0
        assert app.broker._positions == {}
        assert "ACCOUNT_EQUITY=2500" in reply
        assert "ACCOUNT_EQUITY=2500" in env_file.read_text(encoding="utf-8")
        assert float(app.settings.account_equity) == 2500.0
    finally:
        main_mod.PROJECT_ROOT = real_project_root


@pytest.mark.asyncio
async def test_cmd_reset_paper_live_refused(tmp_path):
    os.environ["DRY_RUN"] = "true"
    os.environ["BROKER"] = "mock"
    os.environ["PAPER_TRADING_MODE"] = "false"
    os.environ["TELEGRAM_COMMANDS_ENABLED"] = "false"
    os.environ["SYMBOLS"] = "BTC-USD"
    os.environ["SQLITE_PATH"] = str(tmp_path / "reset_live.db")
    os.environ["LOG_LEVEL"] = "WARNING"

    from trading_bot.config import reload_settings
    from main import TradingApp

    settings = reload_settings()
    app = TradingApp(settings, once=True)
    # Force live even if settings reload quirks
    object.__setattr__(app.settings, "paper_trading_mode", False)
    reply = await app._cmd_reset_paper("reset_paper", ["1600"])
    assert reply == REPLY_RESET_PAPER_LIVE_REFUSED
    assert app.ops.has_pending_reset_paper_confirm() is False


@pytest.mark.asyncio
async def test_cmd_reset_paper_confirm_without_pending(tmp_path):
    os.environ["DRY_RUN"] = "true"
    os.environ["BROKER"] = "mock"
    os.environ["PAPER_TRADING_MODE"] = "true"
    os.environ["TELEGRAM_COMMANDS_ENABLED"] = "false"
    os.environ["SYMBOLS"] = "BTC-USD"
    os.environ["SQLITE_PATH"] = str(tmp_path / "reset_exp.db")
    os.environ["LOG_LEVEL"] = "WARNING"
    os.environ["ACCOUNT_EQUITY"] = "1600"

    from trading_bot.config import reload_settings
    from main import TradingApp

    settings = reload_settings()
    app = TradingApp(settings, once=True)
    reply = await app._cmd_reset_paper("reset_paper", ["confirm"])
    assert reply == REPLY_RESET_PAPER_CONFIRM_EXPIRED
