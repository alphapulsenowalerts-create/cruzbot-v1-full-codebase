"""Interactive Brokers adapter stub — interface only for future swap-in."""

from __future__ import annotations

from typing import List, Optional

from trading_bot.brokers.base import BrokerAdapter
from trading_bot.models import (
    AccountState,
    Bar,
    OrderRequest,
    OrderResult,
    Position,
    Quote,
)


class IBBrokerStub(BrokerAdapter):
    """
    Placeholder for Interactive Brokers (ib_insync / ibapi).

    Not implemented — raises NotImplementedError on all operations.
    Keep method signatures identical to BrokerAdapter so Alpaca can be swapped.
    """

    name = "ib"

    async def connect(self) -> None:
        raise NotImplementedError("IBBrokerStub: connect not implemented")

    async def disconnect(self) -> None:
        raise NotImplementedError("IBBrokerStub: disconnect not implemented")

    async def get_account(self) -> AccountState:
        raise NotImplementedError("IBBrokerStub: get_account not implemented")

    async def get_positions(self) -> List[Position]:
        raise NotImplementedError("IBBrokerStub: get_positions not implemented")

    async def get_position(self, symbol: str) -> Optional[Position]:
        raise NotImplementedError("IBBrokerStub: get_position not implemented")

    async def get_bars(
        self,
        symbol: str,
        *,
        timeframe: str = "1Min",
        limit: int = 100,
    ) -> List[Bar]:
        raise NotImplementedError("IBBrokerStub: get_bars not implemented")

    async def get_quote(self, symbol: str) -> Quote:
        raise NotImplementedError("IBBrokerStub: get_quote not implemented")

    async def submit_order(self, order: OrderRequest) -> OrderResult:
        raise NotImplementedError("IBBrokerStub: submit_order not implemented")

    async def cancel_order(self, broker_order_id: str) -> bool:
        raise NotImplementedError("IBBrokerStub: cancel_order not implemented")

    async def cancel_all_orders(self) -> int:
        raise NotImplementedError("IBBrokerStub: cancel_all_orders not implemented")

    async def get_order(self, broker_order_id: str) -> OrderResult:
        raise NotImplementedError("IBBrokerStub: get_order not implemented")

    async def liquidate_all(self) -> List[OrderResult]:
        raise NotImplementedError("IBBrokerStub: liquidate_all not implemented")
