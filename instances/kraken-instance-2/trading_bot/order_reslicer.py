"""L2 queue-position tracker + post-only order re-slicer (PAPER intel v3).

Tracks depth ahead of our POST_ONLY limit bid. If fill probability stalls for
ORDER_RESLICE_STALL_SEC (default 10s), cancel and re-place 1 tick closer
(higher for bids) while remaining maker (never cross the ask).

Paper: PaperQueueSimulator models queue depth + re-slice for unit tests.
Live: OrderReslicer consumes L2 snapshots from CoinbaseBroker.get_l2_book.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

TAG_RESLICE = "order_reslice: tick_closer"
TAG_STALL = "order_reslice: queue_stall"
TAG_FILLED = "order_reslice: queue_cleared"


def tick_size_for_price(price: float) -> float:
    """Heuristic Coinbase-style tick from price magnitude."""
    p = abs(float(price))
    if p >= 100:
        return 0.01
    if p >= 1:
        return 0.001
    if p >= 0.1:
        return 0.0001
    return 0.00001


def depth_ahead_of_bid(
    bids: Sequence[Dict[str, Any]],
    our_price: float,
    *,
    our_size: float = 0.0,
) -> float:
    """Sum size strictly better than our bid + same-price FIFO ahead."""
    if our_price <= 0:
        return 0.0
    ahead = 0.0
    px = float(our_price)
    for lvl in bids or []:
        try:
            lp = float(lvl.get("price") or 0)
            sz = float(lvl.get("size") or lvl.get("qty") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if lp <= 0 or sz <= 0:
            continue
        if lp > px:
            ahead += sz
        elif abs(lp - px) <= 1e-12:
            ahead += max(0.0, sz - max(0.0, float(our_size)))
    return float(ahead)


def next_maker_bid(
    current_bid: float,
    best_ask: float,
    *,
    tick: Optional[float] = None,
) -> Optional[float]:
    """Raise bid by 1 tick without crossing the ask (post-only safe)."""
    if current_bid <= 0 or best_ask <= 0:
        return None
    t = float(tick) if tick and tick > 0 else tick_size_for_price(current_bid)
    candidate = round(float(current_bid) + t, 10)
    if candidate >= float(best_ask) - 1e-12:
        return None
    return candidate


def fill_probability(depth_ahead: float, our_size: float = 1.0) -> float:
    """Crude fill probability: decays as depth ahead grows vs our size."""
    size = max(1e-9, float(our_size or 1.0))
    d = max(0.0, float(depth_ahead))
    return size / (size + d)


@dataclass
class WorkingOrder:
    order_id: str
    symbol: str
    side: str = "BUY"
    limit_price: float = 0.0
    qty: float = 0.0
    placed_at: float = field(default_factory=time.time)
    last_depth: float = 0.0
    last_depth_change_at: float = field(default_factory=time.time)
    reslice_count: int = 0
    status: str = "OPEN"  # OPEN | FILLED | CANCELLED


@dataclass
class ResliceAction:
    action: str  # hold | reslice | fill | cancel
    order_id: str
    new_price: Optional[float] = None
    reason: str = ""
    depth_ahead: float = 0.0


class OrderReslicer:
    """Track working POST_ONLY bids and decide when to re-slice."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        stall_sec: float = 10.0,
        max_reslices: int = 3,
        min_fill_prob: float = 0.15,
    ) -> None:
        self.enabled = bool(enabled)
        self.stall_sec = max(1.0, float(stall_sec or 10.0))
        self.max_reslices = max(0, int(max_reslices or 3))
        self.min_fill_prob = float(min_fill_prob or 0.15)
        self._orders: Dict[str, WorkingOrder] = {}

    def register(
        self,
        order_id: str,
        symbol: str,
        *,
        limit_price: float,
        qty: float,
        side: str = "BUY",
        now: Optional[float] = None,
    ) -> WorkingOrder:
        ts = time.time() if now is None else float(now)
        wo = WorkingOrder(
            order_id=order_id,
            symbol=symbol.upper(),
            side=side.upper(),
            limit_price=float(limit_price),
            qty=float(qty),
            placed_at=ts,
            last_depth_change_at=ts,
        )
        self._orders[order_id] = wo
        return wo

    def unregister(self, order_id: str) -> None:
        self._orders.pop(order_id, None)

    def get(self, order_id: str) -> Optional[WorkingOrder]:
        return self._orders.get(order_id)

    def open_orders(self) -> List[WorkingOrder]:
        return [o for o in self._orders.values() if o.status == "OPEN"]

    def on_book(
        self,
        order_id: str,
        bids: Sequence[Dict[str, Any]],
        asks: Sequence[Dict[str, Any]],
        *,
        now: Optional[float] = None,
    ) -> ResliceAction:
        """Update queue estimate from L2 snapshot; return action."""
        if not self.enabled:
            return ResliceAction(action="hold", order_id=order_id, reason="disabled")
        wo = self._orders.get(order_id)
        if wo is None or wo.status != "OPEN":
            return ResliceAction(action="hold", order_id=order_id, reason="not_open")
        ts = time.time() if now is None else float(now)
        depth = depth_ahead_of_bid(bids, wo.limit_price, our_size=wo.qty)
        if abs(depth - wo.last_depth) > 1e-9:
            wo.last_depth = depth
            wo.last_depth_change_at = ts
        else:
            wo.last_depth = depth

        if depth <= 1e-12:
            wo.status = "FILLED"
            return ResliceAction(
                action="fill",
                order_id=order_id,
                reason=TAG_FILLED,
                depth_ahead=0.0,
            )

        p_fill = fill_probability(depth, wo.qty)
        stalled = (ts - wo.last_depth_change_at) >= self.stall_sec
        low_prob = p_fill < self.min_fill_prob

        if stalled and low_prob and wo.reslice_count < self.max_reslices:
            best_ask = 0.0
            if asks:
                try:
                    best_ask = float(asks[0].get("price") or 0)
                except (TypeError, ValueError, AttributeError, IndexError):
                    best_ask = 0.0
            new_px = next_maker_bid(wo.limit_price, best_ask)
            if new_px is None:
                return ResliceAction(
                    action="hold",
                    order_id=order_id,
                    reason="cannot_reslice_without_crossing",
                    depth_ahead=depth,
                )
            wo.reslice_count += 1
            old = wo.limit_price
            wo.limit_price = new_px
            wo.last_depth_change_at = ts
            logger.info(
                "ORDER_RESLICE %s %s %.8f -> %.8f (n=%d depth=%.4f p_fill=%.3f)",
                wo.symbol,
                order_id,
                old,
                new_px,
                wo.reslice_count,
                depth,
                p_fill,
            )
            return ResliceAction(
                action="reslice",
                order_id=order_id,
                new_price=new_px,
                reason=TAG_RESLICE,
                depth_ahead=depth,
            )

        if stalled and wo.reslice_count >= self.max_reslices:
            return ResliceAction(
                action="hold",
                order_id=order_id,
                reason=TAG_STALL,
                depth_ahead=depth,
            )

        return ResliceAction(
            action="hold",
            order_id=order_id,
            reason="waiting",
            depth_ahead=depth,
        )


class PaperQueueSimulator:
    """Simulate L2 queue ahead of a paper POST_ONLY bid + re-slice behavior."""

    def __init__(
        self,
        *,
        stall_sec: float = 10.0,
        max_reslices: int = 3,
        tick: float = 0.01,
    ) -> None:
        self.reslicer = OrderReslicer(
            enabled=True, stall_sec=stall_sec, max_reslices=max_reslices
        )
        self.tick = float(tick)
        self._books: Dict[str, Tuple[List[Dict[str, float]], List[Dict[str, float]]]] = {}

    def place_bid(
        self,
        order_id: str,
        symbol: str,
        price: float,
        qty: float,
        *,
        depth_ahead: float,
        best_ask: float,
        now: float = 0.0,
    ) -> WorkingOrder:
        bids = [
            {"price": float(price), "size": float(depth_ahead) + float(qty)},
        ]
        asks = [{"price": float(best_ask), "size": 10.0}]
        self._books[order_id] = (bids, asks)
        return self.reslicer.register(
            order_id, symbol, limit_price=price, qty=qty, now=now
        )

    def drain_depth(self, order_id: str, amount: float) -> None:
        bids, asks = self._books[order_id]
        wo = self.reslicer.get(order_id)
        if wo is None:
            return
        for lvl in bids:
            if abs(lvl["price"] - wo.limit_price) < 1e-9:
                lvl["size"] = max(0.0, float(lvl["size"]) - float(amount))
                break
        self._books[order_id] = (bids, asks)

    def tick_book(self, order_id: str, *, now: float) -> ResliceAction:
        bids, asks = self._books[order_id]
        wo = self.reslicer.get(order_id)
        if wo is None:
            return ResliceAction(action="hold", order_id=order_id, reason="missing")
        action = self.reslicer.on_book(order_id, bids, asks, now=now)
        if action.action == "reslice" and action.new_price is not None:
            residual = max(0.0, action.depth_ahead)
            bids = [{"price": action.new_price, "size": residual + wo.qty}]
            self._books[order_id] = (bids, asks)
        return action
