"""Structural liquidity pool + stop-hunt sweep fade (PAPER intel v3).

Identify HTF swing highs/lows (liquidity pools). Long-only: fade sweeps *below*
swing lows when L2 bid absorption confirms (stop-hunt low then reclaim).

Skip/entry reasons:
  sweep_fade: no_sweep
  sweep_fade: no_absorption
  sweep_fade: entry
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from trading_bot.utils.indicators import swing_highs_lows

logger = logging.getLogger(__name__)

REASON_ENTRY = "sweep_fade: entry"
REASON_NO_SWEEP = "sweep_fade: no_sweep"
REASON_NO_ABSORPTION = "sweep_fade: no_absorption"
REASON_DISABLED = "sweep_fade: disabled"


@dataclass
class SweepFadeSignal:
    symbol: str
    entry: bool
    confidence: float
    swing_low: Optional[float]
    sweep_low: Optional[float]
    absorption_ratio: float
    reason: str


def find_liquidity_pools(
    df: pd.DataFrame,
    *,
    left: int = 3,
    right: int = 3,
    lookback: int = 50,
) -> Tuple[Optional[float], Optional[float]]:
    """Return (swing_high, swing_low) liquidity pools from recent bars."""
    if df is None or df.empty:
        return None, None
    window = df.tail(max(lookback, left + right + 5))
    return swing_highs_lows(window, left=left, right=right)


def detect_sellside_sweep(
    bars: pd.DataFrame,
    swing_low: float,
    *,
    reclaim: bool = True,
) -> Tuple[bool, Optional[float]]:
    """True when a recent low wicked below swing_low then reclaimed (close back above).

    Long-only stop-hunt fade: sell-side liquidity sweep into support.
    """
    if bars is None or bars.empty or swing_low is None or swing_low <= 0:
        return False, None
    if len(bars) < 3:
        return False, None
    # Look at last few bars for a sweep wick
    tail = bars.tail(5)
    swept = False
    sweep_px: Optional[float] = None
    for _, row in tail.iterrows():
        low = float(row["low"])
        close = float(row["close"])
        if low < float(swing_low) * 0.9995:  # pierce pool
            swept = True
            sweep_px = low if sweep_px is None else min(sweep_px, low)
            if reclaim and close >= float(swing_low):
                return True, sweep_px
    if swept and not reclaim:
        return True, sweep_px
    # If swept earlier and latest close reclaimed
    if swept and reclaim:
        last_close = float(tail.iloc[-1]["close"])
        if last_close >= float(swing_low):
            return True, sweep_px
    return False, sweep_px


def l2_bid_absorption(
    bids: Sequence[Dict[str, Any]],
    asks: Sequence[Dict[str, Any]],
    *,
    mid: Optional[float] = None,
    band_pct: float = 0.003,
    min_ratio: float = 1.5,
) -> Tuple[bool, float]:
    """Bid absorption: bid volume near mid >> ask volume (buyers defending)."""
    if mid is None or mid <= 0:
        if bids and asks:
            try:
                mid = (float(bids[0]["price"]) + float(asks[0]["price"])) / 2.0
            except (KeyError, TypeError, ValueError, IndexError):
                return False, 0.0
        else:
            return False, 0.0
    lo = float(mid) * (1.0 - float(band_pct))
    hi = float(mid) * (1.0 + float(band_pct))
    bid_vol = 0.0
    ask_vol = 0.0
    for lvl in bids or []:
        try:
            px = float(lvl.get("price") or 0)
            sz = float(lvl.get("size") or 0)
        except (TypeError, ValueError):
            continue
        if lo <= px <= float(mid) and sz > 0:
            bid_vol += sz
    for lvl in asks or []:
        try:
            px = float(lvl.get("price") or 0)
            sz = float(lvl.get("size") or 0)
        except (TypeError, ValueError):
            continue
        if float(mid) <= px <= hi and sz > 0:
            ask_vol += sz
    if ask_vol <= 1e-12:
        ratio = 99.0 if bid_vol > 0 else 0.0
    else:
        ratio = bid_vol / ask_vol
    return ratio >= float(min_ratio), float(ratio)


def evaluate_sweep_fade(
    symbol: str,
    bars: pd.DataFrame,
    *,
    bids: Optional[Sequence[Dict[str, Any]]] = None,
    asks: Optional[Sequence[Dict[str, Any]]] = None,
    mid: Optional[float] = None,
    enabled: bool = True,
    lookback: int = 50,
    absorption_ratio: float = 1.5,
    confidence_base: float = 70.0,
) -> SweepFadeSignal:
    """Long-only sweep-fade decision for one symbol."""
    if not enabled:
        return SweepFadeSignal(
            symbol=symbol,
            entry=False,
            confidence=0.0,
            swing_low=None,
            sweep_low=None,
            absorption_ratio=0.0,
            reason=REASON_DISABLED,
        )
    try:
        _sh, sl = find_liquidity_pools(bars, lookback=lookback)
        if sl is None:
            return SweepFadeSignal(
                symbol=symbol,
                entry=False,
                confidence=0.0,
                swing_low=None,
                sweep_low=None,
                absorption_ratio=0.0,
                reason=REASON_NO_SWEEP,
            )
        swept, sweep_px = detect_sellside_sweep(bars, sl, reclaim=True)
        if not swept:
            return SweepFadeSignal(
                symbol=symbol,
                entry=False,
                confidence=0.0,
                swing_low=sl,
                sweep_low=sweep_px,
                absorption_ratio=0.0,
                reason=REASON_NO_SWEEP,
            )
        ok_abs, ratio = l2_bid_absorption(
            bids or [],
            asks or [],
            mid=mid,
            min_ratio=absorption_ratio,
        )
        if not ok_abs:
            return SweepFadeSignal(
                symbol=symbol,
                entry=False,
                confidence=0.0,
                swing_low=sl,
                sweep_low=sweep_px,
                absorption_ratio=ratio,
                reason=REASON_NO_ABSORPTION,
            )
        conf = min(95.0, confidence_base + (ratio - absorption_ratio) * 5.0)
        logger.info(
            "SWEEP_FADE entry %s swing_low=%.4f sweep=%.4f abs_ratio=%.2f conf=%.1f",
            symbol,
            sl,
            sweep_px or 0.0,
            ratio,
            conf,
        )
        return SweepFadeSignal(
            symbol=symbol,
            entry=True,
            confidence=conf,
            swing_low=sl,
            sweep_low=sweep_px,
            absorption_ratio=ratio,
            reason=REASON_ENTRY,
        )
    except Exception as exc:
        logger.warning("SWEEP_FADE evaluate failed (degrade): %s", exc)
        return SweepFadeSignal(
            symbol=symbol,
            entry=False,
            confidence=0.0,
            swing_low=None,
            sweep_low=None,
            absorption_ratio=0.0,
            reason=REASON_NO_SWEEP,
        )


class SweepFadeEngine:
    """Thin wrapper for config-bound sweep-fade evaluation."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        lookback: int = 50,
        absorption_ratio: float = 1.5,
        confidence_base: float = 70.0,
    ) -> None:
        self.enabled = bool(enabled)
        self.lookback = int(lookback or 50)
        self.absorption_ratio = float(absorption_ratio or 1.5)
        self.confidence_base = float(confidence_base or 70.0)

    def evaluate(
        self,
        symbol: str,
        bars: pd.DataFrame,
        *,
        bids: Optional[Sequence[Dict[str, Any]]] = None,
        asks: Optional[Sequence[Dict[str, Any]]] = None,
        mid: Optional[float] = None,
    ) -> SweepFadeSignal:
        return evaluate_sweep_fade(
            symbol,
            bars,
            bids=bids,
            asks=asks,
            mid=mid,
            enabled=self.enabled,
            lookback=self.lookback,
            absorption_ratio=self.absorption_ratio,
            confidence_base=self.confidence_base,
        )
