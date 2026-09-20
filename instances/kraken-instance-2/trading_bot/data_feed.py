"""Market data feed: bars/quotes via broker, OHLCV frames, indicators, stale detection."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Deque, Dict, List, Optional

import pandas as pd

from trading_bot.brokers.base import BrokerAdapter
from trading_bot.config import Settings
from trading_bot.models import Bar, HigherTimeframeContext, IndicatorSnapshot, Quote, utcnow
from trading_bot.utils.indicators import (
    compute_indicators,
    ema_slope,
    ema_value,
    latest_adx_chop,
    latest_indicator_dict,
    support_resistance_from_swings,
    swing_highs_lows,
)
from trading_bot.utils.retry import ExponentialBackoff

logger = logging.getLogger(__name__)


class DataFeed:
    """
    Builds per-symbol OHLCV frames, computes indicators, detects stale data,
    and reconnects with exponential backoff.
    """

    def __init__(self, broker: BrokerAdapter, settings: Settings) -> None:
        self.broker = broker
        self.settings = settings
        self._frames: Dict[str, pd.DataFrame] = {}
        self._last_bar_ts: Dict[str, datetime] = {}
        self._last_quote: Dict[str, Quote] = {}
        self._healthy = True
        self._lock = asyncio.Lock()
        self._backoff = ExponentialBackoff(base=1.0, max_delay=60.0)
        self._htf: Dict[str, HigherTimeframeContext] = {}
        self._htf_fetched_at: Dict[str, float] = {}  # monotonic ts for cache TTL
        self._frames_5m: Dict[str, pd.DataFrame] = {}
        self._l2_cache: Dict[str, tuple[float, Optional[float]]] = {}  # symbol -> (ts, ratio)
        self._last_market_fetch_mono: float = 0.0
        maxlen = int(getattr(settings, "bar_buffer_maxlen", 200) or 200)
        self._bar_buf_maxlen = max(50, maxlen)
        self._bar_buffers: Dict[str, Deque[Bar]] = {}
        self._rest_fallback_count: int = 0

    @property
    def healthy(self) -> bool:
        return self._healthy

    @property
    def last_tick_age_seconds(self) -> Optional[float]:
        """Prefer broker heartbeat; fall back to last successful feed refresh."""
        try:
            if hasattr(self.broker, "last_tick_age_seconds"):
                age = self.broker.last_tick_age_seconds
                if age is not None:
                    return float(age)
        except Exception:
            pass
        if self._last_market_fetch_mono <= 0:
            return None
        return max(0.0, time.monotonic() - self._last_market_fetch_mono)

    def clear_stream_buffers(self) -> None:
        """Trim/clear HTF + L2 + frame caches (periodic maintenance; bar state rebuilds)."""
        self._htf.clear()
        self._htf_fetched_at.clear()
        self._l2_cache.clear()
        # Keep last-quote / last-bar timestamps for staleness; drop heavy frames
        self._frames.clear()
        self._frames_5m.clear()
        self._bar_buffers.clear()
        logger.debug("DataFeed stream buffers cleared")

    def _bars_to_frame(self, bars: List[Bar]) -> pd.DataFrame:
        if not bars:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "vwap"])
        rows = [
            {
                "timestamp": b.timestamp,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
                "vwap": b.vwap,
            }
            for b in bars
        ]
        df = pd.DataFrame(rows).set_index("timestamp").sort_index()
        return df


    def _buffer_for(self, symbol: str) -> Deque[Bar]:
        symbol = symbol.upper()
        buf = self._bar_buffers.get(symbol)
        if buf is None:
            buf = deque(maxlen=self._bar_buf_maxlen)
            self._bar_buffers[symbol] = buf
        return buf

    def _frame_from_buffer(self, symbol: str) -> Optional[pd.DataFrame]:
        buf = self._bar_buffers.get(symbol.upper())
        if not buf:
            return None
        return self._bars_to_frame(list(buf))

    def buffer_warm(self, symbol: str, *, min_bars: int = 50) -> bool:
        buf = self._bar_buffers.get(symbol.upper())
        return bool(buf) and len(buf) >= int(min_bars)

    async def refresh_symbol(self, symbol: str) -> IndicatorSnapshot:
        symbol = symbol.upper()
        try:
            min_warm = min(50, int(getattr(self.settings, "lookback_bars", 100) or 100))
            use_buffer = self.buffer_warm(symbol, min_bars=min_warm) and not self.is_stale(symbol)
            bars: List[Bar] = []
            if use_buffer:
                bars = list(self._bar_buffers[symbol])
            else:
                bars = await self.broker.get_bars(
                    symbol,
                    timeframe=self.settings.bar_timeframe,
                    limit=self.settings.lookback_bars,
                )
                self._rest_fallback_count += 1
                # Seed rolling buffer from REST so subsequent ticks stay in-memory
                buf = self._buffer_for(symbol)
                buf.clear()
                for b in bars[-self._bar_buf_maxlen :]:
                    buf.append(b)

            quote: Optional[Quote] = None
            try:
                quote = await self.broker.get_quote(symbol)
                self._last_quote[symbol] = quote
            except Exception as exc:
                logger.debug("quote fetch failed for %s: %s", symbol, exc)

            async with self._lock:
                df = self._bars_to_frame(bars) if bars else self._frame_from_buffer(symbol)
                if df is None:
                    df = pd.DataFrame(columns=["open", "high", "low", "close", "volume", "vwap"])
                if not df.empty:
                    df = compute_indicators(df)
                    # volume ratio vs 20-bar mean
                    if len(df) >= 20:
                        mean_vol = float(df["volume"].tail(20).mean())
                        if mean_vol > 0:
                            df["volume_ratio"] = df["volume"] / mean_vol
                    self._frames[symbol] = df
                    last_ts = df.index[-1]
                    if isinstance(last_ts, datetime):
                        self._last_bar_ts[symbol] = (
                            last_ts if last_ts.tzinfo else last_ts.replace(tzinfo=timezone.utc)
                        )
                    else:
                        self._last_bar_ts[symbol] = utcnow()

            self._backoff.reset()
            self._healthy = True
            self._last_market_fetch_mono = time.monotonic()
            return self.get_indicators(symbol)
        except Exception as exc:
            self._healthy = False
            delay = self._backoff.next_delay()
            logger.error("refresh_symbol(%s) failed: %s — backoff %.1fs", symbol, exc, delay)
            await asyncio.sleep(delay)
            raise

    def get_frame(self, symbol: str) -> Optional[pd.DataFrame]:
        return self._frames.get(symbol.upper())

    def get_frame_5m(self, symbol: str) -> Optional[pd.DataFrame]:
        """Last refreshed 5m OHLCV frame (may include a still-forming last bar)."""
        return self._frames_5m.get(symbol.upper())

    def get_indicators(self, symbol: str) -> IndicatorSnapshot:
        symbol = symbol.upper()
        df = self._frames.get(symbol)
        if df is None or df.empty:
            return IndicatorSnapshot(symbol=symbol, close=0.0, volume=0.0)

        vals = latest_indicator_dict(df)
        extras = {}
        if "volume_ratio" in df.columns:
            vr = df["volume_ratio"].iloc[-1]
            try:
                extras["volume_ratio"] = float(vr)
            except (TypeError, ValueError):
                pass

        # MACD hist previous bar (momentum flip detection)
        if "macd_hist" in df.columns and len(df) >= 2:
            try:
                prev = df["macd_hist"].iloc[-2]
                if prev == prev:  # not NaN
                    extras["macd_hist_prev"] = float(prev)
            except (TypeError, ValueError):
                pass

        # VWAP reclaim: did close cross above VWAP in last 1–3 bars?
        if "vwap" in df.columns and "close" in df.columns and len(df) >= 2:
            try:
                closes = df["close"].astype(float)
                vwaps = df["vwap"].astype(float)
                n = min(4, len(df))
                above_hist = []
                for i in range(-n, 0):
                    c = float(closes.iloc[i])
                    v = float(vwaps.iloc[i])
                    if v != v or v <= 0:  # NaN
                        above_hist.append(False)
                    else:
                        above_hist.append(c >= v)
                extras["close_above_vwap_hist"] = above_hist
                # Bars since reclaim: look back up to 3 for False→True transition ending current above
                reclaim_bars = None
                if above_hist and above_hist[-1]:
                    for lookback in range(1, min(4, len(above_hist))):
                        # check transition within last `lookback` steps
                        window = above_hist[-(lookback + 1) :]
                        if len(window) >= 2 and (not window[0]) and window[-1]:
                            reclaim_bars = lookback
                            break
                        # also: any below then above in window
                        if any(not x for x in window[:-1]) and window[-1]:
                            reclaim_bars = lookback
                            break
                if reclaim_bars is not None:
                    extras["vwap_reclaim_bars"] = int(reclaim_bars)
                    extras["vwap_crossed_up"] = True
            except (TypeError, ValueError):
                pass

        # Regime: ADX + Choppiness on entry timeframe bars
        try:
            adx_period = int(getattr(self.settings, "adx_period", 14) or 14)
            chop_period = int(getattr(self.settings, "chop_period", 14) or 14)
            adx_v, chop_v = latest_adx_chop(df, adx_period=adx_period, chop_period=chop_period)
            if adx_v is not None:
                extras["adx"] = adx_v
            if chop_v is not None:
                extras["chop"] = chop_v
                extras["choppiness"] = chop_v
        except Exception as exc:
            logger.debug("ADX/Chop compute skipped for %s: %s", symbol, exc)

        # Explicit 1m EMA20/EMA50 short trigger data (separate from the legacy 9/21 EMA fields).
        try:
            close_s = df["close"].astype(float)
            ema20 = ema_value(close_s, 20)
            ema50 = ema_value(close_s, 50)
            if len(close_s):
                extras["ema20_1m"] = float(ema20.iloc[-1])
                extras["ema50_1m"] = float(ema50.iloc[-1])
                cross20_50 = "none"
                if len(close_s) >= 2 and all(pd.notna(x) for x in (ema20.iloc[-1], ema50.iloc[-1], ema20.iloc[-2], ema50.iloc[-2])):
                    if ema20.iloc[-1] < ema50.iloc[-1] and ema20.iloc[-2] >= ema50.iloc[-2]:
                        cross20_50 = "bearish"
                    elif ema20.iloc[-1] > ema50.iloc[-1] and ema20.iloc[-2] <= ema50.iloc[-2]:
                        cross20_50 = "bullish"
                extras["ema20_50_cross"] = cross20_50
        except Exception:
            pass

        ts = self._last_bar_ts.get(symbol, utcnow())
        return IndicatorSnapshot(
            symbol=symbol,
            timestamp=ts,
            close=float(vals.get("close") or 0),
            volume=float(vals.get("volume") or 0),
            vwap=vals.get("vwap"),
            rsi=vals.get("rsi"),
            macd=vals.get("macd"),
            macd_signal=vals.get("macd_signal"),
            macd_hist=vals.get("macd_hist"),
            ema_fast=vals.get("ema_fast"),
            ema_slow=vals.get("ema_slow"),
            atr=vals.get("atr"),
            ema_cross=vals.get("ema_cross"),
            extras=extras,
        )

    def get_quote(self, symbol: str) -> Optional[Quote]:
        return self._last_quote.get(symbol.upper())

    def is_stale(self, symbol: str, now: Optional[datetime] = None) -> bool:
        now = now or utcnow()
        ts = self._last_bar_ts.get(symbol.upper())
        if ts is None:
            return True
        age = (now - ts).total_seconds()
        return age > self.settings.stale_data_seconds

    async def refresh_all(self, symbols: Optional[List[str]] = None) -> Dict[str, IndicatorSnapshot]:
        symbols = symbols or self.settings.symbol_list
        out: Dict[str, IndicatorSnapshot] = {}
        conc = int(getattr(self.settings, "scan_concurrency", 6) or 6)
        conc = max(1, min(16, conc))
        sem = asyncio.Semaphore(conc)

        async def _one(sym: str) -> None:
            async with sem:
                try:
                    out[sym] = await self.refresh_symbol(sym)
                    if self.is_stale(sym):
                        logger.warning("Stale data for %s after refresh", sym)
                except Exception:
                    logger.exception("Failed to refresh %s", sym)

        await asyncio.gather(*[_one(s) for s in symbols])
        return out

    async def ensure_fresh(self, symbol: str) -> IndicatorSnapshot:
        """Abort/reconnect path: if stale, force refresh with backoff."""
        # Tick/L2 heartbeat (STALE_TICK_SECONDS) — reconnect client if needed
        ensure_md = getattr(self.broker, "ensure_market_data_fresh", None)
        if callable(ensure_md):
            try:
                await ensure_md()
            except Exception as exc:
                logger.warning("market-data heartbeat check failed: %s", exc)
        if self.is_stale(symbol) or symbol.upper() not in self._frames:
            logger.info("Stale/missing data for %s — forcing refresh", symbol)
            return await self.refresh_symbol(symbol)
        return self.get_indicators(symbol)

    def on_bar(self, bar: Bar) -> None:
        """Callback for live WS bars — append to rolling buffer and recompute."""
        symbol = bar.symbol.upper()
        buf = self._buffer_for(symbol)
        buf.append(bar)
        row = {
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "vwap": bar.vwap,
        }
        df = self._frames.get(symbol)
        new_row = pd.DataFrame([row], index=pd.DatetimeIndex([bar.timestamp]))
        if df is None or df.empty:
            # Prefer full buffer rebuild when available
            df_buf = self._frame_from_buffer(symbol)
            df = df_buf if df_buf is not None and not df_buf.empty else new_row
        else:
            df = pd.concat([df, new_row])
            df = df[~df.index.duplicated(keep="last")].tail(self.settings.lookback_bars)
        try:
            df = compute_indicators(df)
            if len(df) >= 20 and "volume" in df.columns:
                mean_vol = float(df["volume"].tail(20).mean())
                if mean_vol > 0:
                    df["volume_ratio"] = df["volume"] / mean_vol
            self._frames[symbol] = df
            self._last_bar_ts[symbol] = (
                bar.timestamp
                if bar.timestamp.tzinfo
                else bar.timestamp.replace(tzinfo=timezone.utc)
            )
            self._healthy = True
            self._last_market_fetch_mono = time.monotonic()
        except Exception as exc:
            logger.warning("on_bar indicator update failed: %s", exc)


    async def refresh_htf(self, symbol: str) -> HigherTimeframeContext:
        """Fetch 1h (+ 4h) bars: EMA200 slope, swing S/R, and MTF EMA200s."""
        symbol = symbol.upper()
        tf = self.settings.htf_timeframe
        limit = self.settings.htf_lookback_bars
        ema_period = int(getattr(self.settings, "htf_ema_period", 200) or 200)
        bars = await self.broker.get_bars(symbol, timeframe=tf, limit=limit)
        df = self._bars_to_frame(bars)
        if df.empty:
            ctx = HigherTimeframeContext(timeframe=tf)
            self._htf[symbol] = ctx
            self._htf_fetched_at[symbol] = time.monotonic()
            return ctx

        close = df["close"].astype(float)
        ema200 = ema_value(close, ema_period)
        slope = ema_slope(close, ema_period, lookback=5)
        ema_now = None
        try:
            if len(ema200) and not pd.isna(ema200.iloc[-1]):
                ema_now = float(ema200.iloc[-1])
        except Exception:
            ema_now = None

        sh, sl = swing_highs_lows(df)
        last_close = float(close.iloc[-1])
        support, resistance = support_resistance_from_swings(last_close, sh, sl)

        # 4h EMA200 for multi-timeframe alignment (cached with same HTF refresh)
        ema_4h = None
        tf_4h = str(getattr(self.settings, "htf_4h_timeframe", "4Hour") or "4Hour")
        try:
            bars_4h = await self.broker.get_bars(
                symbol, timeframe=tf_4h, limit=max(limit, ema_period + 10)
            )
            df4 = self._bars_to_frame(bars_4h)
            if not df4.empty:
                ema4 = ema_value(df4["close"].astype(float), ema_period)
                if len(ema4) and not pd.isna(ema4.iloc[-1]):
                    ema_4h = float(ema4.iloc[-1])
        except Exception as exc:
            logger.debug("4h HTF fetch failed for %s: %s", symbol, exc)

        # 15m EMA200 is a soft long/short direction block. Missing data stays soft.
        ema_15m = None
        close_15m = None
        try:
            bars_15m = await self.broker.get_bars(
                symbol, timeframe="15Min", limit=max(ema_period + 10, 210)
            )
            df15 = self._bars_to_frame(bars_15m)
            if not df15.empty:
                close15 = df15["close"].astype(float)
                ema15 = ema_value(close15, ema_period)
                close_15m = float(close15.iloc[-1])
                if len(ema15) and not pd.isna(ema15.iloc[-1]):
                    ema_15m = float(ema15.iloc[-1])
        except Exception as exc:
            logger.debug("15m HTF fetch failed for %s: %s", symbol, exc)

        ctx = HigherTimeframeContext(
            timeframe=tf,
            ema_200=ema_now,
            ema_200_slope=slope,
            ema_200_1h=ema_now,
            ema_200_4h=ema_4h,
            ema_200_15m=ema_15m,
            swing_high=sh,
            swing_low=sl,
            support=support,
            resistance=resistance,
            extras={
                "ema_period": ema_period,
                "tf_4h": tf_4h,
                "close_1h": last_close,
                "close_15m": close_15m,
                "ema_200_15m": ema_15m,
            },
        )
        self._htf[symbol] = ctx
        self._htf_fetched_at[symbol] = time.monotonic()
        return ctx

    def get_htf(self, symbol: str) -> Optional[HigherTimeframeContext]:
        return self._htf.get(symbol.upper())

    async def ensure_htf(self, symbol: str) -> HigherTimeframeContext:
        """Return cached HTF, refreshing when missing or past HTF_CACHE_SECONDS."""
        symbol = symbol.upper()
        ttl = float(getattr(self.settings, "htf_cache_seconds", 300.0) or 300.0)
        fetched = self._htf_fetched_at.get(symbol)
        stale = fetched is None or (time.monotonic() - fetched) > ttl
        if symbol not in self._htf or stale:
            return await self.refresh_htf(symbol)
        return self._htf[symbol]

    async def fetch_l2_imbalance(self, symbol: str) -> Optional[float]:
        """Fetch (cached ~15s) L2 depth imbalance ratio via broker helper."""
        symbol = symbol.upper()
        now = time.monotonic()
        cached = self._l2_cache.get(symbol)
        if cached and (now - cached[0]) < 15.0:
            return cached[1]
        ratio: Optional[float] = None
        top5: Optional[float] = None
        try:
            band = float(getattr(self.settings, "l2_imbalance_band_pct", 0.005) or 0.005)
            from trading_bot.utils.decision_filters import l2_top_n_imbalance
            from trading_bot.utils.indicators import l2_depth_imbalance

            book_fn = getattr(self.broker, "get_l2_book", None)
            book = None
            if callable(book_fn):
                try:
                    book = await book_fn(symbol)
                except Exception:
                    book = None
            if isinstance(book, dict):
                ratio = l2_depth_imbalance(
                    book.get("bids") or [],
                    book.get("asks") or [],
                    mid=book.get("mid"),
                    band_pct=band,
                )
                top5 = l2_top_n_imbalance(
                    book.get("bids") or [], book.get("asks") or [], n=5
                )
            if ratio is None:
                getter = getattr(self.broker, "get_l2_imbalance", None)
                if callable(getter):
                    ratio = await getter(symbol, band_pct=band)
            if top5 is None and ratio is not None:
                top5 = ratio
        except Exception as exc:
            logger.debug("L2 imbalance fetch failed for %s: %s", symbol, exc)
            ratio = None
        self._l2_cache[symbol] = (now, ratio)
        if top5 is not None:
            self._l2_cache[f"{symbol}__top5"] = (now, top5)
        return ratio

    def get_l2_top5_ratio(self, symbol: str) -> Optional[float]:
        cached = self._l2_cache.get(f"{symbol.upper()}__top5")
        if cached:
            return cached[1]
        cached = self._l2_cache.get(symbol.upper())
        return cached[1] if cached else None

    async def refresh_entry_5m(self, symbol: str) -> Optional[pd.DataFrame]:
        """Optional 5m frame kept for scalp entry context (alongside 1m primary)."""
        symbol = symbol.upper()
        try:
            bars = await self.broker.get_bars(
                symbol,
                timeframe=self.settings.entry_timeframe_5m,
                limit=min(100, self.settings.lookback_bars),
            )
            df = self._bars_to_frame(bars)
            if not df.empty:
                df = compute_indicators(df)
                self._frames_5m[symbol] = df
            return self._frames_5m.get(symbol)
        except Exception as exc:
            logger.debug("5m refresh failed for %s: %s", symbol, exc)
            return None
