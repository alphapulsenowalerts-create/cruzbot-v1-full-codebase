"""Alpaca paper trading broker adapter (REST + optional WebSocket)."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import aiohttp

from trading_bot.brokers.base import BrokerAdapter
from trading_bot.config import Settings
from trading_bot.models import (
    AccountState,
    Bar,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Quote,
    utcnow,
)
from trading_bot.utils.retry import (
    ExponentialBackoff,
    RateLimitError,
    RetryableError,
    is_rate_limit_status,
    retry_async,
)

logger = logging.getLogger(__name__)

_STATUS_MAP = {
    "new": OrderStatus.SUBMITTED,
    "accepted": OrderStatus.SUBMITTED,
    "pending_new": OrderStatus.PENDING,
    "partially_filled": OrderStatus.PARTIAL,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELLED,
    "cancelled": OrderStatus.CANCELLED,
    "expired": OrderStatus.EXPIRED,
    "rejected": OrderStatus.REJECTED,
}


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return utcnow()
    s = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return utcnow()


class AlpacaBroker(BrokerAdapter):
    """Alpaca Markets paper/live REST (+ WS bars) adapter."""

    name = "alpaca"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._session: Optional[aiohttp.ClientSession] = None
        self._connected = False
        self._ws_task: Optional[asyncio.Task] = None
        self._day_start_equity: Optional[float] = None

    def _headers(self) -> Dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.settings.alpaca_api_key,
            "APCA-API-SECRET-KEY": self.settings.alpaca_secret_key,
            "Content-Type": "application/json",
        }

    async def connect(self) -> None:
        if not self.settings.alpaca_api_key or not self.settings.alpaca_secret_key:
            raise RuntimeError(
                "Alpaca credentials missing. Set ALPACA_API_KEY and ALPACA_SECRET_KEY "
                "or use --dry-run / BROKER=mock."
            )
        if not self.settings.paper_trading_mode:
            logger.warning(
                "PAPER_TRADING_MODE is False — live trading is outside default design. "
                "Proceeding only if base URL is intentional."
            )
        timeout = aiohttp.ClientTimeout(total=30)
        self._session = aiohttp.ClientSession(headers=self._headers(), timeout=timeout)
        acct = await self.get_account()
        self._day_start_equity = acct.equity
        self._connected = True
        logger.info("Alpaca connected (paper=%s equity=%.2f)", acct.paper, acct.equity)

    async def disconnect(self) -> None:
        self._connected = False
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        if not self._session:
            raise RuntimeError("AlpacaBroker not connected")

        async def _do() -> Any:
            assert self._session is not None
            async with self._session.request(method, url, params=params, json=json_body) as resp:
                text = await resp.text()
                if is_rate_limit_status(resp.status):
                    retry_after = resp.headers.get("Retry-After")
                    raise RateLimitError(
                        f"Alpaca 429: {text[:200]}",
                        retry_after=float(retry_after) if retry_after else None,
                    )
                if resp.status >= 500:
                    raise RetryableError(f"Alpaca {resp.status}: {text[:200]}")
                if resp.status >= 400:
                    raise RuntimeError(f"Alpaca {resp.status}: {text[:400]}")
                if not text:
                    return None
                return json.loads(text)

        return await retry_async(_do, max_attempts=5, backoff=ExponentialBackoff())

    async def get_account(self) -> AccountState:
        data = await self._request("GET", f"{self.settings.alpaca_base_url}/v2/account")
        equity = float(data.get("equity") or data.get("last_equity") or 0)
        cash = float(data.get("cash") or 0)
        bp = float(data.get("buying_power") or cash)
        last_eq = float(data.get("last_equity") or equity)
        day_pl = equity - last_eq
        paper = "paper" in (self.settings.alpaca_base_url or "").lower() or self.settings.paper_trading_mode
        if self._day_start_equity is None:
            self._day_start_equity = last_eq or equity
        day_pl_pct = day_pl / self._day_start_equity if self._day_start_equity else 0.0
        return AccountState(
            equity=equity,
            cash=cash,
            buying_power=bp,
            day_pl=day_pl,
            day_pl_pct=day_pl_pct,
            paper=paper,
        )

    async def get_positions(self) -> List[Position]:
        data = await self._request("GET", f"{self.settings.alpaca_base_url}/v2/positions")
        out: List[Position] = []
        for p in data or []:
            out.append(
                Position(
                    symbol=str(p["symbol"]).upper(),
                    qty=float(p.get("qty") or 0),
                    avg_entry_price=float(p.get("avg_entry_price") or 0),
                    market_value=float(p.get("market_value") or 0),
                    unrealized_pl=float(p.get("unrealized_pl") or 0),
                    side=str(p.get("side") or "long"),
                )
            )
        return out

    async def get_position(self, symbol: str) -> Optional[Position]:
        try:
            p = await self._request(
                "GET", f"{self.settings.alpaca_base_url}/v2/positions/{symbol.upper()}"
            )
        except RuntimeError as exc:
            if "404" in str(exc):
                return None
            raise
        return Position(
            symbol=str(p["symbol"]).upper(),
            qty=float(p.get("qty") or 0),
            avg_entry_price=float(p.get("avg_entry_price") or 0),
            market_value=float(p.get("market_value") or 0),
            unrealized_pl=float(p.get("unrealized_pl") or 0),
            side=str(p.get("side") or "long"),
        )

    async def get_bars(
        self,
        symbol: str,
        *,
        timeframe: str = "1Min",
        limit: int = 100,
    ) -> List[Bar]:
        params = {
            "timeframe": timeframe,
            "limit": limit,
            "adjustment": "raw",
            "feed": "iex",
        }
        url = f"{self.settings.alpaca_data_url}/v2/stocks/{symbol.upper()}/bars"
        data = await self._request("GET", url, params=params)
        bars_raw = (data or {}).get("bars") or []
        bars: List[Bar] = []
        for b in bars_raw:
            bars.append(
                Bar(
                    symbol=symbol.upper(),
                    timestamp=_parse_ts(b.get("t")),
                    open=float(b["o"]),
                    high=float(b["h"]),
                    low=float(b["l"]),
                    close=float(b["c"]),
                    volume=float(b.get("v") or 0),
                    vwap=float(b["vw"]) if b.get("vw") is not None else None,
                )
            )
        return bars

    async def get_quote(self, symbol: str) -> Quote:
        url = f"{self.settings.alpaca_data_url}/v2/stocks/{symbol.upper()}/quotes/latest"
        data = await self._request("GET", url, params={"feed": "iex"})
        q = (data or {}).get("quote") or data or {}
        return Quote(
            symbol=symbol.upper(),
            timestamp=_parse_ts(q.get("t")),
            bid=float(q.get("bp") or 0),
            ask=float(q.get("ap") or 0),
            bid_size=float(q.get("bs") or 0),
            ask_size=float(q.get("as") or 0),
        )

    def _map_order(self, data: Dict[str, Any], fallback: Optional[OrderRequest] = None) -> OrderResult:
        status = _STATUS_MAP.get(str(data.get("status", "")).lower(), OrderStatus.PENDING)
        side_raw = str(data.get("side") or (fallback.side.value if fallback else "buy")).upper()
        side = OrderSide.BUY if side_raw == "BUY" else OrderSide.SELL
        return OrderResult(
            client_order_id=str(data.get("client_order_id") or (fallback.client_order_id if fallback else "")),
            broker_order_id=str(data.get("id") or ""),
            status=status,
            symbol=str(data.get("symbol") or (fallback.symbol if fallback else "")).upper(),
            side=side,
            qty=float(data.get("qty") or (fallback.qty if fallback else 0)),
            filled_qty=float(data.get("filled_qty") or 0),
            avg_fill_price=float(data["filled_avg_price"]) if data.get("filled_avg_price") else None,
            message=str(data.get("status") or ""),
            paper=self.settings.paper_trading_mode,
        )

    async def submit_order(self, order: OrderRequest) -> OrderResult:
        if not self.settings.paper_trading_mode and "paper" not in self.settings.alpaca_base_url:
            # still allow if user explicitly disabled paper, but log loudly
            logger.critical("Submitting order while PAPER_TRADING_MODE=False")

        body: Dict[str, Any] = {
            "symbol": order.symbol,
            "qty": str(order.qty),
            "side": order.side.value.lower(),
            "type": order.order_type.value.lower(),
            "time_in_force": order.time_in_force,
            "client_order_id": order.client_order_id,
        }
        if order.limit_price is not None:
            body["limit_price"] = str(round(order.limit_price, 2))
        if order.stop_price is not None:
            body["stop_price"] = str(round(order.stop_price, 2))

        # Bracket via order_class when SL/TP provided
        if order.stop_loss is not None or order.take_profit is not None:
            body["order_class"] = "bracket"
            if order.take_profit is not None:
                body["take_profit"] = {"limit_price": str(round(order.take_profit, 2))}
            if order.stop_loss is not None:
                body["stop_loss"] = {"stop_price": str(round(order.stop_loss, 2))}

        data = await self._request(
            "POST",
            f"{self.settings.alpaca_base_url}/v2/orders",
            json_body=body,
        )
        return self._map_order(data or {}, order)

    async def cancel_order(self, broker_order_id: str) -> bool:
        try:
            await self._request(
                "DELETE",
                f"{self.settings.alpaca_base_url}/v2/orders/{broker_order_id}",
            )
            return True
        except RuntimeError as exc:
            if "404" in str(exc):
                return False
            raise

    async def cancel_all_orders(self) -> int:
        data = await self._request("DELETE", f"{self.settings.alpaca_base_url}/v2/orders")
        if isinstance(data, list):
            return len(data)
        return 0

    async def get_order(self, broker_order_id: str) -> OrderResult:
        data = await self._request(
            "GET", f"{self.settings.alpaca_base_url}/v2/orders/{broker_order_id}"
        )
        return self._map_order(data or {})

    async def liquidate_all(self) -> List[OrderResult]:
        positions = await self.get_positions()
        results: List[OrderResult] = []
        for pos in positions:
            # close position endpoint
            data = await self._request(
                "DELETE",
                f"{self.settings.alpaca_base_url}/v2/positions/{pos.symbol}",
            )
            if isinstance(data, dict) and data.get("id"):
                results.append(self._map_order(data))
            else:
                results.append(
                    OrderResult(
                        client_order_id=f"liq-{pos.symbol}",
                        broker_order_id=None,
                        status=OrderStatus.SUBMITTED,
                        symbol=pos.symbol,
                        side=OrderSide.SELL if pos.qty > 0 else OrderSide.BUY,
                        qty=abs(pos.qty),
                        message="liquidate submitted",
                        paper=self.settings.paper_trading_mode,
                    )
                )
        return results

    async def stream_bars(
        self,
        symbols: List[str],
        on_bar: Callable[[Bar], None],
    ) -> None:
        """WebSocket bar stream with reconnect + exponential backoff."""
        backoff = ExponentialBackoff(base=1.0, max_delay=60.0)
        while self._connected:
            try:
                await self._ws_bars_once(symbols, on_bar)
                backoff.reset()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                delay = backoff.next_delay()
                logger.warning("Alpaca WS disconnected (%s); reconnect in %.1fs", exc, delay)
                await asyncio.sleep(delay)

    async def _ws_bars_once(
        self,
        symbols: List[str],
        on_bar: Callable[[Bar], None],
    ) -> None:
        import websockets  # lazy

        async with websockets.connect(self.settings.alpaca_ws_url) as ws:
            await ws.send(
                json.dumps(
                    {
                        "action": "auth",
                        "key": self.settings.alpaca_api_key,
                        "secret": self.settings.alpaca_secret_key,
                    }
                )
            )
            auth_resp = json.loads(await ws.recv())
            logger.debug("Alpaca WS auth: %s", auth_resp)
            await ws.send(
                json.dumps({"action": "subscribe", "bars": [s.upper() for s in symbols]})
            )
            sub_resp = json.loads(await ws.recv())
            logger.debug("Alpaca WS sub: %s", sub_resp)

            async for raw in ws:
                if not self._connected:
                    break
                msgs = json.loads(raw)
                if not isinstance(msgs, list):
                    msgs = [msgs]
                for msg in msgs:
                    if msg.get("T") != "b":
                        continue
                    bar = Bar(
                        symbol=str(msg.get("S", "")).upper(),
                        timestamp=_parse_ts(msg.get("t")),
                        open=float(msg["o"]),
                        high=float(msg["h"]),
                        low=float(msg["l"]),
                        close=float(msg["c"]),
                        volume=float(msg.get("v") or 0),
                        vwap=float(msg["vw"]) if msg.get("vw") is not None else None,
                    )
                    on_bar(bar)
