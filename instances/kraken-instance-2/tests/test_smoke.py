"""Smoke: import main / one dry-run cycle with mock (no network)."""

from __future__ import annotations

import asyncio
import os

import pytest


def test_import_main():
    import main as main_mod

    assert hasattr(main_mod, "main")
    assert hasattr(main_mod, "async_main")
    assert hasattr(main_mod, "TradingApp")


@pytest.mark.asyncio
async def test_dry_run_once_cycle(tmp_path):
    os.environ["DRY_RUN"] = "true"
    os.environ["BROKER"] = "mock"
    os.environ["PAPER_TRADING_MODE"] = "true"
    os.environ["SYMBOLS"] = "AAPL"
    os.environ["SQLITE_PATH"] = str(tmp_path / "test.db")
    os.environ["LOG_LEVEL"] = "WARNING"

    from trading_bot.config import reload_settings
    from main import TradingApp

    settings = reload_settings()
    assert settings.effective_broker == "mock"
    assert settings.paper_trading_mode is True

    app = TradingApp(settings, once=True)
    await app.startup()
    try:
        decision = await app.cycle_symbol("AAPL")
        assert decision is not None
        assert decision.symbol == "AAPL"
        assert decision.action.value in ("BUY", "SELL", "HOLD")
        account = await app.broker.get_account()
        assert account.equity > 0
        assert app.feed.get_indicators("AAPL").close > 0
    finally:
        await app.shutdown(liquidate=False)


@pytest.mark.asyncio
async def test_main_once_cli(tmp_path):
    os.environ["SQLITE_PATH"] = str(tmp_path / "cli.db")
    from main import async_main

    code = await async_main(["--dry-run", "--once", "--symbols", "AAPL", "--log-level", "WARNING"])
    assert code == 0
