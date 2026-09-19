"""Binance/Bybit perp lead-lag + funding/OI positioning filter (public endpoints)."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence, Tuple

import httpx

logger = logging.getLogger(__name__)

# Coinbase spot → USDT-M perp symbol
SPOT_TO_PERP: Dict[str, str] = {
    "BTC-USD": "BTCUSDT",
    "ETH-USD": "ETHUSDT",
    "SOL-USD": "SOLUSDT",
    "XRP-USD": "XRPUSDT",
    "LINK-USD": "LINKUSDT",
    "AVAX-USD": "AVAXUSDT",
    "SUI-USD": "SUIUSDT",
    "ADA-USD": "ADAUSDT",
}

# Sweep on BTC/ETH can elevate correlated allowlisted majors
SWEEP_CORRELATED: Dict[str, Tuple[str, ...]] = {
    "BTC-USD": ("BTC-USD", "SOL-USD", "LINK-USD", "AVAX-USD"),
    "ETH-USD": ("ETH-USD", "SOL-USD", "SUI-USD", "ADA-USD", "LINK-USD"),
}


def coinbase_to_perp(symbol: str) -> Optional[str]:
    s = (symbol or "").strip().upper().replace("/", "-")
    return SPOT_TO_PERP.get(s)


def perp_to_coinbase(perp: str) -> Optional[str]:
    p = (perp or "").strip().upper()
    for spot, pp in SPOT_TO_PERP.items():
        if pp == p:
            return spot
    return None


@dataclass
class LeadLagSnapshot:
    recent_buy_sweep: bool = False
    recent_liq_cascade: bool = False
    sweep_symbols: Tuple[str, ...] = ()
    liq_symbols: Tuple[str, ...] = ()
    sweep_ts: Optional[float] = None
    liq_ts: Optional[float] = None
    updated_at: float = field(default_factory=time.time)

    def elevate_symbols(self, allowlist: Sequence[str]) -> List[str]:
        """Spot symbols to prioritize when a sweep/liq is hot."""
        allow = {s.upper() for s in allowlist}
        out: List[str] = []
        sources = list(self.sweep_symbols) + list(self.liq_symbols)
        for spot in sources:
            for cand in SWEEP_CORRELATED.get(spot, (spot,)):
                if cand in allow and cand not in out:
                    out.append(cand)
        return out


@dataclass
class FundingOISnapshot:
    symbol: str  # Coinbase spot
    perp: str
    funding_rate: Optional[float] = None
    open_interest: Optional[float] = None
    oi_delta_pct: Optional[float] = None
    fetched_at: float = 0.0


@dataclass
class CvdSnapshot:
    """Rolling signed CVD notional deltas (buy aggressor +, sell aggressor -)."""
    symbol: str
    cvd_1m: float = 0.0
    cvd_5m: float = 0.0
    updated_at: float = 0.0
    ready: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "cvd_1m": self.cvd_1m,
            "cvd_5m": self.cvd_5m,
            "updated_at": self.updated_at,
            "ready": self.ready,
        }


@dataclass
class LiqNotionalSnapshot:
    """Rolling 1m liquidation notional split by short vs long liquidations."""
    symbol: str
    short_liq_1m_usd: float = 0.0
    long_liq_1m_usd: float = 0.0
    updated_at: float = 0.0
    ready: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "short_liq_1m_usd": self.short_liq_1m_usd,
            "long_liq_1m_usd": self.long_liq_1m_usd,
            "updated_at": self.updated_at,
            "ready": self.ready,
        }


def detect_buy_sweep(
    buy_notional_window: float,
    avg_trade_notional: float,
    *,
    mult: float = 3.0,
) -> bool:
    """True when short-window aggressive buy notional > mult × rolling avg trade size."""
    if avg_trade_notional <= 0 or mult <= 0:
        return False
    return float(buy_notional_window) > float(mult) * float(avg_trade_notional)


def detect_liq_cascade(
    liq_events_in_window: int,
    *,
    min_cluster: int = 5,
) -> bool:
    return int(liq_events_in_window) >= int(min_cluster)


def funding_long_trap(
    funding_rate: Optional[float],
    *,
    price_stagnant_or_down: bool,
    funding_block_threshold: float = 0.0003,
) -> Tuple[bool, str]:
    """
    Block BUY when funding > +0.03% AND price stagnant/dropping.
    Returns (block, tag_or_empty).
    """
    if funding_rate is None:
        return False, ""
    if float(funding_rate) > float(funding_block_threshold) and price_stagnant_or_down:
        return True, "funding_long_trap"
    return False, ""


def funding_short_squeeze_boost(
    funding_rate: Optional[float],
    oi_delta_pct: Optional[float],
    *,
    funding_boost_threshold: float = -0.0001,
    oi_surge_pct: float = 0.02,
) -> Tuple[bool, str]:
    """Boost when funding < -0.01% AND OI surging."""
    if funding_rate is None or oi_delta_pct is None:
        return False, ""
    if float(funding_rate) < float(funding_boost_threshold) and float(oi_delta_pct) > float(
        oi_surge_pct
    ):
        return True, "funding_short_squeeze_boost"
    return False, ""


def bars_stagnant_or_down(bars: Sequence[Any], n: int = 3) -> bool:
    """True if last N bars close <= open or cumulative return <= 0."""
    if not bars or n <= 0:
        return False
    tail = list(bars)[-n:]
    if not tail:
        return False

    def _f(b: Any, k: str) -> float:
        if isinstance(b, dict):
            return float(b.get(k) or 0)
        return float(getattr(b, k, 0) or 0)

    closes = [_f(b, "close") for b in tail]
    opens = [_f(b, "open") for b in tail]
    if all(c <= o for c, o in zip(closes, opens)):
        return True
    if opens[0] > 0 and (closes[-1] - opens[0]) / opens[0] <= 0:
        return True
    return False


class PerpLeadLagEngine:
    """
    Lightweight async WS listeners for Binance/Bybit BTC & ETH perp aggTrades + liquidations.
    Thread-safe snapshot; WS failures log and degrade (never block the bot).
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        venues: str = "binance,bybit",
        symbols: Sequence[str] = ("BTC-USD", "ETH-USD"),
        sweep_mult: float = 3.0,
        sweep_window_sec: float = 5.0,
        liq_window_sec: float = 10.0,
        liq_min_cluster: int = 5,
        signal_ttl_sec: float = 30.0,
        avg_trade_window: int = 200,
        on_signal: Optional[Callable[[LeadLagSnapshot], None]] = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.venues = [v.strip().lower() for v in (venues or "binance").split(",") if v.strip()]
        self.spot_symbols = [s.upper() for s in symbols]
        self.perp_symbols = [coinbase_to_perp(s) for s in self.spot_symbols]
        self.perp_symbols = [p for p in self.perp_symbols if p]
        self.sweep_mult = float(sweep_mult)
        self.sweep_window_sec = float(sweep_window_sec)
        self.liq_window_sec = float(liq_window_sec)
        self.liq_min_cluster = int(liq_min_cluster)
        self.signal_ttl_sec = float(signal_ttl_sec)
        self.avg_trade_window = int(avg_trade_window)
        self.on_signal = on_signal

        self._lock = threading.RLock()
        self._buy_events: Dict[str, Deque[Tuple[float, float]]] = defaultdict(deque)  # ts, notional
        self._trade_sizes: Dict[str, Deque[float]] = defaultdict(deque)
        self._liq_events: Dict[str, Deque[float]] = defaultdict(deque)  # ts
        # CVD: signed notional ticks (ts, signed_usd); buy aggressor +, sell aggressor -
        self._cvd_events: Dict[str, Deque[Tuple[float, float]]] = defaultdict(deque)
        self._cvd_seen: Dict[str, bool] = defaultdict(bool)
        self._cvd_updated_at: Dict[str, float] = {}
        # Liq notional 1m: (ts, notional) per side (short = shorts liquidated / forced buy)
        self._short_liq_events: Dict[str, Deque[Tuple[float, float]]] = defaultdict(deque)
        self._long_liq_events: Dict[str, Deque[Tuple[float, float]]] = defaultdict(deque)
        self._liq_notional_seen: Dict[str, bool] = defaultdict(bool)
        self._liq_notional_updated_at: Dict[str, float] = {}
        self.cvd_window_1m_sec: float = 60.0
        self.cvd_window_5m_sec: float = 300.0
        self.liq_notional_window_sec: float = 60.0
        self._sweep_until: Dict[str, float] = {}
        self._liq_until: Dict[str, float] = {}
        self._tasks: List[asyncio.Task] = []
        self._stop = asyncio.Event()
        self._running = False

    def get_snapshot(self) -> LeadLagSnapshot:
        now = time.time()
        with self._lock:
            sweeps = tuple(
                s for s, until in self._sweep_until.items() if until > now
            )
            liqs = tuple(s for s, until in self._liq_until.items() if until > now)
            sweep_ts = max((self._sweep_until[s] - self.signal_ttl_sec for s in sweeps), default=None)
            liq_ts = max((self._liq_until[s] - self.signal_ttl_sec for s in liqs), default=None)
            return LeadLagSnapshot(
                recent_buy_sweep=bool(sweeps),
                recent_liq_cascade=bool(liqs),
                sweep_symbols=sweeps,
                liq_symbols=liqs,
                sweep_ts=sweep_ts,
                liq_ts=liq_ts,
                updated_at=now,
            )

    def get_cvd_snapshot(self, coinbase_symbol: str) -> Dict[str, Any]:
        """Non-blocking snapshot of rolling 1m/5m CVD deltas for a Coinbase spot symbol."""
        spot = (coinbase_symbol or "").strip().upper().replace("/", "-")
        now = time.time()
        with self._lock:
            q = self._cvd_events.get(spot)
            cvd_1m = 0.0
            cvd_5m = 0.0
            if q:
                cut1 = now - self.cvd_window_1m_sec
                cut5 = now - self.cvd_window_5m_sec
                # Trim beyond 5m while summing
                while q and q[0][0] < cut5:
                    q.popleft()
                for ts, signed in q:
                    if ts >= cut5:
                        cvd_5m += signed
                    if ts >= cut1:
                        cvd_1m += signed
            ready = bool(self._cvd_seen.get(spot))
            updated = float(self._cvd_updated_at.get(spot) or 0.0)
            return CvdSnapshot(
                symbol=spot,
                cvd_1m=float(cvd_1m),
                cvd_5m=float(cvd_5m),
                updated_at=updated or now,
                ready=ready,
            ).as_dict()

    def get_liq_snapshot(self, symbol: str) -> Dict[str, Any]:
        """Non-blocking snapshot of rolling 1m short/long liquidation notional (USD)."""
        spot = (symbol or "").strip().upper().replace("/", "-")
        now = time.time()
        with self._lock:
            ncut = now - self.liq_notional_window_sec

            def _sum_trim(q: Deque[Tuple[float, float]]) -> float:
                while q and q[0][0] < ncut:
                    q.popleft()
                return float(sum(n for _, n in q))

            short_q = self._short_liq_events.get(spot) or deque()
            long_q = self._long_liq_events.get(spot) or deque()
            # Ensure we mutate the stored deques when present
            if spot in self._short_liq_events:
                short_usd = _sum_trim(self._short_liq_events[spot])
            else:
                short_usd = 0.0
            if spot in self._long_liq_events:
                long_usd = _sum_trim(self._long_liq_events[spot])
            else:
                long_usd = 0.0
            ready = bool(self._liq_notional_seen.get(spot))
            updated = float(self._liq_notional_updated_at.get(spot) or 0.0)
            return LiqNotionalSnapshot(
                symbol=spot,
                short_liq_1m_usd=float(short_usd),
                long_liq_1m_usd=float(long_usd),
                updated_at=updated or now,
                ready=ready,
            ).as_dict()

    def ingest_agg_trade(
        self,
        perp: str,
        *,
        qty: float,
        price: float,
        is_buyer_maker: bool,
        ts: Optional[float] = None,
    ) -> None:
        """Public helper / test seam. is_buyer_maker=False ⇒ aggressive buy (taker buy)."""
        spot = perp_to_coinbase(perp) or perp
        now = ts if ts is not None else time.time()
        notional = abs(float(qty) * float(price))
        with self._lock:
            sizes = self._trade_sizes[spot]
            sizes.append(notional)
            while len(sizes) > self.avg_trade_window:
                sizes.popleft()
            # Aggressive buy: buyer is taker ⇒ is_buyer_maker False
            # CVD: aggressor side — buyer maker ⇒ sell aggressor (−); else buy aggressor (+)
            if notional > 0:
                signed = -notional if is_buyer_maker else notional
                cvd_q = self._cvd_events[spot]
                cvd_q.append((now, signed))
                cvd_cut = now - self.cvd_window_5m_sec
                while cvd_q and cvd_q[0][0] < cvd_cut:
                    cvd_q.popleft()
                self._cvd_seen[spot] = True
                self._cvd_updated_at[spot] = now

            if not is_buyer_maker and notional > 0:
                buys = self._buy_events[spot]
                buys.append((now, notional))
                cutoff = now - self.sweep_window_sec
                while buys and buys[0][0] < cutoff:
                    buys.popleft()
                window_buy = sum(n for _, n in buys)
                avg = (sum(sizes) / len(sizes)) if sizes else 0.0
                if detect_buy_sweep(window_buy, avg, mult=self.sweep_mult):
                    was_hot = self._sweep_until.get(spot, 0) > now
                    self._sweep_until[spot] = now + self.signal_ttl_sec
                    if not was_hot:
                        logger.info(
                            "PERP_LEADLAG buy_sweep %s window_buy=%.0f avg_trade=%.0f mult=%.1f",
                            spot,
                            window_buy,
                            avg,
                            self.sweep_mult,
                        )
                        self._emit()

    def ingest_liquidation(
        self,
        perp: str,
        *,
        side: Optional[str] = None,
        qty: float = 0.0,
        price: float = 0.0,
        notional: Optional[float] = None,
        ts: Optional[float] = None,
    ) -> None:
        """
        Ingest a force-order / liquidation.

        ``side`` is the liquidation *order* side (Binance ``o.S`` / Bybit ``side``):
          - BUY  ⇒ short was liquidated (forced buy) → counts toward short_liq
          - SELL ⇒ long was liquidated (forced sell) → counts toward long_liq
        """
        spot = perp_to_coinbase(perp) or perp
        now = ts if ts is not None else time.time()
        if notional is None:
            notional = abs(float(qty or 0.0) * float(price or 0.0))
        else:
            notional = abs(float(notional))
        side_u = (side or "").strip().upper()
        with self._lock:
            ev = self._liq_events[spot]
            ev.append(now)
            cutoff = now - self.liq_window_sec
            while ev and ev[0] < cutoff:
                ev.popleft()

            # Side + notional rolling 1m windows (non-blocking in-memory update)
            if notional > 0 and side_u in ("BUY", "SELL"):
                ncut = now - self.liq_notional_window_sec
                if side_u == "BUY":
                    q = self._short_liq_events[spot]
                    q.append((now, notional))
                    while q and q[0][0] < ncut:
                        q.popleft()
                else:
                    q = self._long_liq_events[spot]
                    q.append((now, notional))
                    while q and q[0][0] < ncut:
                        q.popleft()
                self._liq_notional_seen[spot] = True
                self._liq_notional_updated_at[spot] = now

            if detect_liq_cascade(len(ev), min_cluster=self.liq_min_cluster):
                was_hot = self._liq_until.get(spot, 0) > now
                self._liq_until[spot] = now + self.signal_ttl_sec
                if not was_hot:
                    logger.info(
                        "PERP_LEADLAG liq_cascade %s count=%d window=%.0fs",
                        spot,
                        len(ev),
                        self.liq_window_sec,
                    )
                    self._emit()

    def _emit(self) -> None:
        if self.on_signal is None:
            return
        try:
            self.on_signal(self.get_snapshot())
        except Exception as exc:
            logger.debug("leadlag on_signal: %s", exc)

    def clear_buffers(self) -> None:
        """Trim WebSocket trade/liq event deques and expired signal maps."""
        with self._lock:
            self._buy_events.clear()
            self._trade_sizes.clear()
            self._liq_events.clear()
            self._cvd_events.clear()
            self._cvd_seen.clear()
            self._cvd_updated_at.clear()
            self._short_liq_events.clear()
            self._long_liq_events.clear()
            self._liq_notional_seen.clear()
            self._liq_notional_updated_at.clear()
            self._sweep_until.clear()
            self._liq_until.clear()
        logger.debug("PerpLeadLagEngine buffers cleared")

    async def start(self) -> None:
        if not self.enabled:
            logger.info("PERP_LEADLAG disabled")
            return
        if self._running:
            return
        self._running = True
        self._stop.clear()
        if "binance" in self.venues:
            self._tasks.append(asyncio.create_task(self._run_binance(), name="perp_binance_ws"))
        if "bybit" in self.venues:
            self._tasks.append(asyncio.create_task(self._run_bybit(), name="perp_bybit_ws"))
        logger.info(
            "PERP_LEADLAG armed venues=%s perps=%s sweep_mult=%.1f window=%.0fs",
            ",".join(self.venues),
            ",".join(self.perp_symbols),
            self.sweep_mult,
            self.sweep_window_sec,
        )

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._running = False

    async def _run_binance(self) -> None:
        import websockets

        streams = []
        for p in self.perp_symbols:
            low = p.lower()
            streams.append(f"{low}@aggTrade")
            streams.append(f"{low}@forceOrder")
        if not streams:
            return
        url = "wss://fstream.binance.com/stream?streams=" + "/".join(streams)
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    logger.info("PERP_LEADLAG Binance WS connected")
                    backoff = 1.0
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        try:
                            msg = json.loads(raw)
                            data = msg.get("data") or msg
                            et = data.get("e")
                            if et == "aggTrade":
                                self.ingest_agg_trade(
                                    str(data.get("s") or ""),
                                    qty=float(data.get("q") or 0),
                                    price=float(data.get("p") or 0),
                                    is_buyer_maker=bool(data.get("m")),
                                    ts=float(data.get("T") or 0) / 1000.0
                                    if data.get("T")
                                    else None,
                                )
                            elif et == "forceOrder":
                                o = data.get("o") or {}
                                self.ingest_liquidation(
                                    str(o.get("s") or data.get("s") or ""),
                                    side=str(o.get("S") or ""),
                                    qty=float(o.get("q") or o.get("l") or 0),
                                    price=float(o.get("ap") or o.get("p") or 0),
                                    ts=float(o.get("T") or 0) / 1000.0 if o.get("T") else None,
                                )
                        except Exception as exc:
                            logger.debug("binance msg parse: %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("PERP_LEADLAG Binance WS failed (degrade): %s", exc)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(60.0, backoff * 2)

    async def _run_bybit(self) -> None:
        import websockets

        url = "wss://stream.bybit.com/v5/public/linear"
        topics = []
        for p in self.perp_symbols:
            topics.append(f"publicTrade.{p}")
            topics.append(f"allLiquidation.{p}")
        if not topics:
            return
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    await ws.send(json.dumps({"op": "subscribe", "args": topics}))
                    logger.info("PERP_LEADLAG Bybit WS connected")
                    backoff = 1.0
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        try:
                            msg = json.loads(raw)
                            topic = str(msg.get("topic") or "")
                            data = msg.get("data")
                            if topic.startswith("publicTrade.") and isinstance(data, list):
                                perp = topic.split(".", 1)[-1]
                                for t in data:
                                    # Bybit: S=Buy means taker buy
                                    side = str(t.get("S") or t.get("side") or "")
                                    is_buyer_maker = side.lower() != "buy"
                                    self.ingest_agg_trade(
                                        perp,
                                        qty=float(t.get("v") or t.get("size") or 0),
                                        price=float(t.get("p") or t.get("price") or 0),
                                        is_buyer_maker=is_buyer_maker,
                                        ts=float(t.get("T") or 0) / 1000.0 if t.get("T") else None,
                                    )
                            elif topic.startswith("allLiquidation.") and data is not None:
                                perp = topic.split(".", 1)[-1]
                                rows = data if isinstance(data, list) else [data]
                                for row in rows:
                                    if not isinstance(row, dict):
                                        self.ingest_liquidation(perp)
                                        continue
                                    self.ingest_liquidation(
                                        perp,
                                        side=str(row.get("side") or row.get("S") or ""),
                                        qty=float(row.get("size") or row.get("v") or 0),
                                        price=float(row.get("price") or row.get("p") or 0),
                                        ts=(
                                            float(row.get("updatedTime") or row.get("T") or 0)
                                            / 1000.0
                                            if (row.get("updatedTime") or row.get("T"))
                                            else None
                                        ),
                                    )
                        except Exception as exc:
                            logger.debug("bybit msg parse: %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("PERP_LEADLAG Bybit WS failed (degrade): %s", exc)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(60.0, backoff * 2)


class FundingOIFilter:
    """Poll public funding + OI; cache; expose block/boost tags for allowlisted spots."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        symbols: Sequence[str],
        poll_seconds: float = 60.0,
        funding_block_threshold: float = 0.0003,
        funding_boost_threshold: float = -0.0001,
        oi_surge_pct: float = 0.02,
        stagnant_bars: int = 3,
        venue: str = "binance",
    ) -> None:
        self.enabled = bool(enabled)
        self.symbols = [s.upper() for s in symbols]
        self.poll_seconds = float(poll_seconds)
        self.funding_block_threshold = float(funding_block_threshold)
        self.funding_boost_threshold = float(funding_boost_threshold)
        self.oi_surge_pct = float(oi_surge_pct)
        self.stagnant_bars = int(stagnant_bars)
        self.venue = (venue or "binance").lower()
        self._lock = threading.RLock()
        self._cache: Dict[str, FundingOISnapshot] = {}
        self._prev_oi: Dict[str, float] = {}
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    def get(self, symbol: str) -> Optional[FundingOISnapshot]:
        with self._lock:
            return self._cache.get(symbol.upper())

    def clear_buffers(self) -> None:
        """Drop funding/OI cache entries (refreshed on next poll)."""
        with self._lock:
            self._cache.clear()
            # keep _prev_oi for delta continuity across maintenance
        logger.debug("FundingOIFilter cache cleared")

    def evaluate(
        self,
        symbol: str,
        bars: Sequence[Any],
        *,
        confidence: float = 0.0,
    ) -> Tuple[bool, float, str]:
        """
        Returns (allow_buy, confidence_delta, tag).
        allow_buy False ⇒ block with funding_long_trap.
        """
        if not self.enabled:
            return True, 0.0, ""
        snap = self.get(symbol)
        if snap is None or snap.funding_rate is None:
            return True, 0.0, ""
        stagnant = bars_stagnant_or_down(bars, n=self.stagnant_bars)
        block, tag = funding_long_trap(
            snap.funding_rate,
            price_stagnant_or_down=stagnant,
            funding_block_threshold=self.funding_block_threshold,
        )
        if block:
            return False, 0.0, tag
        boost, btag = funding_short_squeeze_boost(
            snap.funding_rate,
            snap.oi_delta_pct,
            funding_boost_threshold=self.funding_boost_threshold,
            oi_surge_pct=self.oi_surge_pct,
        )
        if boost:
            return True, 8.0, btag
        return True, 0.0, ""

    async def start(self) -> None:
        if not self.enabled:
            logger.info("FUNDING_OI filter disabled")
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._poll_loop(), name="funding_oi_poll")
        logger.info(
            "FUNDING_OI armed poll=%.0fs block>%.4f boost<%.4f oi_surge>%.1f%%",
            self.poll_seconds,
            self.funding_block_threshold,
            self.funding_boost_threshold,
            self.oi_surge_pct * 100,
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _poll_loop(self) -> None:
        await self.refresh_once()
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                break
            try:
                await self.refresh_once()
            except Exception as exc:
                logger.warning("FUNDING_OI poll failed (degrade): %s", exc)

    async def refresh_once(self) -> None:
        for spot in self.symbols:
            perp = coinbase_to_perp(spot)
            if not perp:
                continue
            try:
                funding, oi = await self._fetch_binance(perp)
            except Exception as exc:
                logger.debug("funding fetch %s: %s", spot, exc)
                continue
            oi_delta = None
            with self._lock:
                prev = self._prev_oi.get(spot)
                if prev and prev > 0 and oi is not None:
                    oi_delta = (float(oi) - float(prev)) / float(prev)
                if oi is not None:
                    self._prev_oi[spot] = float(oi)
                self._cache[spot] = FundingOISnapshot(
                    symbol=spot,
                    perp=perp,
                    funding_rate=funding,
                    open_interest=oi,
                    oi_delta_pct=oi_delta,
                    fetched_at=time.time(),
                )

    async def _fetch_binance(self, perp: str) -> Tuple[Optional[float], Optional[float]]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            fr = await client.get(
                "https://fapi.binance.com/fapi/v1/premiumIndex",
                params={"symbol": perp},
            )
            fr.raise_for_status()
            fj = fr.json()
            funding = float(fj.get("lastFundingRate")) if fj.get("lastFundingRate") is not None else None
            oi_r = await client.get(
                "https://fapi.binance.com/fapi/v1/openInterest",
                params={"symbol": perp},
            )
            oi_r.raise_for_status()
            oj = oi_r.json()
            oi = float(oj.get("openInterest")) if oj.get("openInterest") is not None else None
            return funding, oi
