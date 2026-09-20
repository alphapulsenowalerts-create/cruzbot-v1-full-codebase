"""Kraken adapter: symbol mapping, POST_ONLY / oflags=post, mocked REST, factory."""

from __future__ import annotations

import base64
import os

import pytest

from trading_bot.brokers.kraken import (
    KrakenBroker,
    build_add_order_payload,
    from_kraken_pair,
    sign_kraken_request,
    to_kraken_altname,
    to_kraken_pair,
    to_kraken_wsname,
    to_standard_symbol,
    ws_subscribe_message,
)
from trading_bot.config import Settings, reload_settings
from trading_bot.models import OrderRequest, OrderSide, OrderStatus, OrderType


def _dummy_secret() -> str:
    """Deterministic dummy base64 secret — not a live Kraken key."""
    return base64.b64encode(b"cruzbot-test-kraken-secret-32bytes!!").decode()


def test_symbol_mapping_btc_and_aliases():
    assert to_standard_symbol("btc") == "BTC-USD"
    assert to_standard_symbol("BTC-USD") == "BTC-USD"
    assert to_standard_symbol("BTC/USD") == "BTC-USD"
    assert to_standard_symbol("BTCUSD") == "BTC-USD"
    assert to_standard_symbol("XBTUSD") == "BTC-USD"
    assert to_standard_symbol("XBT/USD") == "BTC-USD"
    assert to_standard_symbol("XXBTZUSD") == "BTC-USD"

    assert to_kraken_pair("BTC-USD") == "XBTUSD"
    assert to_kraken_pair("BTC/USD") == "XBTUSD"
    assert to_kraken_pair("btc") == "XBTUSD"
    assert to_kraken_wsname("BTC-USD") == "XBT/USD"
    assert to_kraken_altname("BTC-USD") == "XXBTZUSD"
    assert from_kraken_pair("XBTUSD") == "BTC-USD"
    assert from_kraken_pair("XBT/USD") == "BTC-USD"
    assert from_kraken_pair("XXBTZUSD") == "BTC-USD"


def test_symbol_mapping_allowlist():
    expected = {
        "ETH-USD": ("ETHUSD", "ETH/USD", "XETHZUSD"),
        "SOL-USD": ("SOLUSD", "SOL/USD", "SOLUSD"),
        "XRP-USD": ("XRPUSD", "XRP/USD", "XXRPZUSD"),
        "LINK-USD": ("LINKUSD", "LINK/USD", "LINKUSD"),
        "AVAX-USD": ("AVAXUSD", "AVAX/USD", "AVAXUSD"),
        "SUI-USD": ("SUIUSD", "SUI/USD", "SUIUSD"),
        "ADA-USD": ("ADAUSD", "ADA/USD", "ADAUSD"),
        "DOGE-USD": ("DOGEUSD", "DOGE/USD", "DOGEUSD"),
        "DOT-USD": ("DOTUSD", "DOT/USD", "DOTUSD"),
        "ATOM-USD": ("ATOMUSD", "ATOM/USD", "ATOMUSD"),
        "LTC-USD": ("LTCUSD", "LTC/USD", "XLTCZUSD"),
        "UNI-USD": ("UNIUSD", "UNI/USD", "UNIUSD"),
        "NEAR-USD": ("NEARUSD", "NEAR/USD", "NEARUSD"),
    }
    for standard, (rest, wsname, alt) in expected.items():
        assert to_kraken_pair(standard) == rest
        assert to_kraken_wsname(standard) == wsname
        assert to_kraken_altname(standard) == alt
        assert from_kraken_pair(rest) == standard
        assert from_kraken_pair(wsname) == standard
        assert from_kraken_pair(alt) == standard


def test_expanded_symbols_normalize_and_allowlisted():
    """New liquid USD pairs normalize and pass HARD_SYMBOL_ALLOWLIST."""
    from trading_bot.config import HARD_SYMBOL_ALLOWLIST, Settings

    cases = {
        "DOGE": "DOGE-USD",
        "DOGEUSD": "DOGE-USD",
        "DOGE/USD": "DOGE-USD",
        "DOT": "DOT-USD",
        "ATOMUSD": "ATOM-USD",
        "LTC": "LTC-USD",
        "XLTC": "LTC-USD",
        "XLTCZUSD": "LTC-USD",
        "LTC/USD": "LTC-USD",
        "UNI": "UNI-USD",
        "NEARUSD": "NEAR-USD",
        "NEAR/USD": "NEAR-USD",
    }
    for raw, standard in cases.items():
        assert to_standard_symbol(raw) == standard
        assert standard in HARD_SYMBOL_ALLOWLIST
        assert Settings.normalize_symbol(standard) in HARD_SYMBOL_ALLOWLIST

    # BTC→XBT mapping must remain intact
    assert to_kraken_pair("BTC-USD") == "XBTUSD"
    assert to_kraken_wsname("BTC-USD") == "XBT/USD"


def test_ws_subscribe_uses_xbt_wsname():
    msg = ws_subscribe_message(["BTC-USD", "ETH/USD"], channel="ticker")
    assert msg["event"] == "subscribe"
    assert msg["pair"] == ["XBT/USD", "ETH/USD"]
    assert msg["subscription"]["name"] == "ticker"


def test_sign_kraken_request_is_deterministic():
    secret = _dummy_secret()
    data = {"nonce": "1616492376594"}
    a = sign_kraken_request("/0/private/Balance", data, secret)
    b = sign_kraken_request("/0/private/Balance", data, secret)
    assert a == b
    assert a != sign_kraken_request("/0/private/Balance", {"nonce": "1616492376595"}, secret)


def test_build_add_order_payload_oflags_post():
    order = OrderRequest(
        symbol="BTC-USD",
        side=OrderSide.BUY,
        qty=0.001,
        order_type=OrderType.LIMIT,
        limit_price=50000.0,
        post_only=True,
        client_order_id="adt-test-kraken-1",
    )
    payload = build_add_order_payload(order, post_only=True)
    assert payload["pair"] == "XBTUSD"
    assert payload["type"] == "buy"
    assert payload["ordertype"] == "limit"
    assert payload["price"] == "50000.0"
    assert payload["volume"] == "0.001"
    assert payload["oflags"] == "post"
    assert payload["cl_ord_id"] == "adt-test-kraken-1"

    bare = build_add_order_payload(order, post_only=False)
    assert "oflags" not in bare


@pytest.mark.asyncio
async def test_kraken_constructs_and_paper_simulates_without_network(tmp_path):
    os.environ["BROKER"] = "kraken"
    os.environ["PAPER_TRADING_MODE"] = "true"
    os.environ["DRY_RUN"] = "false"
    os.environ["ACCOUNT_EQUITY"] = "1000"
    os.environ["SYMBOLS"] = "BTC-USD"
    os.environ["KRAKEN_API_KEY"] = ""
    os.environ["KRAKEN_API_SECRET"] = ""
    os.environ["SQLITE_PATH"] = str(tmp_path / "kr.db")
    os.environ["PAPER_BOOK_PATH"] = str(tmp_path / "paper_book.json")

    settings = reload_settings()
    assert settings.paper_trading_mode is True
    assert settings.effective_broker == "kraken"

    broker = KrakenBroker(settings)
    assert broker.name == "kraken"

    await broker.connect()
    try:
        acct = await broker.get_account()
        assert acct.paper is True
        assert acct.equity == pytest.approx(1000.0)

        bars = await broker.get_bars("BTC-USD", limit=20)
        assert len(bars) == 20
        assert bars[-1].close > 0
        assert bars[-1].symbol == "BTC-USD"

        quote = await broker.get_quote("XBT/USD")
        assert quote.bid > 0 and quote.ask >= quote.bid
        assert quote.symbol == "BTC-USD"

        order = OrderRequest(
            symbol="BTC-USD",
            side=OrderSide.BUY,
            qty=0.001,
            order_type=OrderType.LIMIT,
            limit_price=quote.ask,
            paper=True,
            post_only=True,
        )
        result = await broker.submit_order(order)
        assert result.paper is True
        assert result.status == OrderStatus.FILLED
        assert result.broker_order_id and result.broker_order_id.startswith("kr-paper-")
        assert "simulated" in result.message.lower() or "paper" in result.message.lower()

        assert broker._session is None

        pos = await broker.get_position("BTC-USD")
        assert pos is not None
        assert pos.qty == pytest.approx(0.001)

        cancelled = await broker.cancel_all_orders()
        assert cancelled == 0  # already filled
    finally:
        await broker.disconnect()


@pytest.mark.asyncio
async def test_post_only_rejects_market_orders_paper(tmp_path):
    settings = Settings(
        BROKER="kraken",
        PAPER_TRADING_MODE=True,
        DRY_RUN=False,
        POST_ONLY=True,
        ACCOUNT_EQUITY=200,
        KRAKEN_API_KEY="",
        KRAKEN_API_SECRET="",
        SQLITE_PATH=str(tmp_path / "po.db"),
        PAPER_BOOK_PATH=str(tmp_path / "po_book.json"),
    )
    broker = KrakenBroker(settings)
    await broker.connect()
    try:
        order = OrderRequest(
            symbol="ETH-USD",
            side=OrderSide.BUY,
            qty=0.01,
            order_type=OrderType.MARKET,
            paper=True,
        )
        result = await broker.submit_order(order)
        assert result.status == OrderStatus.REJECTED
        assert result.message == "market_orders_disabled"
        assert result.broker_order_id and result.broker_order_id.startswith("kr-paper-reject-")
        assert await broker.get_position("ETH-USD") is None
    finally:
        await broker.disconnect()


@pytest.mark.asyncio
async def test_build_broker_selects_kraken(tmp_path):
    os.environ["BROKER"] = "kraken"
    os.environ["PAPER_TRADING_MODE"] = "true"
    os.environ["DRY_RUN"] = "false"
    os.environ["KRAKEN_API_KEY"] = ""
    os.environ["KRAKEN_API_SECRET"] = ""
    os.environ["SQLITE_PATH"] = str(tmp_path / "factory.db")

    from main import build_broker

    settings = reload_settings()
    broker = build_broker(settings)
    assert isinstance(broker, KrakenBroker)


@pytest.mark.asyncio
async def test_live_place_cancel_query_mocked_oflags_post(tmp_path):
    settings = Settings(
        BROKER="kraken",
        PAPER_TRADING_MODE=False,
        DRY_RUN=False,
        POST_ONLY=True,
        ACCOUNT_EQUITY=200,
        KRAKEN_API_KEY="dummy-key",
        KRAKEN_API_SECRET=_dummy_secret(),
        SQLITE_PATH=str(tmp_path / "live.db"),
    )
    broker = KrakenBroker(settings)
    broker._connected = True
    calls: list[tuple[str, dict]] = []

    async def fake_private(endpoint: str, data=None):
        payload = dict(data or {})
        calls.append((endpoint, payload))
        if endpoint == "AddOrder":
            return {"txid": ["OTEST-XXXXX-YYYYY"], "descr": {"order": "buy 0.001 XBTUSD @ limit 100"}}
        if endpoint == "CancelOrder":
            return {"count": 1}
        if endpoint == "QueryOrders":
            return {
                "OTEST-XXXXX-YYYYY": {
                    "status": "open",
                    "vol": "0.001",
                    "vol_exec": "0",
                    "descr": {
                        "pair": "XBTUSD",
                        "type": "buy",
                        "ordertype": "limit",
                        "price": "100",
                    },
                    "oflags": "post",
                    "cl_ord_id": "adt-live-1",
                }
            }
        if endpoint == "CancelAll":
            return {"count": 2}
        return {}

    broker._private = fake_private  # type: ignore[method-assign]

    order = OrderRequest(
        symbol="BTC/USD",
        side=OrderSide.BUY,
        qty=0.001,
        order_type=OrderType.LIMIT,
        limit_price=100.0,
        paper=False,
        post_only=True,
        client_order_id="adt-live-1",
    )
    result = await broker.submit_order(order)
    assert result.status == OrderStatus.SUBMITTED
    assert result.broker_order_id == "OTEST-XXXXX-YYYYY"
    assert result.paper is False

    add = next(c for c in calls if c[0] == "AddOrder")
    assert add[1]["pair"] == "XBTUSD"
    assert add[1]["oflags"] == "post"
    assert add[1]["ordertype"] == "limit"
    assert add[1]["type"] == "buy"

    queried = await broker.get_order("OTEST-XXXXX-YYYYY")
    assert queried.symbol == "BTC-USD"
    assert queried.status == OrderStatus.SUBMITTED
    assert queried.qty == pytest.approx(0.001)

    assert await broker.cancel_order("OTEST-XXXXX-YYYYY") is True
    cancel = next(c for c in calls if c[0] == "CancelOrder")
    assert cancel[1]["txid"] == "OTEST-XXXXX-YYYYY"

    assert await broker.cancel_all_orders() == 2


@pytest.mark.asyncio
async def test_live_post_only_rejects_market_without_rest(tmp_path):
    settings = Settings(
        BROKER="kraken",
        PAPER_TRADING_MODE=False,
        POST_ONLY=True,
        KRAKEN_API_KEY="dummy-key",
        KRAKEN_API_SECRET=_dummy_secret(),
        SQLITE_PATH=str(tmp_path / "mkt.db"),
    )
    broker = KrakenBroker(settings)
    called = {"n": 0}

    async def fake_private(endpoint: str, data=None):
        called["n"] += 1
        raise AssertionError("market POST_ONLY must not call AddOrder")

    broker._private = fake_private  # type: ignore[method-assign]
    order = OrderRequest(
        symbol="SOL-USD",
        side=OrderSide.BUY,
        qty=1.0,
        order_type=OrderType.MARKET,
        paper=False,
    )
    result = await broker.submit_order(order)
    assert result.status == OrderStatus.REJECTED
    assert result.message == "market_orders_disabled"
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_mocked_public_ohlc_and_ticker():
    settings = Settings(
        BROKER="kraken",
        PAPER_TRADING_MODE=False,
        POST_ONLY=True,
        KRAKEN_API_KEY="dummy-key",
        KRAKEN_API_SECRET=_dummy_secret(),
    )
    broker = KrakenBroker(settings)
    broker._session = object()  # pretend connected so public path is used

    async def fake_public(endpoint: str, params=None):
        if endpoint == "OHLC":
            assert params["pair"] == "XBTUSD"
            # [time, open, high, low, close, vwap, volume, count]
            return {
                "XXBTZUSD": [
                    [1_700_000_000, "100", "101", "99", "100.5", "100.2", "12", 3],
                    [1_700_000_060, "100.5", "102", "100", "101", "100.8", "8", 2],
                ],
                "last": 1_700_000_060,
            }
        if endpoint == "Ticker":
            assert params["pair"] == "XBTUSD"
            return {"XXBTZUSD": {"a": ["101.5", "1", "1"], "b": ["101.0", "1", "1"]}}
        if endpoint == "Depth":
            return {
                "XXBTZUSD": {
                    "bids": [["101.0", "2.0", 1]],
                    "asks": [["101.5", "1.5", 1]],
                }
            }
        raise AssertionError(endpoint)

    broker._public = fake_public  # type: ignore[method-assign]

    bars = await broker.get_bars("BTC-USD", timeframe="1Min", limit=2)
    assert len(bars) == 2
    assert bars[-1].close == pytest.approx(101.0)
    assert bars[-1].symbol == "BTC-USD"

    quote = await broker.get_quote("XXBTZUSD")
    assert quote.bid == pytest.approx(101.0)
    assert quote.ask == pytest.approx(101.5)
    assert quote.symbol == "BTC-USD"

    book = await broker.get_l2_book("BTC/USD", limit=5)
    assert book["mid"] == pytest.approx(101.25)
    assert book["bids"][0]["size"] == pytest.approx(2.0)
