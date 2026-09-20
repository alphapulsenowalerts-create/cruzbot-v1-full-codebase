"""In-memory mock broker for dry-run / tests — no network, no credentials."""

from __future__ import annotations

import asyncio
import math
import random
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional
from uuid import uuid4

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
from trading_bot.brokers.base import BrokerAdapter


class MockBroker(BrokerAdapter):
    """Simulates account, bars, quotes, and fills for local dry-runs."""

    name = "mock"

    def __init__(
        self,
        equity: float = 1000.0,
        symbols: Optional[List[str]] = None,
        seed: int = 42,
    ) -> None:
        self._equity0 = equity
        self._cash = equity
        self._equity = equity
        self._symbols = [s.upper() for s in (symbols or ["AAPL", "MSFT", "SPY"])]
        self._rng = random.Random(seed)
        self._prices: Dict[str, float] = {s: 100.0 + i * 25 for i, s in enumerate(self._symbols)}
        self._positions: Dict[str, Position] = {}
        self._orders: Dict[str, OrderResult] = {}
        self._connected = False
        self._day_start_equity = equity

    def reset_paper_book(self, cash: float | None = None) -> None:
        """Flat in-memory reset (Telegram /reset_paper + tests)."""
        val = float(cash if cash is not None else self._equity0)
        self._cash = val
        self._equity = val
        self._equity0 = val
        self._positions.clear()
        self._day_start_equity = val

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    def _bump_price(self, symbol: str) -> float:
        p = self._prices.get(symbol, 100.0)
        shock = self._rng.gauss(0, 0.15)
        p = max(1.0, p * (1 + shock / 100))
        self._prices[symbol] = p
        return p

    async def get_account(self) -> AccountState:
        pos_value = sum(p.market_value for p in self._positions.values())
        self._equity = self._cash + pos_value
        day_pl = self._equity - self._day_start_equity
        return AccountState(
            equity=self._equity,
            cash=self._cash,
            buying_power=self._cash,
            day_pl=day_pl,
            day_pl_pct=(day_pl / self._day_start_equity) if self._day_start_equity else 0.0,
            paper=True,
        )

    async def get_positions(self) -> List[Position]:
        # refresh mark-to-market
        for sym, pos in list(self._positions.items()):
            px = self._prices.get(sym, pos.avg_entry_price)
            pos.market_value = pos.qty * px
            pos.unrealized_pl = (px - pos.avg_entry_price) * pos.qty
        return list(self._positions.values())

    async def get_position(self, symbol: str) -> Optional[Position]:
        positions = await self.get_positions()
        for p in positions:
            if p.symbol == symbol.upper():
                return p
        return None

    async def get_bars(
        self,
        symbol: str,
        *,
        timeframe: str = "1Min",
        limit: int = 100,
    ) -> List[Bar]:
        symbol = symbol.upper()
        now = utcnow()
        bars: List[Bar] = []
        price = self._prices.get(symbol, 100.0)
        # walk backwards generating synthetic OHLCV
        for i in range(limit, 0, -1):
            ret = self._rng.gauss(0, 0.002)
            o = price
            c = max(1.0, o * (1 + ret))
            h = max(o, c) * (1 + abs(self._rng.gauss(0, 0.001)))
            l = min(o, c) * (1 - abs(self._rng.gauss(0, 0.001)))
            vol = float(self._rng.randint(10_000, 50_000))
            ts = now - timedelta(minutes=i)
            bars.append(
                Bar(
                    symbol=symbol,
                    timestamp=ts,
                    open=round(o, 4),
                    high=round(h, 4),
                    low=round(l, 4),
                    close=round(c, 4),
                    volume=vol,
                    vwap=round((h + l + c) / 3, 4),
                )
            )
            price = c
        self._prices[symbol] = price
        return bars

    async def get_quote(self, symbol: str) -> Quote:
        symbol = symbol.upper()
        mid = self._bump_price(symbol)
        spread = mid * 0.0005
        return Quote(
            symbol=symbol,
            timestamp=utcnow(),
            bid=round(mid - spread / 2, 4),
            ask=round(mid + spread / 2, 4),
            bid_size=100,
            ask_size=100,
        )

    async def submit_order(self, order: OrderRequest) -> OrderResult:
        symbol = order.symbol.upper()
        px = order.limit_price or self._prices.get(symbol, 100.0)
        # simulate immediate fill at limit (with tiny adverse slippage already applied upstream)
        cost = px * order.qty
        broker_id = f"mock-{uuid4().hex[:12]}"

        if order.side == OrderSide.BUY:
            if cost > self._cash + 1e-6:
                result = OrderResult(
                    client_order_id=order.client_order_id,
                    broker_order_id=broker_id,
                    status=OrderStatus.REJECTED,
                    symbol=symbol,
                    side=order.side,
                    qty=order.qty,
                    message="insufficient cash",
                    paper=True,
                )
                self._orders[broker_id] = result
                return result
            self._cash -= cost
            existing = self._positions.get(symbol)
            if existing:
                new_qty = existing.qty + order.qty
                existing.avg_entry_price = (
                    (existing.avg_entry_price * existing.qty + cost) / new_qty
                )
                existing.qty = new_qty
                existing.market_value = new_qty * px
            else:
                self._positions[symbol] = Position(
                    symbol=symbol,
                    qty=order.qty,
                    avg_entry_price=px,
                    market_value=cost,
                    side="long",
                )
        else:  # SELL
            existing = self._positions.get(symbol)
            if not existing or existing.qty < order.qty - 1e-9:
                result = OrderResult(
                    client_order_id=order.client_order_id,
                    broker_order_id=broker_id,
                    status=OrderStatus.REJECTED,
                    symbol=symbol,
                    side=order.side,
                    qty=order.qty,
                    message="insufficient position",
                    paper=True,
                )
                self._orders[broker_id] = result
                return result
            self._cash += cost
            existing.qty -= order.qty
            if existing.qty <= 1e-9:
                del self._positions[symbol]
            else:
                existing.market_value = existing.qty * px

        self._prices[symbol] = px
        result = OrderResult(
            client_order_id=order.client_order_id,
            broker_order_id=broker_id,
            status=OrderStatus.FILLED,
            symbol=symbol,
            side=order.side,
            qty=order.qty,
            filled_qty=order.qty,
            avg_fill_price=px,
            message="mock fill",
            paper=True,
        )
        self._orders[broker_id] = result
        return result

    async def cancel_order(self, broker_order_id: str) -> bool:
        order = self._orders.get(broker_order_id)
        if not order:
            return False
        if order.status in (OrderStatus.FILLED, OrderStatus.CANCELLED):
            return False
        order.status = OrderStatus.CANCELLED
        return True

    async def cancel_all_orders(self) -> int:
        n = 0
        for o in self._orders.values():
            if o.status in (OrderStatus.SUBMITTED, OrderStatus.PENDING, OrderStatus.PARTIAL):
                o.status = OrderStatus.CANCELLED
                n += 1
        return n

    async def get_order(self, broker_order_id: str) -> OrderResult:
        if broker_order_id not in self._orders:
            raise KeyError(broker_order_id)
        return self._orders[broker_order_id]

    async def liquidate_all(self) -> List[OrderResult]:
        results: List[OrderResult] = []
        for pos in list((await self.get_positions())):
            req = OrderRequest(
                symbol=pos.symbol,
                side=OrderSide.SELL,
                qty=pos.qty,
                order_type=OrderType.LIMIT,
                limit_price=self._prices.get(pos.symbol, pos.avg_entry_price),
                paper=True,
            )
            results.append(await self.submit_order(req))
        return results

    async def stream_bars(
        self,
        symbols: List[str],
        on_bar: Callable[[Bar], None],
    ) -> None:
        """Emit a synthetic bar every few seconds until cancelled."""
        while self._connected:
            for sym in symbols:
                bars = await self.get_bars(sym, limit=1)
                if bars:
                    on_bar(bars[-1])
            await asyncio.sleep(2.0)
