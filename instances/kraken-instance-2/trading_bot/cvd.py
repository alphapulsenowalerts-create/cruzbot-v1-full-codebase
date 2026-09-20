"""Cumulative Volume Delta (CVD) + Phase 1 long-entry gates.

CVD is updated from perp aggTrade execution side (buyer-maker vs seller-maker)
with O(1) counters so the asyncio WebSocket callback never does heavy work.

Divergence (long-only): a *closed* 5m candle that is green (close > open) with a
negative CVD delta for that same period is treated as institutional absorption
and blocks BUY.

Liquidation sweep (long-only): a POST_ONLY limit buy requires a recent spike in
*short* liquidation notional (exchange BUY-to-close shorts).

Cold / unavailable feeds
------------------------
Default is fail-closed when a gate is enabled and its data is missing.
Set PHASE1_ALLOW_COLD_FEED=true for paper warmup to bypass until the tape is hot.
PHASE1_FAIL_CLOSED=false also fail-opens on missing data (confirmed divergence
or a missing short-liq spike still block once the feed is warm).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_CVD_PERIOD_SEC = 300.0  # 5m
DEFAULT_LIQ_SWEEP_WINDOW_SEC = 60.0
DEFAULT_LIQ_SWEEP_NOTIONAL_USD = 50_000.0

REASON_CVD_ABSORPTION = "cvd_divergence: absorption (green 5m / negative CVD)"
REASON_CVD_COLD = "cvd_divergence: cold feed (fail-closed)"
REASON_CVD_BYPASS = "cvd_divergence: cold feed bypass"
REASON_CVD_SLOPE = "cvd_slope_nonpositive"
REASON_LIQ_NO_SPIKE = "liq_sweep: no short-liquidation spike"
REASON_LIQ_COLD = "liq_sweep: cold feed (fail-closed)"
REASON_LIQ_BYPASS = "liq_sweep: cold feed bypass"


def period_start_ts(ts: float, period_sec: float = DEFAULT_CVD_PERIOD_SEC) -> int:
    """UTC-aligned period start (seconds) for a trade or bar timestamp."""
    p = float(period_sec) if period_sec and period_sec > 0 else DEFAULT_CVD_PERIOD_SEC
    return int(float(ts) // p) * int(p)


def signed_cvd_delta(
    qty: float,
    price: float,
    is_buyer_maker: bool,
    *,
    use_notional: bool = True,
) -> float:
    """Taker buy → +delta; taker sell → −delta.

    Binance/Bybit `is_buyer_maker=True` means the buyer was maker, so the
    aggressor was the seller (taker sell).
    """
    mag = abs(float(qty) * float(price)) if use_notional else abs(float(qty))
    if mag <= 0:
        return 0.0
    return -mag if bool(is_buyer_maker) else mag


def is_short_liquidation(side: Optional[str]) -> bool:
    """Forced BUY (exchange buying to close shorts) = short liquidation.

    Binance forceOrder `S` and Bybit allLiquidation `S`: Buy → short liq,
    Sell → long liq.
    """
    if side is None:
        return False
    s = str(side).strip().lower()
    return s in ("buy", "b")


def _bar_ts_unix(ts: Any) -> Optional[float]:
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        v = float(ts)
        # ms vs s
        if v > 1e12:
            return v / 1000.0
        return v
    if hasattr(ts, "timestamp"):
        try:
            return float(ts.timestamp())
        except Exception:
            return None
    return None


def _bar_ohlc(bar: Any) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Return (open, close, ts_unix) from dict / object / Series."""
    if bar is None:
        return None, None, None
    if isinstance(bar, dict):
        o = bar.get("open")
        c = bar.get("close")
        ts = bar.get("timestamp") or bar.get("ts") or bar.get("start")
        try:
            return (
                float(o) if o is not None else None,
                float(c) if c is not None else None,
                _bar_ts_unix(ts),
            )
        except (TypeError, ValueError):
            return None, None, None
    try:
        o = getattr(bar, "open", None)
        c = getattr(bar, "close", None)
        ts = getattr(bar, "timestamp", None)
        if ts is None and hasattr(bar, "name"):
            ts = bar.name
        return (
            float(o) if o is not None else None,
            float(c) if c is not None else None,
            _bar_ts_unix(ts),
        )
    except (TypeError, ValueError):
        return None, None, None


def last_closed_5m_candle(
    bars: Any,
    *,
    now: Optional[float] = None,
    period_sec: float = DEFAULT_CVD_PERIOD_SEC,
) -> Optional[Tuple[float, float, int]]:
    """Last *closed* 5m bar as (open, close, period_start).

    A bar is closed when ``now >= period_start + period_sec``. The last row in
    a live frame is often still forming and is skipped.
    """
    if bars is None:
        return None
    now_ts = float(now) if now is not None else time.time()
    period = float(period_sec) if period_sec and period_sec > 0 else DEFAULT_CVD_PERIOD_SEC

    rows: List[Any] = []
    if hasattr(bars, "empty"):
        try:
            if bars.empty:
                return None
            rows = [bars.iloc[i] for i in range(len(bars))]
        except Exception:
            return None
    elif isinstance(bars, (list, tuple)):
        rows = list(bars)
    else:
        return None

    for bar in reversed(rows):
        o, c, ts = _bar_ohlc(bar)
        if o is None or c is None or ts is None:
            continue
        pstart = period_start_ts(ts, period)
        if now_ts + 1e-9 >= float(pstart) + period:
            return float(o), float(c), int(pstart)
    return None


@dataclass
class CvdSnapshot:
    symbol: str
    cumulative: float = 0.0
    period_start: Optional[int] = None
    period_delta: float = 0.0
    completed_period_start: Optional[int] = None
    completed_period_delta: Optional[float] = None
    trade_count: int = 0
    warm: bool = False
    updated_at: float = field(default_factory=time.time)

    def delta_for(self, period_start: Optional[int]) -> Optional[float]:
        if period_start is None:
            return None
        ps = int(period_start)
        if self.period_start is not None and int(self.period_start) == ps:
            return float(self.period_delta)
        if (
            self.completed_period_start is not None
            and int(self.completed_period_start) == ps
        ):
            return float(self.completed_period_delta or 0.0)
        return None


class CvdTracker:
    """Thread-safe running CVD + per-period delta. WS-callback safe (O(1))."""

    def __init__(
        self,
        *,
        period_sec: float = DEFAULT_CVD_PERIOD_SEC,
        keep_periods: int = 12,
    ) -> None:
        self.period_sec = float(period_sec) if period_sec and period_sec > 0 else DEFAULT_CVD_PERIOD_SEC
        self.keep_periods = max(2, int(keep_periods))
        self._lock = threading.Lock()
        self._cum: Dict[str, float] = defaultdict(float)
        self._period_start: Dict[str, int] = {}
        self._period_delta: Dict[str, float] = defaultdict(float)
        self._completed: Dict[str, Dict[int, float]] = defaultdict(dict)
        self._completed_order: Dict[str, List[int]] = defaultdict(list)
        self._trade_count: Dict[str, int] = defaultdict(int)

    def ingest(
        self,
        symbol: str,
        *,
        qty: float,
        price: float,
        is_buyer_maker: bool,
        ts: Optional[float] = None,
    ) -> None:
        spot = (symbol or "").strip().upper()
        if not spot:
            return
        now = float(ts) if ts is not None else time.time()
        delta = signed_cvd_delta(qty, price, is_buyer_maker)
        if delta == 0.0:
            # Still counts as tape activity (zero-qty is ignored)
            return
        ps = period_start_ts(now, self.period_sec)
        with self._lock:
            cur = self._period_start.get(spot)
            if cur is None:
                self._period_start[spot] = ps
                cur = ps
            elif ps != cur:
                self._flush_period(spot, cur)
                self._period_start[spot] = ps
                self._period_delta[spot] = 0.0
            self._period_delta[spot] += delta
            self._cum[spot] += delta
            self._trade_count[spot] += 1

    def _flush_period(self, spot: str, period_start: int) -> None:
        """Caller holds ``_lock``."""
        completed = self._completed[spot]
        order = self._completed_order[spot]
        completed[int(period_start)] = float(self._period_delta.get(spot, 0.0))
        order.append(int(period_start))
        while len(order) > self.keep_periods:
            old = order.pop(0)
            completed.pop(old, None)

    def period_delta(self, symbol: str, period_start: Optional[int]) -> Optional[float]:
        if period_start is None:
            return None
        spot = (symbol or "").strip().upper()
        ps = int(period_start)
        with self._lock:
            cur = self._period_start.get(spot)
            if cur is not None and int(cur) == ps:
                return float(self._period_delta.get(spot, 0.0))
            stored = self._completed.get(spot, {}).get(ps)
            if stored is not None:
                return float(stored)
            return None

    def snapshot(self, symbol: str) -> CvdSnapshot:
        spot = (symbol or "").strip().upper()
        now = time.time()
        with self._lock:
            cur = self._period_start.get(spot)
            order = self._completed_order.get(spot) or []
            last_ps = order[-1] if order else None
            last_delta = (
                self._completed.get(spot, {}).get(last_ps) if last_ps is not None else None
            )
            count = int(self._trade_count.get(spot, 0))
            return CvdSnapshot(
                symbol=spot,
                cumulative=float(self._cum.get(spot, 0.0)),
                period_start=cur,
                period_delta=float(self._period_delta.get(spot, 0.0)),
                completed_period_start=last_ps,
                completed_period_delta=float(last_delta) if last_delta is not None else None,
                trade_count=count,
                warm=count > 0,
                updated_at=now,
            )

    def trade_count(self, symbol: str) -> int:
        spot = (symbol or "").strip().upper()
        with self._lock:
            return int(self._trade_count.get(spot, 0))

    def slope(self, symbol: str, periods: int = 5) -> Optional[float]:
        """O(1)-ish CVD momentum: last N completed period deltas sum/mean slope.

        Positive => net taker-buy pressure over the window. Uses completed
        periods only (excludes the in-progress bucket) so the WS callback
        path stays cheap — callers just read.
        """
        spot = (symbol or "").strip().upper()
        n = max(1, int(periods))
        with self._lock:
            order = self._completed_order.get(spot) or []
            if len(order) < n:
                # Fall back: include current period delta if we have any tape
                if self._trade_count.get(spot, 0) <= 0:
                    return None
                deltas = []
                completed = self._completed.get(spot, {})
                for ps in order[-n:]:
                    deltas.append(float(completed.get(ps, 0.0)))
                if self._period_start.get(spot) is not None:
                    deltas.append(float(self._period_delta.get(spot, 0.0)))
                if len(deltas) < 2:
                    # Single-bucket: treat cumulative period delta as slope proxy
                    return float(deltas[-1]) if deltas else None
                return float(deltas[-1] - deltas[0]) / float(len(deltas) - 1)
            completed = self._completed.get(spot, {})
            deltas = [float(completed.get(ps, 0.0)) for ps in order[-n:]]
        if len(deltas) < 2:
            return float(deltas[0]) if deltas else None
        return float(deltas[-1] - deltas[0]) / float(len(deltas) - 1)

    def clear(self) -> None:
        with self._lock:
            self._cum.clear()
            self._period_start.clear()
            self._period_delta.clear()
            self._completed.clear()
            self._completed_order.clear()
            self._trade_count.clear()


def check_cvd_divergence(
    candle_open: Optional[float],
    candle_close: Optional[float],
    period_cvd_delta: Optional[float],
    *,
    enabled: bool = True,
    fail_closed: bool = True,
    allow_cold_feed: bool = False,
    feed_warm: bool = False,
    period_known: Optional[bool] = None,
) -> Tuple[bool, str]:
    """Return ``(allow_buy, reason)``. Only blocks green-close / negative-CVD."""
    if not enabled:
        return True, ""

    known = period_known
    if known is None:
        known = period_cvd_delta is not None and candle_open is not None and candle_close is not None

    if not feed_warm or not known:
        if allow_cold_feed or not fail_closed:
            return True, REASON_CVD_BYPASS
        return False, REASON_CVD_COLD

    o = float(candle_open)  # type: ignore[arg-type]
    c = float(candle_close)  # type: ignore[arg-type]
    delta = float(period_cvd_delta)  # type: ignore[arg-type]
    if c > o and delta < 0:
        return False, REASON_CVD_ABSORPTION
    return True, ""


def check_short_liq_sweep(
    short_liq_notional_window: Optional[float],
    *,
    threshold: float = DEFAULT_LIQ_SWEEP_NOTIONAL_USD,
    enabled: bool = True,
    fail_closed: bool = True,
    allow_cold_feed: bool = False,
    feed_warm: bool = False,
    spike_latched: bool = False,
) -> Tuple[bool, str]:
    """Return ``(allow_buy, reason)``. Requires short-liq notional spike."""
    if not enabled:
        return True, ""

    if not feed_warm:
        if allow_cold_feed or not fail_closed:
            return True, REASON_LIQ_BYPASS
        return False, REASON_LIQ_COLD

    notional = float(short_liq_notional_window or 0.0)
    if spike_latched or notional + 1e-9 >= float(threshold):
        return True, ""
    return False, REASON_LIQ_NO_SPIKE



def check_cvd_slope_positive(
    slope: Optional[float],
    *,
    enabled: bool = True,
    fail_closed: bool = True,
    allow_cold_feed: bool = False,
    feed_warm: bool = False,
) -> Tuple[bool, str]:
    """Mandatory LONG confirmation: 5-period CVD slope must be > 0."""
    if not enabled:
        return True, ""
    if slope is None or not feed_warm:
        if allow_cold_feed or not fail_closed:
            return True, REASON_CVD_BYPASS
        return False, REASON_CVD_COLD
    if float(slope) <= 0.0:
        return False, REASON_CVD_SLOPE
    return True, ""


def evaluate_phase1_long_gates(
    *,
    candle_open: Optional[float],
    candle_close: Optional[float],
    period_cvd_delta: Optional[float],
    short_liq_notional: Optional[float],
    cvd_enabled: bool = True,
    liq_enabled: bool = True,
    liq_threshold: float = DEFAULT_LIQ_SWEEP_NOTIONAL_USD,
    cvd_feed_warm: bool = False,
    liq_feed_warm: bool = False,
    fail_closed: bool = True,
    allow_cold_feed: bool = False,
    spike_latched: bool = False,
) -> Tuple[bool, str]:
    """Compose CVD divergence + short-liq sweep. First block wins."""
    ok_cvd, cvd_reason = check_cvd_divergence(
        candle_open,
        candle_close,
        period_cvd_delta,
        enabled=cvd_enabled,
        fail_closed=fail_closed,
        allow_cold_feed=allow_cold_feed,
        feed_warm=cvd_feed_warm,
    )
    if not ok_cvd:
        return False, cvd_reason

    ok_liq, liq_reason = check_short_liq_sweep(
        short_liq_notional,
        threshold=liq_threshold,
        enabled=liq_enabled,
        fail_closed=fail_closed,
        allow_cold_feed=allow_cold_feed,
        feed_warm=liq_feed_warm,
        spike_latched=spike_latched,
    )
    if not ok_liq:
        return False, liq_reason

    # Surface bypass tags for logs when a gate was skipped due to cold feed
    tags = [r for r in (cvd_reason, liq_reason) if r]
    return True, "; ".join(tags)
