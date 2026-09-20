"""Unit tests for Telegram /test_trade parse, paper-only guard, and happy-path mocks."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from trading_bot.models import OrderResult, OrderSide, OrderStatus, OrderType, Quote
from trading_bot.telegram_commands import (
    TEST_TRADE_NOTIONAL_USD,
    TEST_TRADE_USAGE,
    TestTradeError,
    assert_paper_mode_for_test_trade,
    build_test_trade_order,
    format_test_trade_reply,
    parse_command,
    parse_test_trade_args,
)


def test_parse_command_test_trade():
    assert parse_command("/test_trade SOL-USD") == ("test_trade", ["SOL-USD"])
    assert parse_command("/test_trade@CruzBot LINK") == ("test_trade", ["LINK"])
    assert parse_command("/test_trade") == ("test_trade", [])


def test_parse_test_trade_args_normalizes_and_allowlists():
    assert parse_test_trade_args(["SOL"]) == "SOL-USD"
    assert parse_test_trade_args(["link-usd"]) == "LINK-USD"
    assert parse_test_trade_args(["ETH/USD"]) == "ETH-USD"


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["SOL", "extra"],
        ["FOO"],
        ["FOO-USD"],
        [""],
    ],
)
def test_parse_test_trade_args_rejects(args):
    with pytest.raises(TestTradeError) as exc:
        parse_test_trade_args(args)
    msg = str(exc.value)
    assert "Usage: /test_trade" in msg or "allowlist" in msg


def test_assert_paper_mode_refuses_live():
    with pytest.raises(TestTradeError) as exc:
        assert_paper_mode_for_test_trade(paper=False)
    assert "PAPER ONLY" in str(exc.value)
    assert_paper_mode_for_test_trade(paper=True)  # no raise


def test_format_test_trade_reply():
    text = format_test_trade_reply("LINK-USD", 12.54)
    assert text == (
        "🧪 TEST TRADE EXECUTED: Bought $100 of LINK-USD @ $12.54. "
        "Check /status to view open position."
    )


def test_build_test_trade_order_sizes_to_notional():
    order = build_test_trade_order("SOL-USD", price=200.0, notional=100.0)
    assert order.symbol == "SOL-USD"
    assert order.side == OrderSide.BUY
    assert order.order_type == OrderType.LIMIT
    assert order.limit_price == 200.0
    assert order.paper is True
    assert order.post_only is True
    assert abs(order.qty * 200.0 - 100.0) < 1e-4


@pytest.mark.asyncio
async def test_cmd_test_trade_paper_guard_and_happy_path(tmp_path, monkeypatch):
    import os

    os.environ["DRY_RUN"] = "true"
    os.environ["BROKER"] = "mock"
    os.environ["PAPER_TRADING_MODE"] = "true"
    os.environ["TELEGRAM_COMMANDS_ENABLED"] = "false"
    os.environ["SQLITE_PATH"] = str(tmp_path / "test_trade.db")
    os.environ["LOG_LEVEL"] = "WARNING"

    from trading_bot.config import reload_settings
    from main import TradingApp

    settings = reload_settings()
    app = TradingApp(settings, once=True)

    # Live refuse
    object.__setattr__(app.settings, "paper_trading_mode", False)
    refused = await app._cmd_test_trade("test_trade", ["SOL"])
    assert "PAPER ONLY" in refused

    object.__setattr__(app.settings, "paper_trading_mode", True)

    # Quote + submit mocks — bypass real broker; still exercise bypass path
    from datetime import datetime, timezone

    quote = Quote(
        symbol="SOL-USD",
        timestamp=datetime.now(timezone.utc),
        bid=149.9,
        ask=150.1,
    )
    app.broker.get_quote = AsyncMock(return_value=quote)

    filled = OrderResult(
        client_order_id="test-1",
        broker_order_id="paper-1",
        status=OrderStatus.FILLED,
        symbol="SOL-USD",
        side=OrderSide.BUY,
        qty=0.66666666,
        filled_qty=0.66666666,
        avg_fill_price=150.0,
        message="paper fill",
        paper=True,
    )
    app.executor.submit = AsyncMock(return_value=filled)

    reply = await app._cmd_test_trade("test_trade", ["SOL"])
    assert "TEST TRADE EXECUTED" in reply
    assert "SOL-USD" in reply
    assert "$150.00" in reply or "150.00" in reply
    app.executor.submit.assert_awaited_once()
    order_arg = app.executor.submit.await_args.args[0]
    assert order_arg.symbol == "SOL-USD"
    assert order_arg.side == OrderSide.BUY
    assert order_arg.paper is True
    # Strategy/risk bypass: order built directly, not via risk.evaluate
    assert abs(float(order_arg.qty) * float(order_arg.limit_price) - 100.0) < 0.01
