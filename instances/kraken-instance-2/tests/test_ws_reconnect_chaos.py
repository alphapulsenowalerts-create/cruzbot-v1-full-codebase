"""Network reconnect & chaos unit tests for KrakenBroker (no live network).

Simulates WebSocket drops / stale_tick / force_reconnect and asserts:
  - reconnect path is invoked (existing Kraken log wording)
  - no unhandled exception bubbles out of the asyncio path
  - pending local/paper open orders are cancelled via cancel_all_orders
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trading_bot.brokers.kraken import KrakenBroker
from trading_bot.config import reload_settings
from trading_bot.models import OrderResult, OrderSide, OrderStatus


def _paper_kraken(tmp_path) -> KrakenBroker:
    os.environ["PAPER_TRADING_MODE"] = "true"
    os.environ["DRY_RUN"] = "true"
    os.environ["BROKER"] = "kraken"
    os.environ["STALE_TICK_SECONDS"] = "15"
    book = tmp_path / "paper_book_chaos.json"
    book.write_text(
        '{"cash": 1600.0, "equity": 1600.0, "day_start_equity": 1600.0, '
        '"updated_at": "2026-09-19T00:00:00+00:00", "positions": {}}'
    )
    settings = reload_settings()
    settings = settings.model_copy(
        update={
            "paper_trading_mode": True,
            "stale_tick_seconds": 15.0,
            "paper_book_path": str(book),
            "kraken_api_key": "",
            "kraken_api_secret": "",
            "symbol_list": ["BTC-USD"],
        }
    )
    broker = KrakenBroker(settings)
    broker._reconnect_backoff.base = 0.01
    broker._reconnect_backoff.max_delay = 0.05
    return broker


def _seed_pending_paper_order(broker: KrakenBroker, oid: str = "kr-paper-chaos-1") -> OrderResult:
    order = OrderResult(
        client_order_id="client-chaos-1",
        broker_order_id=oid,
        status=OrderStatus.SUBMITTED,
        symbol="BTC-USD",
        side=OrderSide.BUY,
        qty=0.001,
        filled_qty=0.0,
        message="resting paper limit (test fixture)",
        paper=True,
    )
    broker._paper_orders[oid] = order
    return order


@pytest.mark.asyncio
async def test_stale_tick_invokes_reconnect_path(tmp_path, caplog):
    broker = _paper_kraken(tmp_path)
    broker._touch_tick()
    broker._last_tick_mono = time.monotonic() - 30.0
    assert broker.is_market_data_stale() is True

    with caplog.at_level(logging.INFO):
        ok = await broker.ensure_market_data_fresh()

    assert any(
        "closing session and reconnecting" in r.message
        or "Market data stale_tick" in r.message
        for r in caplog.records
    ), f"expected reconnect log; got={[r.message for r in caplog.records]}"
    assert any("Reconnect backoff sleep" in r.message for r in caplog.records)
    assert isinstance(ok, bool)
    assert broker._reconnect_in_progress is False


@pytest.mark.asyncio
async def test_force_reconnect_cancels_pending_paper_orders(tmp_path, caplog):
    broker = _paper_kraken(tmp_path)
    pending = _seed_pending_paper_order(broker)
    filled = OrderResult(
        client_order_id="client-filled",
        broker_order_id="kr-paper-filled",
        status=OrderStatus.FILLED,
        symbol="BTC-USD",
        side=OrderSide.BUY,
        qty=0.001,
        filled_qty=0.001,
        avg_fill_price=100.0,
        message="already filled",
        paper=True,
    )
    broker._paper_orders["kr-paper-filled"] = filled
    broker._last_tick_mono = time.monotonic() - 30.0
    cash_before = broker._paper_cash

    with caplog.at_level(logging.INFO):
        await broker.force_reconnect_market_data(reason="ws_disconnect")

    assert pending.status == OrderStatus.CANCELLED
    assert filled.status == OrderStatus.FILLED  # must not touch filled
    assert abs(broker._paper_cash - cash_before) < 1e-9
    assert any("closing session and reconnecting" in r.message for r in caplog.records)
    assert any(
        "cancel_all simulated open orders" in r.message
        or ("cancelled" in r.message.lower() and "paper" in r.message.lower())
        for r in caplog.records
    )
    assert broker._reconnect_in_progress is False


@pytest.mark.asyncio
async def test_force_reconnect_no_unhandled_exception_when_probe_fails(tmp_path):
    broker = _paper_kraken(tmp_path)
    broker._last_tick_mono = time.monotonic() - 30.0
    broker.settings = broker.settings.model_copy(
        update={"kraken_api_key": "k", "kraken_api_secret": "s"}
    )
    with patch.object(broker, "get_quote", new=AsyncMock(side_effect=RuntimeError("boom"))):
        with patch.object(broker, "_init_api_client", new=AsyncMock()):
            await broker.force_reconnect_market_data(reason="stale_tick")
    assert broker._reconnect_in_progress is False


@pytest.mark.asyncio
async def test_concurrent_reconnect_serialized_no_crash(tmp_path):
    """Overlapping force_reconnect calls must not raise or leave the lock stuck."""
    broker = _paper_kraken(tmp_path)
    broker._last_tick_mono = time.monotonic() - 30.0
    active = {"n": 0}
    max_active = {"n": 0}

    orig_close = broker._close_api_surfaces

    async def tracked_close() -> None:
        active["n"] += 1
        max_active["n"] = max(max_active["n"], active["n"])
        await asyncio.sleep(0.03)
        await orig_close()
        active["n"] -= 1

    with patch.object(broker, "_close_api_surfaces", side_effect=tracked_close):
        await asyncio.gather(
            broker.force_reconnect_market_data(reason="a"),
            broker.force_reconnect_market_data(reason="b"),
            broker.force_reconnect_market_data(reason="c"),
        )
    assert max_active["n"] == 1  # reconnect lock serializes body
    assert broker._reconnect_in_progress is False


@pytest.mark.asyncio
async def test_ws_quote_drop_retries_without_crashing_event_loop(tmp_path, caplog):
    """Mock websockets.connect raising once, then exit cleanly — no raise."""
    broker = _paper_kraken(tmp_path)
    broker._connected = True

    class _BoomWS:
        async def __aenter__(self):
            raise ConnectionError("simulated WS drop")

        async def __aexit__(self, *args: Any) -> None:
            return None

    connect_calls = {"n": 0}

    def fake_connect(*_a, **_k):
        connect_calls["n"] += 1
        if connect_calls["n"] == 1:
            return _BoomWS()

        class _StopWS:
            async def __aenter__(self_inner):
                broker._connected = False
                return self_inner

            async def __aexit__(self_inner, *args: Any) -> None:
                return None

            async def send(self_inner, _payload: str) -> None:
                return None

            def __aiter__(self_inner):
                return self_inner

            async def __anext__(self_inner):
                raise StopAsyncIteration

        return _StopWS()

    fake_ws_mod = MagicMock()
    fake_ws_mod.connect = fake_connect

    with patch.dict("sys.modules", {"websockets": fake_ws_mod}):
        with patch(
            "trading_bot.brokers.kraken.ExponentialBackoff.next_delay",
            return_value=0.01,
        ):
            with caplog.at_level(logging.WARNING):
                await asyncio.wait_for(
                    broker.stream_quotes(["BTC-USD"], on_quote=lambda q: None),
                    timeout=2.0,
                )

    assert connect_calls["n"] >= 1
    assert any(
        "Kraken WS quote error" in r.message and "retry" in r.message.lower()
        for r in caplog.records
    ), f"expected WS retry log; got={[r.message for r in caplog.records]}"


@pytest.mark.asyncio
async def test_heartbeat_swallow_matches_main_monitor(tmp_path):
    """main.TradingBot._heartbeat_monitor wraps ensure() so loop stays alive."""
    broker = _paper_kraken(tmp_path)
    broker._last_tick_mono = time.monotonic() - 30.0

    async def boom_reconnect(*, reason: str = "stale") -> None:
        raise RuntimeError(f"injected reconnect failure ({reason})")

    with patch.object(broker, "force_reconnect_market_data", side_effect=boom_reconnect):
        caught: List[str] = []
        try:
            ensure = getattr(broker, "ensure_market_data_fresh", None)
            if callable(ensure):
                await ensure()
        except Exception as exc:
            # Same swallow as main._heartbeat_monitor
            caught.append(str(exc))
        assert caught and "injected reconnect failure" in caught[0]
