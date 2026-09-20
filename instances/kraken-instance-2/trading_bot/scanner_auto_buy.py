"""Scanner → paper execution bridge.

When entry proximity clears the active threshold, optionally auto-submit a
paper BUY (same fill/registration path as /test_trade). Aggressive profile
bypasses TOD + volume-spike / RVOL prefilters; medium/low keep those gates.
Hard auto-fire ceilings: $1000/trade, $3000 exposure, 3 concurrent.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

# Hard ceilings for scanner auto-fire (even if .env temporarily higher)
AUTO_PAPER_MAX_NOTIONAL_USD = 1000.0
AUTO_PAPER_MAX_EXPOSURE_USD = 3000.0
AUTO_PAPER_MAX_CONCURRENT = 3

# Aggressive uses 1.0x (effectively bypass volume spike); others keep engine mins
AGGRESSIVE_RVOL_FLOOR = 1.0


def is_aggressive_profile(profile: Any) -> bool:
    return str(profile or "").strip().lower() in {"aggressive", "agg", "a"}


def clamp_auto_notional(requested: float) -> float:
    """Clamp auto-fire size to the hard $1000 ceiling (and >0)."""
    try:
        v = float(requested)
    except (TypeError, ValueError):
        v = AUTO_PAPER_MAX_NOTIONAL_USD
    if v <= 0 or v != v:
        v = AUTO_PAPER_MAX_NOTIONAL_USD
    return min(v, AUTO_PAPER_MAX_NOTIONAL_USD)


def format_scanner_loop_log(
    symbol: str,
    proximity: float,
    threshold: float,
    status: str,
) -> str:
    """Exact scanner-loop log line style."""
    prox_i = int(round(float(proximity)))
    thresh_i = int(round(float(threshold)))
    return (
        f"[SCANNER LOOP] Symbol: {symbol} | Proximity: {prox_i}% | "
        f"Target: {thresh_i}% | Status: {status}"
    )


def evaluate_scanner_auto_buy(
    *,
    proximity: float,
    threshold: float,
    profile: str,
    paper: bool,
    paused: bool,
    already_open: bool,
    open_position_count: int,
    max_concurrent: Optional[int] = None,
    open_exposure_usd: float = 0.0,
    max_exposure_usd: Optional[float] = None,
    trade_notional: Optional[float] = None,
    tod_blocked: bool = False,
    disable_tod_gate: bool = False,
    rvol: Optional[float] = None,
    rvol_min: float = 2.0,
    bypass_volume_spike: bool = False,
) -> Tuple[bool, str]:
    """Decide whether scanner should auto-fire a paper buy.

    Returns ``(should_fire, status)`` where status is ``AUTO-FIRED`` or
    ``SKIPPED: <reason>`` (reason tokens: below_threshold, tod_gate, rvol,
    max_positions, exposure_cap, trade_cap, already_open, paused, live_blocked,
    etc.).
    """
    prox = float(proximity or 0.0)
    thresh = float(threshold or 0.0)
    aggressive = is_aggressive_profile(profile)

    if not paper:
        return False, "SKIPPED: live_blocked"

    if paused:
        return False, "SKIPPED: paused"

    if already_open:
        return False, "SKIPPED: already_open"

    conc_cap = int(
        max_concurrent
        if max_concurrent is not None
        else (AUTO_PAPER_MAX_CONCURRENT if aggressive else AUTO_PAPER_MAX_CONCURRENT)
    )
    if aggressive:
        conc_cap = min(conc_cap, AUTO_PAPER_MAX_CONCURRENT)
    if int(open_position_count) >= conc_cap:
        return False, "SKIPPED: max_positions"

    exp_cap = float(
        max_exposure_usd
        if max_exposure_usd is not None
        else AUTO_PAPER_MAX_EXPOSURE_USD
    )
    exp_cap = min(exp_cap, AUTO_PAPER_MAX_EXPOSURE_USD)
    notion = clamp_auto_notional(
        trade_notional if trade_notional is not None else AUTO_PAPER_MAX_NOTIONAL_USD
    )
    if notion <= 0:
        return False, "SKIPPED: trade_cap"
    if float(open_exposure_usd or 0.0) + notion > exp_cap + 1e-9:
        return False, "SKIPPED: exposure_cap"

    if prox < thresh:
        return False, "SKIPPED: below_threshold"

    # TOD: aggressive (or explicit disable) bypasses; others honor blackout
    if tod_blocked and not (aggressive or disable_tod_gate):
        return False, "SKIPPED: tod_gate"

    # Elite Day-Trader: always enforce RVOL >= rvol_min (even on AGGRESSIVE).
    # bypass_volume_spike only skips when rvol_min <= 0 (legacy opt-out).
    min_rv = float(rvol_min or 0.0)
    if min_rv > 0:
        if rvol is None:
            return False, "SKIPPED: rvol"
        try:
            rv = float(rvol)
        except (TypeError, ValueError):
            return False, "SKIPPED: rvol"
        if rv != rv or rv < min_rv:
            return False, "SKIPPED: rvol"
    elif aggressive or bypass_volume_spike:
        return True, "AUTO-FIRED"

    return True, "AUTO-FIRED"


def proximity_score_from_result(prox: Any) -> float:
    """Extract 0–100 score from get_entry_proximity / best_entry_proximity result."""
    if prox is None:
        return 0.0
    if isinstance(prox, Mapping):
        for key in ("score", "proximity", "entry_proximity", "pct"):
            if key in prox and prox[key] is not None:
                try:
                    return float(prox[key])
                except (TypeError, ValueError):
                    continue
    try:
        return float(prox)
    except (TypeError, ValueError):
        return 0.0
