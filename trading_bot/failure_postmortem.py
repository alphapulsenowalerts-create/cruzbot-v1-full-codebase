"""AI/heuristic post-mortem on stop-loss exits + temporary BUY blacklist."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Signature tags
TAG_LIQUIDITY_GRAB = "Liquidity Grab"
TAG_LOW_VOL_FAKEOUT = "Low-Volume Fakeout"
TAG_HTF_SLOPE_FADE = "HTF Slope Fade"
TAG_WIDE_SPREAD_CHASE = "Wide Spread Chase"
TAG_UNKNOWN = "Unknown Failure"


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def summarize_candles(bars: Sequence[Any], max_n: int = 30) -> Dict[str, Any]:
    """Compact OHLC summary for ~30m pre-exit window."""
    if not bars:
        return {"n": 0}
    tail = list(bars)[-max_n:]

    def g(b: Any, k: str) -> float:
        if isinstance(b, dict):
            return _f(b.get(k))
        return _f(getattr(b, k, None))

    opens = [g(b, "open") for b in tail]
    highs = [g(b, "high") for b in tail]
    lows = [g(b, "low") for b in tail]
    closes = [g(b, "close") for b in tail]
    vols = [g(b, "volume") for b in tail]
    ret = 0.0
    if opens and opens[0] > 0 and closes:
        ret = (closes[-1] - opens[0]) / opens[0]
    avg_vol = sum(vols) / max(1, len(vols))
    return {
        "n": len(tail),
        "open0": opens[0] if opens else None,
        "close_last": closes[-1] if closes else None,
        "high_max": max(highs) if highs else None,
        "low_min": min(lows) if lows else None,
        "ret_pct": ret,
        "avg_volume": avg_vol,
        "last_volume": vols[-1] if vols else None,
        "red_bars": sum(1 for o, c in zip(opens, closes) if c < o),
    }


def build_failure_snapshot(
    *,
    symbol: str,
    indicators: Any = None,
    htf: Any = None,
    quote: Any = None,
    recent_bars: Sequence[Any] = (),
    extras: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Serialize ~30 min pre-exit context for trade_failures."""
    extras = dict(extras or {})
    if indicators is not None and getattr(indicators, "extras", None):
        extras = {**dict(indicators.extras), **extras}
    l2 = extras.get("l2_imbalance_ratio", extras.get("l2_imbalance"))
    spread = None
    if quote is not None:
        bid = _f(getattr(quote, "bid", None), 0.0)
        ask = _f(getattr(quote, "ask", None), 0.0)
        mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else 0.0
        if mid > 0:
            spread = (ask - bid) / mid
    elif extras.get("spread_pct") is not None:
        spread = _f(extras.get("spread_pct"))

    rsi = None
    if indicators is not None:
        rsi = getattr(indicators, "rsi", None)
    if rsi is None:
        rsi = extras.get("rsi")

    ema_slope = None
    if htf is not None:
        ema_slope = getattr(htf, "ema_200_slope", None)
    if ema_slope is None:
        ema_slope = extras.get("ema_200_slope")

    candle = summarize_candles(recent_bars, max_n=30)
    return {
        "symbol": (symbol or "").upper(),
        "l2_depth_ratio": _f(l2, default=float("nan")) if l2 is not None else None,
        "spread_pct": spread,
        "htf_ema_slope": _f(ema_slope) if ema_slope is not None else None,
        "rsi": _f(rsi) if rsi is not None else None,
        "candle_summary": candle,
        "volume_ratio": extras.get("volume_ratio") or extras.get("breakout_rvol"),
        "adx": extras.get("adx"),
        "chop": extras.get("chop", extras.get("choppiness")),
    }


def classify_failure(snapshot: Dict[str, Any]) -> str:
    """Rule-based 'AI' cluster tags for stop-loss post-mortem."""
    l2 = snapshot.get("l2_depth_ratio")
    spread = snapshot.get("spread_pct")
    slope = snapshot.get("htf_ema_slope")
    candle = snapshot.get("candle_summary") or {}
    vol_ratio = snapshot.get("volume_ratio")
    avg_vol = _f(candle.get("avg_volume"))
    last_vol = _f(candle.get("last_volume"))
    ret = _f(candle.get("ret_pct"))

    # Liquidity Grab: thin/ask-heavy book then stop-out (L2 << 1) with sharp wick dump
    if l2 is not None and not (isinstance(l2, float) and l2 != l2):  # not NaN
        if float(l2) < 0.85 and ret < -0.002:
            return TAG_LIQUIDITY_GRAB

    # Low-Volume Fakeout: breakout-ish then die on low volume
    if vol_ratio is not None and float(vol_ratio) < 1.2:
        return TAG_LOW_VOL_FAKEOUT
    if avg_vol > 0 and last_vol > 0 and last_vol < 0.4 * avg_vol and ret < 0:
        return TAG_LOW_VOL_FAKEOUT

    # HTF slope against longs
    if slope is not None and float(slope) < 0:
        return TAG_HTF_SLOPE_FADE

    # Wide spread chase
    if spread is not None and float(spread) > 0.0015:
        return TAG_WIDE_SPREAD_CHASE

    return TAG_UNKNOWN


def snapshot_matches_signature(
    live: Dict[str, Any],
    signature_tag: str,
    *,
    stored: Optional[Dict[str, Any]] = None,
) -> bool:
    """Heuristic match of live conditions to a tagged failure signature."""
    tag = (signature_tag or "").strip()
    l2 = live.get("l2_depth_ratio")
    spread = live.get("spread_pct")
    slope = live.get("htf_ema_slope")
    candle = live.get("candle_summary") or {}
    vol_ratio = live.get("volume_ratio")
    avg_vol = _f(candle.get("avg_volume"))
    last_vol = _f(candle.get("last_volume"))

    if tag == TAG_LIQUIDITY_GRAB:
        return l2 is not None and float(l2) < 0.9
    if tag == TAG_LOW_VOL_FAKEOUT:
        if vol_ratio is not None and float(vol_ratio) < 1.3:
            return True
        return avg_vol > 0 and last_vol > 0 and last_vol < 0.45 * avg_vol
    if tag == TAG_HTF_SLOPE_FADE:
        return slope is not None and float(slope) < 0
    if tag == TAG_WIDE_SPREAD_CHASE:
        return spread is not None and float(spread) > 0.0012
    if tag == TAG_UNKNOWN and stored:
        # weak match: same symbol recent unknown — rely on store TTL only
        return False
    return False


def check_failure_blacklist(
    live_snapshot: Dict[str, Any],
    recent_failures: Sequence[Dict[str, Any]],
) -> Tuple[bool, str]:
    """
    If live conditions match a recent tagged signature, return (True, reason).
    recent_failures rows: {signature_tag, symbol, snapshot?, blocked_until?}
    Caller filters by lookback / block duration.
    """
    for row in recent_failures:
        tag = row.get("signature_tag") or row.get("tag") or ""
        if not tag or tag == TAG_UNKNOWN:
            continue
        # Prefer symbol-local, but allow cross-symbol for Liquidity Grab / Low-Vol
        row_sym = (row.get("symbol") or "").upper()
        live_sym = (live_snapshot.get("symbol") or "").upper()
        if row_sym and live_sym and row_sym != live_sym:
            if tag not in (TAG_LIQUIDITY_GRAB, TAG_LOW_VOL_FAKEOUT):
                continue
        stored_snap = row.get("snapshot") if isinstance(row.get("snapshot"), dict) else None
        if snapshot_matches_signature(live_snapshot, tag, stored=stored_snap):
            reason = f"failure_blacklist: {tag}"
            return True, reason
    return False, ""
