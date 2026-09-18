"""Technical indicators: VWAP, RSI, MACD, EMA, ATR via pandas-ta with fallback."""

from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

try:
    import pandas_ta as ta  # type: ignore

    HAS_PANDAS_TA = True
except ImportError:  # pragma: no cover
    ta = None
    HAS_PANDAS_TA = False


def _rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def _macd(
    series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[pd.Series, pd.Series, pd.Series]:
    ema_fast = _ema(series, fast)
    ema_slow = _ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = _ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def _atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()


def _vwap(df: pd.DataFrame) -> pd.Series:
    if "vwap" in df.columns and df["vwap"].notna().any():
        return df["vwap"]
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["volume"].replace(0, np.nan)
    cum_tp_vol = (typical * vol).cumsum()
    cum_vol = vol.cumsum()
    return cum_tp_vol / cum_vol


def compute_indicators(
    df: pd.DataFrame,
    *,
    rsi_len: int = 14,
    ema_fast: int = 9,
    ema_slow: int = 21,
    atr_len: int = 14,
) -> pd.DataFrame:
    """
    Enrich OHLCV DataFrame with VWAP, RSI, MACD, EMA, ATR.

    Prefers pandas-ta when installed; otherwise uses pure pandas/numpy fallbacks.
    """
    if df is None or df.empty:
        return df.copy() if df is not None else pd.DataFrame()

    out = df.copy()
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(c.lower() for c in out.columns)
    # normalize column names to lower
    out.columns = [str(c).lower() for c in out.columns]
    if missing and not required.issubset(set(out.columns)):
        raise ValueError(f"OHLCV frame missing columns: {required - set(out.columns)}")

    if HAS_PANDAS_TA and ta is not None:
        try:
            out["rsi"] = ta.rsi(out["close"], length=rsi_len)
            macd_df = ta.macd(out["close"])
            if macd_df is not None and not macd_df.empty:
                cols = list(macd_df.columns)
                out["macd"] = macd_df[cols[0]]
                out["macd_hist"] = macd_df[cols[1]] if len(cols) > 1 else np.nan
                out["macd_signal"] = macd_df[cols[2]] if len(cols) > 2 else np.nan
            out["ema_fast"] = ta.ema(out["close"], length=ema_fast)
            out["ema_slow"] = ta.ema(out["close"], length=ema_slow)
            out["atr"] = ta.atr(out["high"], out["low"], out["close"], length=atr_len)
            vwap_s = ta.vwap(out["high"], out["low"], out["close"], out["volume"])
            out["vwap"] = vwap_s if vwap_s is not None else _vwap(out)
        except Exception:
            # fall through to pure implementation
            out = _fallback_indicators(out, rsi_len, ema_fast, ema_slow, atr_len)
    else:
        out = _fallback_indicators(out, rsi_len, ema_fast, ema_slow, atr_len)

    # EMA cross label on last rows
    crosses = []
    ef = out["ema_fast"]
    es = out["ema_slow"]
    for i in range(len(out)):
        if i == 0 or pd.isna(ef.iloc[i]) or pd.isna(es.iloc[i]):
            crosses.append("none")
            continue
        if ef.iloc[i] > es.iloc[i] and ef.iloc[i - 1] <= es.iloc[i - 1]:
            crosses.append("bullish")
        elif ef.iloc[i] < es.iloc[i] and ef.iloc[i - 1] >= es.iloc[i - 1]:
            crosses.append("bearish")
        else:
            crosses.append("none")
    out["ema_cross"] = crosses
    return out


def _fallback_indicators(
    out: pd.DataFrame,
    rsi_len: int,
    ema_fast: int,
    ema_slow: int,
    atr_len: int,
) -> pd.DataFrame:
    out = out.copy()
    out["rsi"] = _rsi(out["close"], rsi_len)
    macd_line, signal_line, hist = _macd(out["close"])
    out["macd"] = macd_line
    out["macd_signal"] = signal_line
    out["macd_hist"] = hist
    out["ema_fast"] = _ema(out["close"], ema_fast)
    out["ema_slow"] = _ema(out["close"], ema_slow)
    out["atr"] = _atr(out, atr_len)
    out["vwap"] = _vwap(out)
    return out


def latest_indicator_dict(df: pd.DataFrame) -> Dict[str, Any]:
    """Extract last-row indicator values as a plain dict."""
    if df is None or df.empty:
        return {}
    row = df.iloc[-1]
    def _f(key: str) -> Optional[float]:
        val = row.get(key)
        if val is None or (isinstance(val, float) and np.isnan(val)):
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    return {
        "close": _f("close"),
        "volume": _f("volume"),
        "vwap": _f("vwap"),
        "rsi": _f("rsi"),
        "macd": _f("macd"),
        "macd_signal": _f("macd_signal"),
        "macd_hist": _f("macd_hist"),
        "ema_fast": _f("ema_fast"),
        "ema_slow": _f("ema_slow"),
        "atr": _f("atr"),
        "ema_cross": str(row.get("ema_cross", "none")),
    }


def ema_value(series: pd.Series, length: int = 200) -> pd.Series:
    """EMA of arbitrary length (e.g. 200 for HTF trend)."""
    return _ema(series, length)


def ema_slope(series: pd.Series, length: int = 200, lookback: int = 5) -> Optional[float]:
    """
    Slope of EMA(length) over `lookback` bars: (ema_now - ema_then) / lookback.
    Positive → rising EMA (uptrend bias).
    """
    if series is None or len(series) < length + lookback:
        return None
    ema = _ema(series, length)
    cur = ema.iloc[-1]
    prev = ema.iloc[-1 - lookback]
    if pd.isna(cur) or pd.isna(prev):
        return None
    return float((cur - prev) / lookback)


def swing_highs_lows(
    df: pd.DataFrame,
    *,
    left: int = 3,
    right: int = 3,
) -> tuple[Optional[float], Optional[float]]:
    """
    Simple swing high / low from recent pivots.
    Returns (most recent swing high, most recent swing low).
    """
    if df is None or df.empty or len(df) < left + right + 1:
        return None, None
    highs = df["high"].astype(float)
    lows = df["low"].astype(float)
    swing_h: list[float] = []
    swing_l: list[float] = []
    for i in range(left, len(df) - right):
        window_h = highs.iloc[i - left : i + right + 1]
        window_l = lows.iloc[i - left : i + right + 1]
        if highs.iloc[i] >= window_h.max():
            swing_h.append(float(highs.iloc[i]))
        if lows.iloc[i] <= window_l.min():
            swing_l.append(float(lows.iloc[i]))
    sh = swing_h[-1] if swing_h else float(highs.tail(20).max())
    sl = swing_l[-1] if swing_l else float(lows.tail(20).min())
    return sh, sl


def support_resistance_from_swings(
    close: float,
    swing_high: Optional[float],
    swing_low: Optional[float],
) -> tuple[Optional[float], Optional[float]]:
    """Map swing high→resistance, swing low→support (simple)."""
    support = swing_low
    resistance = swing_high
    # If price already through a level, keep as nearest structural level
    if support is not None and close < support:
        support = swing_low
    if resistance is not None and close > resistance:
        resistance = swing_high
    return support, resistance


# ---------------------------------------------------------------------------
# Intelligence upgrades: ADX, Choppiness, L2 imbalance, ATR sizing helpers
# ---------------------------------------------------------------------------


def _true_range(df: pd.DataFrame) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    return pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index (Wilder). Returns series aligned to df index."""
    if df is None or df.empty or len(df) < period + 2:
        return pd.Series(dtype=float)
    out = df.copy()
    out.columns = [str(c).lower() for c in out.columns]
    high = out["high"].astype(float)
    low = out["low"].astype(float)
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(
        np.where((up > down) & (up > 0), up, 0.0), index=out.index, dtype=float
    )
    minus_dm = pd.Series(
        np.where((down > up) & (down > 0), down, 0.0), index=out.index, dtype=float
    )
    tr = _true_range(out)
    alpha = 1.0 / float(period)
    atr_w = tr.ewm(alpha=alpha, min_periods=period, adjust=False).mean()
    plus_di = 100.0 * (
        plus_dm.ewm(alpha=alpha, min_periods=period, adjust=False).mean()
        / atr_w.replace(0, np.nan)
    )
    minus_di = 100.0 * (
        minus_dm.ewm(alpha=alpha, min_periods=period, adjust=False).mean()
        / atr_w.replace(0, np.nan)
    )
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=alpha, min_periods=period, adjust=False).mean()


def choppiness_index(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Choppiness Index (CI).
    CI = 100 * log10(sum(TR, n) / (highest_high - lowest_low)) / log10(n)
    High CI (>60) = choppy/range; low CI = trending.
    """
    if df is None or df.empty or len(df) < period:
        return pd.Series(dtype=float)
    out = df.copy()
    out.columns = [str(c).lower() for c in out.columns]
    tr = _true_range(out)
    sum_tr = tr.rolling(period).sum()
    hh = out["high"].astype(float).rolling(period).max()
    ll = out["low"].astype(float).rolling(period).min()
    rng = (hh - ll).replace(0, np.nan)
    ci = 100.0 * np.log10(sum_tr / rng) / np.log10(float(period))
    return ci


def latest_adx_chop(
    df: pd.DataFrame,
    *,
    adx_period: int = 14,
    chop_period: int = 14,
) -> tuple[Optional[float], Optional[float]]:
    """Return (adx, chop) for the last bar, or (None, None) if insufficient data."""
    if df is None or df.empty:
        return None, None
    adx_s = adx(df, adx_period)
    chop_s = choppiness_index(df, chop_period)
    adx_v = None
    chop_v = None
    try:
        if len(adx_s) and not pd.isna(adx_s.iloc[-1]):
            adx_v = float(adx_s.iloc[-1])
    except Exception:
        adx_v = None
    try:
        if len(chop_s) and not pd.isna(chop_s.iloc[-1]):
            chop_v = float(chop_s.iloc[-1])
    except Exception:
        chop_v = None
    return adx_v, chop_v


def check_regime_filter(
    adx_val: Optional[float],
    chop_val: Optional[float],
    *,
    adx_min: float = 25.0,
    chop_max: float = 60.0,
    enabled: bool = True,
) -> tuple[bool, str]:
    """Block BUY when ADX < min OR Choppiness > max. Skip safely if missing when enabled."""
    if not enabled:
        return True, "regime: disabled"
    if adx_val is None:
        return False, "regime: no_adx"
    if chop_val is None:
        return False, "regime: no_chop"
    if float(adx_val) < float(adx_min):
        return False, f"regime: ADX {float(adx_val):.1f} < {float(adx_min):.0f}"
    if float(chop_val) > float(chop_max):
        return False, f"regime: chop {float(chop_val):.1f} > {float(chop_max):.0f}"
    return True, f"regime ok ADX={float(adx_val):.1f} chop={float(chop_val):.1f}"


def l2_depth_imbalance(
    bids: list,
    asks: list,
    *,
    mid: Optional[float] = None,
    band_pct: float = 0.005,
    epsilon: float = 1e-12,
) -> Optional[float]:
    """
    Bid vs ask volume within band_pct of mid.
    Ratio = bid_vol / max(ask_vol, epsilon). Returns None if book unusable.
    Each level: dict with 'price' and 'size' (or 'qty').
    """
    if not bids or not asks:
        return None
    try:
        def _px_sz(level) -> tuple[float, float]:
            if isinstance(level, dict):
                px = float(level.get("price") or level.get("px") or 0)
                sz = float(
                    level.get("size")
                    or level.get("qty")
                    or level.get("quantity")
                    or 0
                )
                return px, sz
            if isinstance(level, (list, tuple)) and len(level) >= 2:
                return float(level[0]), float(level[1])
            return 0.0, 0.0

        bid0, _ = _px_sz(bids[0])
        ask0, _ = _px_sz(asks[0])
        if mid is None or mid <= 0:
            if bid0 > 0 and ask0 > 0:
                mid = (bid0 + ask0) / 2.0
            else:
                return None
        mid = float(mid)
        band = abs(float(band_pct))
        lo = mid * (1.0 - band)
        hi = mid * (1.0 + band)
        # Inclusive band with tiny epsilon to avoid float edge rejects
        eps = max(mid * 1e-12, 1e-12)
        bid_vol = 0.0
        ask_vol = 0.0
        for lvl in bids:
            px, sz = _px_sz(lvl)
            if (lo - eps) <= px <= (mid + eps) and sz > 0:
                bid_vol += sz
        for lvl in asks:
            px, sz = _px_sz(lvl)
            if (mid - eps) <= px <= (hi + eps) and sz > 0:
                ask_vol += sz
        if bid_vol <= 0 and ask_vol <= 0:
            return None
        return bid_vol / max(ask_vol, float(epsilon))
    except Exception:
        return None


def check_l2_imbalance(
    ratio: Optional[float],
    *,
    min_ratio: float = 1.2,
    enabled: bool = True,
) -> tuple[bool, str]:
    """Require bid/ask depth imbalance > min_ratio for BUY confirmation."""
    if not enabled:
        return True, "l2_imbalance: disabled"
    if ratio is None:
        return False, "l2_imbalance: no_book"
    if float(ratio) <= float(min_ratio):
        return False, f"l2_imbalance: {float(ratio):.2f} < {float(min_ratio)}"
    return True, f"l2_imbalance ok {float(ratio):.2f} >= {float(min_ratio)}"


def check_mtf_align(
    price: float,
    ema_1h: Optional[float],
    ema_4h: Optional[float],
    *,
    enabled: bool = True,
) -> tuple[bool, str]:
    """BUY only if price strictly above both 1h and 4h EMA200."""
    if not enabled:
        return True, "mtf_align: disabled"
    if price is None or float(price) <= 0:
        return False, "mtf_align: invalid price"
    if ema_1h is None:
        return False, "mtf_align: no 1h EMA200"
    if ema_4h is None:
        return False, "mtf_align: no 4h EMA200"
    if float(price) <= float(ema_1h):
        return False, "mtf_align: price below 1h EMA200"
    if float(price) <= float(ema_4h):
        return False, "mtf_align: price below 4h EMA200"
    return True, "mtf_align ok above 1h+4h EMA200"


def atr_scaled_notional(
    atr: Optional[float],
    price: float,
    *,
    base_notional: float = 50.0,
    atr_ref_pct: float = 0.01,
    min_notional: float = 10.0,
    max_notional: float = 50.0,
    enabled: bool = True,
) -> float:
    """
    Dynamic ATR position sizing (notional USD).

    Formula: notional = base_notional * (atr_ref / atr)
      where atr_ref = price * atr_ref_pct
    Higher ATR → smaller size; lower ATR → larger size.
    Clipped to [min_notional, max_notional]. Hard cap remains max_notional ($50).
    When disabled or ATR missing → base_notional (still clipped).
    """
    max_n = float(max_notional)
    min_n = float(min_notional)
    base = min(float(base_notional), max_n)
    if not enabled or atr is None or float(atr) <= 0 or price is None or float(price) <= 0:
        return max(min_n, min(base, max_n))
    atr_ref = float(price) * float(atr_ref_pct)
    if atr_ref <= 0:
        return max(min_n, min(base, max_n))
    raw = base * (atr_ref / float(atr))
    return max(min_n, min(raw, max_n))
