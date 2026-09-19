"""Tests for Telegram /set_limit parse, validate, apply, and .env upsert."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from trading_bot.config import (
    SET_LIMIT_ENV_MAX_BOOK,
    SET_LIMIT_ENV_TRADE_CAP,
    apply_set_limit_to_settings,
    reload_settings,
    upsert_env_vars,
)
from trading_bot.telegram_commands import (
    REPLY_SET_LIMIT_USAGE,
    format_set_limit_reply,
    parse_command,
    parse_set_limit_args,
)


def test_parse_command_set_limit():
    assert parse_command("/set_limit 100 400") == ("set_limit", ["100", "400"])
    assert parse_command("/set_limit@CruzBot 50.5 200") == ("set_limit", ["50.5", "200"])


def test_parse_set_limit_args_success():
    cap, book, err = parse_set_limit_args(["100", "400"])
    assert err is None
    assert cap == 100.0
    assert book == 400.0
    cap, book, err = parse_set_limit_args(["50.5", "200.25"])
    assert err is None
    assert cap == 50.5
    assert book == 200.25


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["100"],
        ["100", "400", "extra"],
        ["abc", "400"],
        ["-10", "400"],
        ["0", "400"],
        ["100", "0"],
        ["100", "-1"],
        ["400", "100"],  # max_book < trade_cap
    ],
)
def test_parse_set_limit_args_rejects(args):
    cap, book, err = parse_set_limit_args(args)
    assert cap is None and book is None
    assert err == REPLY_SET_LIMIT_USAGE
    assert "set_limit" in err
    assert "100 1000" in err


def test_format_set_limit_reply():
    assert format_set_limit_reply(100, 400) == (
        "Trade cap updated to $100.00 | Max book updated to $400.00"
    )
    assert format_set_limit_reply(50.5, 200.1) == (
        "Trade cap updated to $50.50 | Max book updated to $200.10"
    )


def test_upsert_env_vars_and_apply(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "PAPER_TRADING_MODE=true\n"
        "MAX_NOTIONAL_PER_TRADE_USD=50\n"
        "MAX_TOTAL_EXPOSURE_USD=200\n"
        "MAX_TRADE_CAP=50\n"
        "SOME_SECRET=do-not-leak\n"
    )
    upsert_env_vars(
        {SET_LIMIT_ENV_TRADE_CAP: 100.0, SET_LIMIT_ENV_MAX_BOOK: 400.0},
        path=env,
    )
    text = env.read_text()
    assert "MAX_NOTIONAL_PER_TRADE_USD=100" in text
    assert "MAX_TOTAL_EXPOSURE_USD=400" in text
    # Alias present → updated
    assert "MAX_TRADE_CAP=100" in text
    # Unrelated keys preserved; we never print secrets in assertions beyond presence
    assert "PAPER_TRADING_MODE=true" in text
    assert "SOME_SECRET=do-not-leak" in text
    assert "MAX_BOOK_ALLOCATION" not in text  # not present → not created
    assert os.environ[SET_LIMIT_ENV_TRADE_CAP] == "100"
    assert os.environ[SET_LIMIT_ENV_MAX_BOOK] == "400"


@pytest.mark.asyncio
async def test_cmd_set_limit_applies_and_persists(tmp_path, monkeypatch):
    os.environ["DRY_RUN"] = "true"
    os.environ["BROKER"] = "mock"
    os.environ["PAPER_TRADING_MODE"] = "true"
    os.environ["TELEGRAM_COMMANDS_ENABLED"] = "false"
    os.environ["SYMBOLS"] = "BTC-USD"
    os.environ["SQLITE_PATH"] = str(tmp_path / "set_limit.db")
    os.environ["LOG_LEVEL"] = "WARNING"
    os.environ["MAX_NOTIONAL_PER_TRADE_USD"] = "50"
    os.environ["MAX_TOTAL_EXPOSURE_USD"] = "200"

    env_path = tmp_path / ".env"
    env_path.write_text(
        "PAPER_TRADING_MODE=true\n"
        "MAX_NOTIONAL_PER_TRADE_USD=50\n"
        "MAX_TOTAL_EXPOSURE_USD=200\n"
    )

    from trading_bot.config import reload_settings
    from main import TradingApp
    import trading_bot.config as cfg

    monkeypatch.setattr(cfg, "PROJECT_ROOT", tmp_path)
    # Also patch main.PROJECT_ROOT used by _cmd_set_limit
    import main as main_mod

    monkeypatch.setattr(main_mod, "PROJECT_ROOT", tmp_path)

    settings = reload_settings()
    app = TradingApp(settings, once=True)
    assert app.settings.paper_trading_mode is True
    assert float(app.settings.max_notional_per_trade_usd) == 50.0
    assert float(app.settings.max_total_exposure_usd) == 200.0

    bad = await app._cmd_set_limit("set_limit", ["400", "100"])
    assert bad == REPLY_SET_LIMIT_USAGE
    assert float(app.settings.max_notional_per_trade_usd) == 50.0

    reply = await app._cmd_set_limit("set_limit", ["100", "400"])
    assert reply == "Trade cap updated to $100.00 | Max book updated to $400.00"
    assert float(app.settings.max_notional_per_trade_usd) == 100.0
    assert float(app.settings.max_total_exposure_usd) == 400.0
    # Shared settings on risk/executor
    assert float(app.risk.settings.max_notional_per_trade_usd) == 100.0
    assert float(app.executor.settings.max_total_exposure_usd) == 400.0
    # Still paper
    assert app.settings.paper_trading_mode is True

    persisted = env_path.read_text()
    assert "MAX_NOTIONAL_PER_TRADE_USD=100" in persisted
    assert "MAX_TOTAL_EXPOSURE_USD=400" in persisted
    assert "PAPER_TRADING_MODE=true" in persisted
