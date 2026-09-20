"""Abstract broker adapter — swap Alpaca / IB / Mock behind this interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator, Callable, List, Optional

from trading_bot.models import (
    AccountState,
    Bar,
    OrderRequest,
    OrderResult,
    Position,
    Quote,
)


class BrokerAdapter(ABC):
    """Common broker surface used by data_feed and executor."""

    name: str = "base"

    @abstractmethod
    async def connect(self) -> None:
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        ...

    @abstractmethod
    async def get_account(self) -> AccountState:
        ...

    @abstractmethod
    async def get_positions(self) -> List[Position]:
        ...

    @abstractmethod
    async def get_position(self, symbol: str) -> Optional[Position]:
        ...

    @abstractmethod
    async def get_bars(
        self,
        symbol: str,
        *,
        timeframe: str = "1Min",
        limit: int = 100,
    ) -> List[Bar]:
        ...

    @abstractmethod
    async def get_quote(self, symbol: str) -> Quote:
        ...

    @abstractmethod
    async def submit_order(self, order: OrderRequest) -> OrderResult:
        ...

    @abstractmethod
    async def cancel_order(self, broker_order_id: str) -> bool:
        ...

    @abstractmethod
    async def cancel_all_orders(self) -> int:
        ...

    @abstractmethod
    async def get_order(self, broker_order_id: str) -> OrderResult:
        ...

    @abstractmethod
    async def liquidate_all(self) -> List[OrderResult]:
        ...

    async def stream_bars(
        self,
        symbols: List[str],
        on_bar: Callable[[Bar], None],
    ) -> None:
        """Optional WebSocket bar stream. Default: no-op (REST polling)."""
        raise NotImplementedError(f"{self.name} does not implement stream_bars")

    async def stream_quotes(
        self,
        symbols: List[str],
        on_quote: Callable[[Quote], None],
    ) -> None:
        raise NotImplementedError(f"{self.name} does not implement stream_quotes")

    async def health_check(self) -> bool:
        try:
            await self.get_account()
            return True
        except Exception:
            return False
