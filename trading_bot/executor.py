"""Order executor: limit orders, slippage protection, idempotency, partial fills."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Dict, Optional, Set

from trading_bot.brokers.base import BrokerAdapter
from trading_bot.config import HARD_SYMBOL_ALLOWLIST, Settings
from trading_bot.logger import TradeLogger

if TYPE_CHECKING:
    from trading_bot.notifier import Notifier
from trading_bot.models import (
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
)

logger = logging.getLogger(__name__)


class Executor:
    """
    Submits validated orders through the broker adapter.
    Respects PAPER_TRADING_MODE; idempotent by client_order_id.
    """

    def __init__(
        self,
        broker: BrokerAdapter,
        settings: Settings,
        trade_logger: Optional[TradeLogger] = None,
        notifier: Optional["Notifier"] = None,
    ) -> None:
        self.broker = broker
        self.settings = settings
        self.trade_logger = trade_logger
        self.notifier = notifier
        self._submitted_ids: Set[str] = set()
        self._results: Dict[str, OrderResult] = {}

    def apply_slippage(self, side: OrderSide, price: float) -> float:
        """Protective limit adjustment by slippage_bps."""
        bps = self.settings.slippage_bps / 10_000.0
        if side == OrderSide.BUY:
            return round(price * (1.0 + bps), 4)
        return round(price * (1.0 - bps), 4)

    async def submit(self, order: OrderRequest) -> OrderResult:
        # Hard allowlist — never submit non-allowlist (incl. sells / liquidations)
        sym = Settings.normalize_symbol(order.symbol)
        if sym not in HARD_SYMBOL_ALLOWLIST:
            logger.error("Rejecting non-allowlist order for %s", sym)
            return OrderResult(
                client_order_id=order.client_order_id,
                status=OrderStatus.REJECTED,
                symbol=sym,
                side=order.side,
                qty=order.qty,
                message=f"blocked: {sym} not on hard allowlist",
                paper=order.paper,
            )
        if order.symbol != sym:
            order = order.model_copy(update={"symbol": sym})

        if not self.settings.paper_trading_mode:
            logger.critical(
                "LIVE MODE: PAPER_TRADING_MODE=False — real broker orders enabled "
                "(explicit opt-in). Allowlist + $50/$200 caps still enforced."
            )

        # Idempotency
        if order.client_order_id in self._submitted_ids:
            existing = self._results.get(order.client_order_id)
            if existing:
                logger.info("Idempotent hit for %s", order.client_order_id)
                return existing
            return OrderResult(
                client_order_id=order.client_order_id,
                status=OrderStatus.REJECTED,
                symbol=order.symbol,
                side=order.side,
                qty=order.qty,
                message="duplicate client_order_id without cached result",
                paper=order.paper,
            )

        # Slippage-protected limit
        if order.order_type == OrderType.LIMIT and order.limit_price is not None:
            protected = self.apply_slippage(order.side, order.limit_price)
            order = order.model_copy(update={"limit_price": protected})

        order = order.model_copy(update={"paper": True if self.settings.paper_trading_mode else order.paper})

        # Strategy: post-only maker limits only — never convert to market/taker
        if order.order_type == OrderType.MARKET and bool(getattr(self.settings, "post_only", True)):
            logger.error(
                "Rejecting MARKET order (market_orders_disabled): %s %s",
                order.side.value,
                order.symbol,
            )
            return OrderResult(
                client_order_id=order.client_order_id,
                status=OrderStatus.REJECTED,
                symbol=order.symbol,
                side=order.side,
                qty=order.qty,
                message="market_orders_disabled",
                paper=order.paper,
            )
        if bool(getattr(self.settings, "post_only", True)):
            if order.order_type != OrderType.LIMIT or order.limit_price is None:
                logger.error(
                    "Rejecting non-limit order under POST_ONLY: %s %s type=%s",
                    order.side.value,
                    order.symbol,
                    order.order_type.value,
                )
                return OrderResult(
                    client_order_id=order.client_order_id,
                    status=OrderStatus.REJECTED,
                    symbol=order.symbol,
                    side=order.side,
                    qty=order.qty,
                    message="market_orders_disabled: POST_ONLY requires LIMIT with price",
                    paper=order.paper,
                )
            if not order.post_only:
                order = order.model_copy(update={"post_only": True})

        # Capture entry before sell fill clears the paper position (for ledger P&L)
        pre_sell_entry = 0.0
        if order.side == OrderSide.SELL:
            try:
                pos_before = await self.broker.get_position(order.symbol)
                if pos_before is not None:
                    pre_sell_entry = float(getattr(pos_before, "avg_entry_price", 0) or 0)
            except Exception:
                pre_sell_entry = 0.0

        logger.info(
            "SUBMIT %s %s qty=%.4f limit=%s id=%s",
            order.side.value,
            order.symbol,
            order.qty,
            order.limit_price,
            order.client_order_id,
        )

        try:
            result = await self.broker.submit_order(order)
        except Exception as exc:
            logger.exception("Order submit failed: %s", exc)
            result = OrderResult(
                client_order_id=order.client_order_id,
                status=OrderStatus.REJECTED,
                symbol=order.symbol,
                side=order.side,
                qty=order.qty,
                message=str(exc),
                paper=order.paper,
            )

        self._submitted_ids.add(order.client_order_id)
        self._results[order.client_order_id] = result

        if result.status == OrderStatus.PARTIAL:
            logger.warning(
                "Partial fill %s filled=%.4f / %.4f",
                order.symbol,
                result.filled_qty,
                order.qty,
            )

        if self.trade_logger:
            self.trade_logger.log_order(order, result)

        # Alerts owned by main.py (avoids duplicate Telegram spam)


        # Fee-aware paper ledger — ONLY real fills (never REJECTED / zero qty)
        try:
            filled_qty = float(result.filled_qty or 0)
            fill_px = float(result.avg_fill_price or 0)
            notional = fill_px * filled_qty
            is_fill = result.status in (OrderStatus.FILLED, OrderStatus.PARTIAL) and filled_qty > 0
            if is_fill and notional >= 1.0:
                from scripts.paper_ledger import log_event
                if bool(getattr(order, "post_only", False)) or bool(
                    getattr(self.settings, "post_only", False)
                ):
                    fee_rate = float(getattr(self.settings, "maker_fee_rate", 0.005) or 0.005)
                else:
                    fee_rate = float(getattr(self.settings, "taker_fee_rate", 0.009) or 0.009)
                fee = notional * fee_rate
                side = result.side.value if hasattr(result.side, "value") else str(result.side)
                kind = "BUY" if str(side).upper().startswith("B") else "SELL"
                realized_pnl = 0.0
                if kind == "SELL" and pre_sell_entry > 0 and filled_qty > 0:
                    entry_notional = pre_sell_entry * filled_qty
                    buy_cost_with_fees = entry_notional + entry_notional * fee_rate
                    realized_pnl = (notional - fee) - buy_cost_with_fees
                log_event(
                    kind,
                    symbol=str(result.symbol),
                    side=str(side).upper(),
                    qty=filled_qty,
                    price=fill_px,
                    notional=notional,
                    fee=fee,
                    pnl=realized_pnl,
                    note="executor fill",
                )
            elif not is_fill:
                logger.debug(
                    "ledger skip non-fill status=%s qty=%.8f",
                    result.status.value,
                    filled_qty,
                )
        except Exception as ledger_exc:
            logger.debug("paper ledger skip: %s", ledger_exc)

        return result

    async def refresh_status(self, broker_order_id: str) -> OrderResult:
        result = await self.broker.get_order(broker_order_id)
        if result.client_order_id:
            self._results[result.client_order_id] = result
        return result

    async def cancel_all(self) -> int:
        n = await self.broker.cancel_all_orders()
        logger.info("Cancelled %s open orders", n)
        return n

    async def liquidate_all(self) -> list[OrderResult]:
        """Liquidate allowlisted positions only — never touch non-allowlist holdings."""
        logger.warning("Liquidating ALLOWLIST positions only")
        try:
            positions = await self.broker.get_positions()
        except Exception:
            # Fallback: broker-native liquidate if it already filters
            return await self.broker.liquidate_all()

        results: list[OrderResult] = []
        for pos in positions:
            sym = Settings.normalize_symbol(getattr(pos, "symbol", ""))
            if sym not in HARD_SYMBOL_ALLOWLIST:
                logger.info("Skipping non-allowlist holding during liquidate: %s", sym)
                continue
            qty = abs(float(getattr(pos, "qty", 0) or 0))
            if qty <= 0:
                continue
            side = OrderSide.SELL
            # Prefer post-only limit at mark when POST_ONLY (never market/taker)
            limit_px = None
            try:
                q = await self.broker.get_quote(sym)
                if q is not None:
                    limit_px = float(q.bid or q.mid or 0) or None
            except Exception:
                limit_px = None
            if bool(getattr(self.settings, "post_only", True)) and limit_px and limit_px > 0:
                req = OrderRequest(
                    symbol=sym,
                    side=side,
                    qty=qty,
                    order_type=OrderType.LIMIT,
                    limit_price=limit_px,
                    paper=self.settings.paper_trading_mode,
                    post_only=True,
                )
            else:
                req = OrderRequest(
                    symbol=sym,
                    side=side,
                    qty=qty,
                    order_type=OrderType.MARKET,
                    paper=self.settings.paper_trading_mode,
                )
            results.append(await self.submit(req))
        return results
