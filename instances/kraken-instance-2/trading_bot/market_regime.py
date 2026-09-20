"""BTC market-regime engine for Elite Day-Trader Architecture.

Scans BTC-USD on 15m: EMA(20), EMA(50).
- price < EMA(50) → BEAR_CHOP
- Also dump flag when BTC-USD dropped >1.2% in last 30 minutes.

Blocks altcoin LONG entries under BEAR_CHOP or dump; BTC itself may still trade.
SHORT alts are not blocked by default.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional, Sequence, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

STATE_BEAR_CHOP = "BEAR_CHOP"
STATE_BULL_OK = "BULL_OK"
SKIP_BEAR_CHOP = "btc_regime_bear_chop"
SKIP_DUMP_30M = "btc_dump_30m"

BTC_SYMBOL = "BTC-USD"
EMA_FAST = 20
EMA_SLOW = 50
DUMP_PCT = 0.012  # 1.2%
DUMP_LOOKBACK_MIN = 30
DEFAULT_CACHE_SEC = 60.0


def _ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=int(length), adjust=False).mean()


def _bars_to_frame(bars: Sequence[Any]) -> pd.DataFrame:
    rows = []
    for b in bars or []:
        if isinstance(b, dict):
            ts = b.get("timestamp") or b.get("t") or b.get("time")
            close = b.get("close") or b.get("c")
            rows.append(
                {
                    "timestamp": ts,
                    "open": float(b.get("open") or b.get("o") or close or 0),
                    "high": float(b.get("high") or b.get("h") or close or 0),
                    "low": float(b.get("low") or b.get("l") or close or 0),
                    "close": float(close or 0),
                    "volume": float(b.get("volume") or b.get("v") or 0),
                }
            )
        else:
            ts = getattr(b, "timestamp", None) or getattr(b, "t", None)
            close = getattr(b, "close", None)
            rows.append(
                {
                    "timestamp": ts,
                    "open": float(getattr(b, "open", close) or 0),
                    "high": float(getattr(b, "high", close) or 0),
                    "low": float(getattr(b, "low", close) or 0),
                    "close": float(close or 0),
                    "volume": float(getattr(b, "volume", 0) or 0),
                }
            )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if "timestamp" in df.columns:
        try:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
            df = df.sort_values("timestamp")
        except Exception:
            pass
    return df.reset_index(drop=True)


def compute_regime_from_bars(
    bars_15m: Sequence[Any],
    *,
    bars_1m: Optional[Sequence[Any]] = None,
    dump_pct: float = DUMP_PCT,
    dump_lookback_min: int = DUMP_LOOKBACK_MIN,
) -> Tuple[str, bool, dict]:
    """Return (state, dump_30m, meta) from BTC bars.

    state is BEAR_CHOP if close < EMA50 on 15m, else BULL_OK.
    dump_30m True when price dropped > dump_pct over ~30 minutes (prefer 1m bars).
    """
    meta: dict = {
        "price": None,
        "ema20": None,
        "ema50": None,
        "dump_pct": None,
        "state": STATE_BULL_OK,
        "dump_30m": False,
    }
    df15 = _bars_to_frame(bars_15m)
    if df15.empty or len(df15) < EMA_SLOW:
        meta["state"] = STATE_BULL_OK
        meta["note"] = "insufficient_15m"
        return STATE_BULL_OK, False, meta

    close = df15["close"].astype(float)
    ema20 = _ema(close, EMA_FAST)
    ema50 = _ema(close, EMA_SLOW)
    px = float(close.iloc[-1])
    e20 = float(ema20.iloc[-1])
    e50 = float(ema50.iloc[-1])
    meta["price"] = px
    meta["ema20"] = e20
    meta["ema50"] = e50

    state = STATE_BEAR_CHOP if (px > 0 and e50 > 0 and px < e50) else STATE_BULL_OK
    meta["state"] = state

    dump = False
    dump_move = None
    src = bars_1m if bars_1m else bars_15m
    df_d = _bars_to_frame(src)
    if not df_d.empty and len(df_d) >= 2:
        # Prefer last ~30 1m bars; else ~2x 15m bars
        lookback = int(dump_lookback_min) if bars_1m else max(2, int(dump_lookback_min) // 15)
        lookback = min(lookback, len(df_d) - 1)
        px_now = float(df_d["close"].iloc[-1])
        px_then = float(df_d["close"].iloc[-(lookback + 1)])
        if px_then > 0 and px_now > 0:
            dump_move = (px_now - px_then) / px_then
            meta["dump_pct"] = dump_move
            if dump_move <= -float(dump_pct):
                dump = True
    meta["dump_30m"] = dump
    return state, dump, meta


def is_tokenized_symbol(symbol: str) -> bool:
    """Kraken xStocks use a lowercase ``x`` suffix (for example AAPLx-USD)."""
    base = str(symbol or "").strip().replace("/", "-").replace("_", "-").split("-", 1)[0]
    return base.lower().endswith("x")


SKIP_XSTOCK_OUTSIDE_RTH = "xstock_outside_rth"
_NY = None


def _ny_tz():
    global _NY
    if _NY is None:
        from zoneinfo import ZoneInfo
        _NY = ZoneInfo("America/New_York")
    return _NY


def is_us_equity_rth(now=None) -> bool:
    """True Mon–Fri 9:30–16:00 America/New_York (EST/EDT via ZoneInfo)."""
    from datetime import datetime, time as dtime
    if now is None:
        now = datetime.now(_ny_tz())
    elif getattr(now, "tzinfo", None) is None:
        now = now.replace(tzinfo=_ny_tz())
    else:
        now = now.astimezone(_ny_tz())
    if now.weekday() >= 5:
        return False
    t = now.timetz() if hasattr(now, "timetz") else now.time()
    # compare as time without tz
    from datetime import time as dtime
    tt = now.time().replace(tzinfo=None) if getattr(now.time(), "tzinfo", None) else now.time()
    return dtime(9, 30) <= tt < dtime(16, 0)


def xstock_entry_allowed(symbol: str, now=None):
    """Crypto always ok; x-USD only during US RTH. Returns (ok, reason)."""
    if not is_tokenized_symbol(symbol):
        return True, ""
    if is_us_equity_rth(now):
        return True, ""
    return False, SKIP_XSTOCK_OUTSIDE_RTH



def is_xstock_weekend_closed(now=None) -> bool:
    """True Fri 20:00 ET → Sun 20:00 ET (xStock weekend lockout window)."""
    from datetime import datetime, time as dtime, timedelta
    if now is None:
        now = datetime.now(_ny_tz())
    elif getattr(now, "tzinfo", None) is None:
        now = now.replace(tzinfo=_ny_tz())
    else:
        now = now.astimezone(_ny_tz())
    wd = now.weekday()  # Mon=0 … Sun=6
    tt = now.time()
    if wd == 4 and tt >= dtime(20, 0):  # Friday after 8pm
        return True
    if wd == 5:  # all Saturday
        return True
    if wd == 6 and tt < dtime(20, 0):  # Sunday before 8pm
        return True
    return False


def seconds_until_friday_xstock_flush(now=None) -> float:
    """Seconds until next Friday 19:55 America/New_York."""
    from datetime import datetime, time as dtime, timedelta
    if now is None:
        now = datetime.now(_ny_tz())
    elif getattr(now, "tzinfo", None) is None:
        now = now.replace(tzinfo=_ny_tz())
    else:
        now = now.astimezone(_ny_tz())
    target_t = dtime(19, 55)
    # find next Friday
    days_ahead = (4 - now.weekday()) % 7
    cand = (now + timedelta(days=days_ahead)).replace(
        hour=19, minute=55, second=0, microsecond=0
    )
    if cand <= now:
        cand = cand + timedelta(days=7)
    return max(0.0, (cand - now).total_seconds())


def is_btc_symbol(symbol: str) -> bool:
    s = (symbol or "").strip().upper().replace("/", "-").replace("_", "-")
    return s in {"BTC-USD", "BTCUSD", "XBT-USD", "XXBTZUSD"}


def check_alt_long_allowed(
    symbol: str,
    *,
    state: str,
    dump_30m: bool,
    enabled: bool = True,
) -> Tuple[bool, str]:
    """Block LONG when BEAR_CHOP or BTC dump (crypto + xStocks). BTC always allowed."""
    if not enabled:
        return True, "btc_regime: disabled"
    if is_btc_symbol(symbol):
        return True, "btc_regime: btc_allowlisted"
    # Tokenized equities are independent from crypto BTC regime.
    if dump_30m:
        return False, SKIP_DUMP_30M
    if (state or "").upper() == STATE_BEAR_CHOP:
        return False, SKIP_BEAR_CHOP
    return True, "btc_regime: ok"


@dataclass
class RegimeSnapshot:
    state: str = STATE_BULL_OK
    dump_30m: bool = False
    meta: Optional[dict] = None
    updated_at: float = 0.0

    @property
    def status_line(self) -> str:
        return f"Market: {self.state}"


class BtcMarketRegimeEngine:
    """Cached BTC regime scanner; refresh via broker.get_bars."""

    def __init__(
        self,
        *,
        cache_seconds: float = DEFAULT_CACHE_SEC,
        dump_pct: float = DUMP_PCT,
        enabled: bool = True,
    ) -> None:
        self.cache_seconds = float(cache_seconds)
        self.dump_pct = float(dump_pct)
        self.enabled = bool(enabled)
        self._snap = RegimeSnapshot()
        self._lock_mono = 0.0

    @property
    def state(self) -> str:
        return self._snap.state

    @property
    def dump_30m(self) -> bool:
        return self._snap.dump_30m

    @property
    def status_line(self) -> str:
        return self._snap.status_line

    def snapshot(self) -> RegimeSnapshot:
        return self._snap

    async def refresh(self, broker: Any) -> RegimeSnapshot:
        if not self.enabled:
            self._snap = RegimeSnapshot(state=STATE_BULL_OK, dump_30m=False, updated_at=time.time())
            return self._snap
        now = time.monotonic()
        if self._snap.updated_at and (now - self._lock_mono) < self.cache_seconds:
            return self._snap
        bars_15m: list = []
        bars_1m: list = []
        try:
            bars_15m = await broker.get_bars(BTC_SYMBOL, timeframe="15Min", limit=max(EMA_SLOW + 20, 80))
        except Exception as exc:
            logger.debug("btc regime 15m fetch: %s", exc)
        try:
            bars_1m = await broker.get_bars(BTC_SYMBOL, timeframe="1Min", limit=max(DUMP_LOOKBACK_MIN + 5, 40))
        except Exception as exc:
            logger.debug("btc regime 1m fetch: %s", exc)
        state, dump, meta = compute_regime_from_bars(
            bars_15m, bars_1m=bars_1m or None, dump_pct=self.dump_pct
        )
        self._snap = RegimeSnapshot(
            state=state, dump_30m=dump, meta=meta, updated_at=time.time()
        )
        self._lock_mono = now
        logger.info(
            "BTC_REGIME state=%s dump_30m=%s price=%s ema50=%s dump_pct=%s",
            state,
            dump,
            meta.get("price"),
            meta.get("ema50"),
            meta.get("dump_pct"),
        )
        return self._snap

    def allow_long(self, symbol: str) -> Tuple[bool, str]:
        return check_alt_long_allowed(
            symbol,
            state=self._snap.state,
            dump_30m=self._snap.dump_30m,
            enabled=self.enabled,
        )
