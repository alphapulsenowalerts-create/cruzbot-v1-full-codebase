"""Entry proximity scoring + Telegram progress bar (long-only, <1ms, no network)."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Sequence, Union

Number = Union[int, float]

# Default LONG vs WAIT threshold for /status Target Setup (and proximity direction).
# score >= threshold → LONG; otherwise WAIT. Long-only — never SHORT.
# Mutable at runtime via set_entry_threshold() / ENTRY_THRESHOLD env + /set_threshold.
DEFAULT_ENTRY_THRESHOLD = 60.0
LONG_THRESHOLD = DEFAULT_ENTRY_THRESHOLD  # back-compat alias; kept in sync by setter
_entry_threshold: float = DEFAULT_ENTRY_THRESHOLD


def get_entry_threshold() -> float:
    """Active runtime entry proximity threshold (percent, typically 50–95)."""
    return float(_entry_threshold)


def set_entry_threshold(value: float | int) -> float:
    """Update runtime threshold used by status + strategy entry gate.

    Also mirrors into module-level LONG_THRESHOLD for older imports/tests.
    """
    global _entry_threshold, LONG_THRESHOLD
    v = float(value)
    _entry_threshold = v
    LONG_THRESHOLD = v
    return v


def make_progress_bar(percentage: Number, length: int = 10) -> str:
    """Visual bar using filled █ and empty ░ blocks (Telegram-safe)."""
    try:
        pct = float(percentage)
    except (TypeError, ValueError):
        pct = 0.0
    pct = max(0.0, min(100.0, pct))
    n = max(1, int(length))
    filled = int(round((pct / 100.0) * n))
    filled = max(0, min(n, filled))
    return ("█" * filled) + ("░" * (n - filled))


def _f(val: Any, default: Optional[float] = None) -> Optional[float]:
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _vwap_proximity_pts(close: float, vwap: Optional[float], extras: Mapping[str, Any]) -> tuple[float, str]:
    """Up to 40 pts: retest_ok or distance of price to VWAP."""
    if extras.get("retest_ok") is True:
        return 40.0, "VWAP retest"
    if extras.get("on_breakout_spike") is True:
        return 5.0, "breakout spike (await retest)"

    if vwap is None or vwap <= 0 or close <= 0:
        return 0.0, "no VWAP"

    dist = abs(close - float(vwap)) / float(vwap)
    above = close >= float(vwap)
    tag = "above VWAP" if above else "below VWAP"
    if dist <= 0.001:
        base = 35.0
    elif dist <= 0.005:
        base = 28.0
    elif dist <= 0.01:
        base = 18.0
    elif dist <= 0.02:
        base = 10.0
    else:
        base = 0.0
    # Mild long bias when already reclaiming VWAP
    if above and base > 0:
        base = min(40.0, base + 5.0)
    return base, tag


def _volume_setup_pts(
    extras: Mapping[str, Any],
    *,
    rvol_breakout_mult: float,
) -> tuple[float, str]:
    """Up to 25 pts: retest volume quality / RVOL progress."""
    bvol = _f(extras.get("breakout_volume"))
    pvol = _f(extras.get("pullback_volume"))
    if extras.get("retest_ok") is True and bvol and bvol > 0 and pvol is not None:
        if pvol < bvol * 0.5:
            return 25.0, "low-vol retest"
        if pvol < bvol * 0.75:
            return 15.0, "partial retest vol"
        return 5.0, "retest vol elevated"

    vr = _f(extras.get("breakout_rvol"), _f(extras.get("volume_ratio")))
    if vr is None:
        return 0.0, "no RVOL"
    thresh = max(1e-9, float(rvol_breakout_mult))
    if vr >= thresh:
        # Spike without confirmed retest still partial credit
        return 15.0, f"RVOL {vr:.1f}x"
    # Scale toward threshold
    return min(12.0, 12.0 * (vr / thresh)), f"RVOL {vr:.1f}x"


def _grok_pts(sentiment: Optional[Mapping[str, Any]]) -> tuple[float, str]:
    """Up to 20 pts. Missing / HOLD / SELL → 0 (never crash)."""
    if not isinstance(sentiment, Mapping):
        return 0.0, "Grok idle"
    try:
        action = str(sentiment.get("action") or "HOLD").upper()
    except Exception:
        return 0.0, "Grok idle"
    try:
        conf = float(sentiment.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    if action == "BUY":
        return 20.0 * conf, f"Grok BUY {conf:.2f}"
    if action == "SELL":
        return 0.0, "Grok SELL"
    return 0.0, "Grok HOLD"


def _cvd_pts(extras: Mapping[str, Any]) -> tuple[float, str]:
    """Up to 10 pts when CVD / phase1 keys are present."""
    has_cvd = any(
        k in extras
        for k in ("cvd_period_delta", "cvd_cumulative", "phase1_allow", "phase1_reason")
    )
    if not has_cvd:
        return 0.0, "CVD n/a"

    if extras.get("phase1_allow") is True:
        return 10.0, "phase1 allow"
    if extras.get("phase1_allow") is False:
        # Still partial if raw delta shows buy pressure
        delta = _f(extras.get("cvd_period_delta"))
        if delta is not None and delta > 0:
            return 3.0, "CVD+ but phase1 block"
        return 0.0, "phase1 block"

    delta = _f(extras.get("cvd_period_delta"))
    if delta is None:
        return 0.0, "CVD cold"
    if delta > 0:
        return 7.0, "CVD+"
    if delta < 0:
        # Absorption-style (green + neg CVD) needs candle; mild credit only
        return 3.0, "CVD−"
    return 1.0, "CVD flat"


def _liq_pts(extras: Mapping[str, Any]) -> tuple[float, str]:
    """Up to 5 pts when short-liq keys are present."""
    if "short_liq_spike" not in extras and "short_liq_notional" not in extras:
        return 0.0, "liq n/a"
    if extras.get("short_liq_spike") is True:
        return 5.0, "short-liq sweep"
    notional = _f(extras.get("short_liq_notional"), 0.0) or 0.0
    if notional > 0:
        return min(4.0, 4.0 * min(1.0, notional / 50_000.0)), "liq building"
    return 0.0, "no liq spike"


def _l2_boost_pts(extras: Mapping[str, Any]) -> tuple[float, str]:
    """+10 pts when top-book imbalance ratio > 1.5 (capped later with total score)."""
    from trading_bot.utils.decision_filters import (
        L2_PROXIMITY_BOOST_PTS,
        L2_PROXIMITY_BOOST_RATIO,
        extract_l2_ratio,
        proximity_l2_boost,
    )

    ratio = extract_l2_ratio(extras)
    return proximity_l2_boost(
        ratio,
        min_ratio=L2_PROXIMITY_BOOST_RATIO,
        boost_pts=L2_PROXIMITY_BOOST_PTS,
    )


def get_entry_proximity(
    *,
    close: Optional[Number] = None,
    vwap: Optional[Number] = None,
    extras: Optional[Mapping[str, Any]] = None,
    sentiment: Optional[Mapping[str, Any]] = None,
    rvol_breakout_mult: float = 2.0,
    symbol: str = "",
    long_threshold: Optional[float] = None,
    short_min_volume: Optional[float] = None,
    prefer_short: bool = False,
) -> Dict[str, Any]:
    """
    Weighted entry proximity score in [0, 100], with a minimal EMA20/50 short trigger.

    Components (max): VWAP/retest 40 + volume 25 + Grok BUY 20 + CVD 10 + liq 5 + L2 boost 10.
    Missing Grok / CVD / liq → 0 pts for that bucket (never raises).
    Direction is LONG, SHORT, or WAIT. SHORT wins only on the explicit EMA20/EMA50
    bearish-cross + price-below-EMA50 + volume-surge trigger.
    """
    thresh = float(long_threshold) if long_threshold is not None else get_entry_threshold()
    ex: Mapping[str, Any] = extras if isinstance(extras, Mapping) else {}
    close_f = _f(close, 0.0) or 0.0
    vwap_f = _f(vwap)

    vwap_pts, vwap_tag = _vwap_proximity_pts(close_f, vwap_f, ex)
    vol_pts, vol_tag = _volume_setup_pts(ex, rvol_breakout_mult=rvol_breakout_mult)
    grok_pts, grok_tag = _grok_pts(sentiment)
    cvd_pts, cvd_tag = _cvd_pts(ex)
    liq_pts, liq_tag = _liq_pts(ex)
    l2_pts, l2_tag = _l2_boost_pts(ex)

    long_score = float(vwap_pts + vol_pts + grok_pts + cvd_pts + liq_pts + l2_pts)
    long_score = max(0.0, min(100.0, long_score))
    ema50 = _f(ex.get("ema50_1m"))
    short_cross = str(ex.get("ema20_50_cross") or "").lower() == "bearish"
    short_volume = _f(ex.get("volume_ratio"), 0.0) or 0.0
    short_floor = max(float(short_min_volume), 0.0) if short_min_volume is not None else max(float(rvol_breakout_mult), 1.5)
    bearish_surge = any(
        bool(ex.get(key))
        for key in ("bearish_volume_surge", "bearish_rvol_surge", "sell_volume_surge")
    )
    short_signal = bool(
        short_cross and ema50 and close_f > 0 and close_f < ema50
        and (bearish_surge or short_volume >= short_floor)
    )
    short_score = 100.0 if short_signal else 0.0
    if short_score >= thresh and (prefer_short or short_score > long_score):
        direction = "SHORT"
        score = short_score
    elif long_score >= thresh:
        direction = "LONG"
        score = long_score
    else:
        direction = "WAIT"
        score = long_score

    setup_bits = [t for t in (vwap_tag, vol_tag) if t]
    setup = " · ".join(setup_bits[:2]) if setup_bits else "scanning"
    sym = (symbol or "").strip().upper()
    if direction in ("LONG", "SHORT") and sym:
        target = f"{sym} {direction} ({setup})"
    elif sym:
        target = f"{sym} WAIT ({setup})"
    else:
        target = f"WAIT ({setup})"

    return {
        "direction": direction,
        "score": round(score, 1),
        "long_score": round(long_score, 1),
        "short_score": round(short_score, 1),
        "symbol": sym or None,
        "price": close_f if close_f > 0 else None,
        "target_setup": target,
        "components": {
            "vwap_retest": round(vwap_pts, 1),
            "volume": round(vol_pts, 1),
            "grok": round(grok_pts, 1),
            "cvd": round(cvd_pts, 1),
            "liq": round(liq_pts, 1),
            "l2_boost": round(l2_pts, 1),
        },
        "tags": {
            "vwap": vwap_tag,
            "volume": vol_tag,
            "grok": grok_tag,
            "cvd": cvd_tag,
            "liq": liq_tag,
            "l2": l2_tag,
        },
        "bar": make_progress_bar(score),
    }


def get_entry_proximity_from_obs(
    obs: Any,
    *,
    sentiment: Optional[Mapping[str, Any]] = None,
    rvol_breakout_mult: float = 2.0,
    long_threshold: Optional[float] = None,
    short_min_volume: Optional[float] = None,
    prefer_short: bool = False,
) -> Dict[str, Any]:
    """Score from AgentObservation / duck-typed indicator snapshot."""
    ind = getattr(obs, "indicators", None)
    if ind is None:
        return get_entry_proximity(
            sentiment=sentiment,
            rvol_breakout_mult=rvol_breakout_mult,
            long_threshold=long_threshold,
            short_min_volume=short_min_volume,
            prefer_short=prefer_short,
            symbol=str(getattr(obs, "symbol", "") or ""),
        )
    extras = getattr(ind, "extras", None) or {}
    return get_entry_proximity(
        close=getattr(ind, "close", None),
        vwap=getattr(ind, "vwap", None),
        extras=extras if isinstance(extras, Mapping) else {},
        sentiment=sentiment,
        rvol_breakout_mult=rvol_breakout_mult,
        symbol=str(getattr(obs, "symbol", None) or getattr(ind, "symbol", "") or ""),
        long_threshold=long_threshold,
        short_min_volume=short_min_volume,
        prefer_short=prefer_short,
    )


def best_entry_proximity(
    observations: Sequence[Any],
    *,
    sentiment: Optional[Mapping[str, Any]] = None,
    rvol_breakout_mult: float = 2.0,
    long_threshold: Optional[float] = None,
    short_min_volume: Optional[float] = None,
    prefer_short: bool = False,
) -> Dict[str, Any]:
    """Pick highest-score symbol from a sequence of observations."""
    best: Optional[Dict[str, Any]] = None
    for obs in observations or ():
        prox = get_entry_proximity_from_obs(
            obs,
            sentiment=sentiment,
            rvol_breakout_mult=rvol_breakout_mult,
            long_threshold=long_threshold,
            short_min_volume=short_min_volume,
            prefer_short=prefer_short,
        )
        if best is None or float(prox["score"]) > float(best["score"]):
            best = prox
    if best is None:
        return get_entry_proximity(
            sentiment=sentiment,
            rvol_breakout_mult=rvol_breakout_mult,
            long_threshold=long_threshold,
            short_min_volume=short_min_volume,
            prefer_short=prefer_short,
        )
    return best
