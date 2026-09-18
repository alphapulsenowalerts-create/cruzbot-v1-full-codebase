"""Coinbase adapter: construct + paper-mode simulate without network."""

from __future__ import annotations

import os

import pytest

from trading_bot.brokers.coinbase import CoinbaseBroker, _to_product_id
from trading_bot.config import Settings, reload_settings
from trading_bot.models import OrderRequest, OrderSide, OrderStatus, OrderType


def test_to_product_id_normalization():
    assert _to_product_id("btc") == "BTC-USD"
    assert _to_product_id("ETH-USD") == "ETH-USD"
    assert _to_product_id("sol/usd") == "SOL-USD"
    assert _to_product_id("BTCUSD") == "BTC-USD"


@pytest.mark.asyncio
async def test_coinbase_constructs_and_paper_simulates_without_network(tmp_path):
    os.environ["BROKER"] = "coinbase"
    os.environ["PAPER_TRADING_MODE"] = "true"
    os.environ["DRY_RUN"] = "false"
    os.environ["ACCOUNT_EQUITY"] = "1000"
    os.environ["SYMBOLS"] = "BTC-USD"
    # Explicitly empty credentials → offline paper path (no network)
    os.environ["COINBASE_API_KEY"] = ""
    os.environ["COINBASE_API_SECRET"] = ""
    os.environ["SQLITE_PATH"] = str(tmp_path / "cb.db")
    os.environ["PAPER_BOOK_PATH"] = str(tmp_path / "paper_book.json")
    os.environ["ACCOUNT_EQUITY"] = "1000"

    settings = reload_settings()
    assert settings.paper_trading_mode is True
    assert settings.effective_broker == "coinbase"

    broker = CoinbaseBroker(settings)
    assert broker.name == "coinbase"

    await broker.connect()
    try:
        acct = await broker.get_account()
        assert acct.paper is True
        assert acct.equity == pytest.approx(1000.0)

        bars = await broker.get_bars("BTC-USD", limit=20)
        assert len(bars) == 20
        assert bars[-1].close > 0

        quote = await broker.get_quote("BTC-USD")
        assert quote.bid > 0 and quote.ask >= quote.bid

        order = OrderRequest(
            symbol="BTC-USD",
            side=OrderSide.BUY,
            qty=0.001,
            order_type=OrderType.LIMIT,
            limit_price=quote.ask,
            paper=True,
        )
        result = await broker.submit_order(order)
        assert result.paper is True
        assert result.status == OrderStatus.FILLED
        assert result.broker_order_id and result.broker_order_id.startswith("cb-paper-")
        assert "simulated" in result.message.lower() or "paper" in result.message.lower()

        # Ensure no live client/session was opened for the offline paper path
        assert broker._client is None
        assert broker._session is None

        pos = await broker.get_position("BTC-USD")
        assert pos is not None
        assert pos.qty == pytest.approx(0.001)

        cancelled = await broker.cancel_all_orders()
        assert cancelled == 0  # already filled
    finally:
        await broker.disconnect()


@pytest.mark.asyncio
async def test_build_broker_selects_coinbase(tmp_path):
    os.environ["BROKER"] = "coinbase"
    os.environ["PAPER_TRADING_MODE"] = "true"
    os.environ["DRY_RUN"] = "false"
    os.environ["COINBASE_API_KEY"] = ""
    os.environ["COINBASE_API_SECRET"] = ""
    os.environ["SQLITE_PATH"] = str(tmp_path / "factory.db")

    from main import build_broker

    settings = reload_settings()
    broker = build_broker(settings)
    assert isinstance(broker, CoinbaseBroker)
