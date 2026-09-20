"""Fast decision helpers: HTF EMA200, ATR brackets, ADX threshold raise, L2 top-N."""

from __future__ import annotations

import math
from typing import Any, Mapping, Optional, Tuple

# Defaults: SL floor = max(SL_MIN_PCT * entry, ATR_SL_MULT * ATR14)
ATR_BRACKET_SL_MULT = 1.5
ATR_BRACKET_TP_MULT = 2.5
SL_MIN_PCT = 0.012
ADX_THRESHOLD_FLOOR = 20.0
ADX_THRESHOLD_RAISE = 15.0
L2_PROXIMITY_BOOST_RATIO = 1.5
L2_PROXIMITY_BOOST_PTS = 10.0


def atr_bracket_levels(
    entry: float,
    atr: float,
    *,
    sl_mult: float = ATR_BRACKET_SL_MULT,
    tp_mult: float = ATR_BRACKET_TP_MULT,
    sl_min_pct: float = 0.01,
    tp_min_pct: float = 0.02,
    sl_max_pct: float = 0.012,
) -> Tuple[float, float]:
    """Return (stop_loss, take_profit) for a long.

    SL: floor sl_min_pct (1%), ceiling sl_max_pct (1.2%) so ATR cannot blow out risk.
    TP: >= tp_min_pct (2%) and >= 2× SL distance when that still clears the floor.
    """
    e = float(entry)
    a = float(atr)
    if e <= 0 or a <= 0 or a != a:
        raise ValueError("atr_bracket_levels requires positive entry and atr")
    sl_dist = max(e * float(sl_min_pct), float(sl_mult) * a)
    sl_cap = e * float(sl_max_pct)
    if sl_cap > 0:
        sl_dist = min(sl_dist, sl_cap)
    tp_dist = max(
        e * float(tp_min_pct),
        float(tp_mult) * a,
        2.0 * sl_dist,
    )
    return e - sl_dist, e + tp_dist


def format_post_stop_cooldown_skip(symbol: str, remaining_seconds: float) -> str:
    """Exact skip log when a signal would fire but post-stop cooldown blocks."""
    rem = max(0.0, float(remaining_seconds or 0.0))
    mins = int(math.ceil(rem / 60.0)) if rem > 0 else 0
    if mins < 1 and rem > 0:
        mins = 1
    return (
        f"[SKIP] {symbol}: Signal active but cooling down for another {mins} minutes."
    )


def check_htf_ema200_long(
    close_1h: Optional[float],
    ema200_1h: Optional[float],
    *,
    enabled: bool = True,
) -> Tuple[bool, str]:
    """LONG allowed only when 1h close >= 1h EMA200 (when data present)."""
    if not enabled:
        return True, "htf_ema200: disabled"
    if close_1h is None or ema200_1h is None:
        # Missing HTF → do not invent a block (caller may still skip via mtf gate)
        return True, "htf_ema200: no_data"
    try:
        c = float(close_1h)
        e = float(ema200_1h)
    except (TypeError, ValueError):
        return True, "htf_ema200: no_data"
    if c != c or e != e or e <= 0:
        return True, "htf_ema200: no_data"
    if c < e:
        return False, "SKIPPED: htf_ema200"
    return True, "htf_ema200 ok"


def effective_entry_threshold(
    base_threshold: float,
    adx: Optional[float],
    *,
    adx_floor: float = ADX_THRESHOLD_FLOOR,
    raise_by: float = ADX_THRESHOLD_RAISE,
) -> Tuple[float, bool]:
    """If ADX < floor, raise effective ENTRY_THRESHOLD by raise_by. Returns (thresh, raised)."""
    base = float(base_threshold)
    if adx is None:
        return base, False
    try:
        adx_f = float(adx)
    except (TypeError, ValueError):
        return base, False
    if adx_f != adx_f:
        return base, False
    if adx_f < float(adx_floor):
        return base + float(raise_by), True
    return base, False


def l2_top_n_imbalance(
    bids: list,
    asks: list,
    *,
    n: int = 5,
    epsilon: float = 1e-12,
) -> Optional[float]:
    """Top-N bid volume / top-N ask volume. None if book unusable."""
    if not bids or not asks:
        return None
    try:
        def _sz(level) -> float:
            if isinstance(level, dict):
                return float(
                    level.get("size")
                    or level.get("qty")
                    or level.get("quantity")
                    or 0
                )
            if isinstance(level, (list, tuple)) and len(level) >= 2:
                return float(level[1])
            return 0.0

        bid_vol = sum(_sz(lvl) for lvl in list(bids)[: max(1, int(n))])
        ask_vol = sum(_sz(lvl) for lvl in list(asks)[: max(1, int(n))])
        if bid_vol <= 0 and ask_vol <= 0:
            return None
        return bid_vol / max(ask_vol, float(epsilon))
    except Exception:
        return None


def proximity_l2_boost(
    ratio: Optional[float],
    *,
    min_ratio: float = L2_PROXIMITY_BOOST_RATIO,
    boost_pts: float = L2_PROXIMITY_BOOST_PTS,
) -> Tuple[float, str]:
    """+boost_pts when top-book imbalance ratio > min_ratio."""
    if ratio is None:
        return 0.0, "l2 boost n/a"
    try:
        r = float(ratio)
    except (TypeError, ValueError):
        return 0.0, "l2 boost n/a"
    if r != r:
        return 0.0, "l2 boost n/a"
    if r > float(min_ratio):
        return float(boost_pts), f"l2 boost {r:.2f}"
    return 0.0, f"l2 flat {r:.2f}"


def extract_adx(extras: Optional[Mapping[str, Any]]) -> Optional[float]:
    if not isinstance(extras, Mapping):
        return None
    for key in ("adx", "ADX"):
        if key in extras and extras[key] is not None:
            try:
                return float(extras[key])
            except (TypeError, ValueError):
                continue
    return None


def extract_l2_ratio(extras: Optional[Mapping[str, Any]]) -> Optional[float]:
    if not isinstance(extras, Mapping):
        return None
    for key in ("l2_top5_ratio", "l2_imbalance_ratio", "l2_imbalance"):
        if key in extras and extras[key] is not None:
            try:
                return float(extras[key])
            except (TypeError, ValueError):
                continue
    return None


# --- Elite Day-Trader ATR brackets + fee-lock trail ---
ELITE_ATR_SL_MULT = 1.5
ELITE_ATR_TP_MULT = 2.5
# Fee-aware trail: arm only after full RT fee buffer; SL floor covers RT fees.
# Default ~0.85% (= ~0.75% RT + 0.10% pad). Override via TRAIL_FEE_BUFFER_PCT.
DEFAULT_TRAIL_FEE_BUFFER_PCT = 0.0125
ELITE_FEE_LOCK_ARM_PCT = DEFAULT_TRAIL_FEE_BUFFER_PCT  # was 0.006
ELITE_FEE_LOCK_LONG_MULT = 1.0 + DEFAULT_TRAIL_FEE_BUFFER_PCT  # was 1.003
ELITE_FEE_LOCK_SHORT_MULT = 1.0 - DEFAULT_TRAIL_FEE_BUFFER_PCT  # was 0.997


def trail_fee_buffer_pct(settings: Any = None) -> float:
    """Round-trip fee buffer for trail arm + SL floor: entry+exit fees + 0.10%."""
    if settings is not None:
        explicit = getattr(settings, "trail_fee_buffer_pct", None)
        try:
            if explicit is not None and float(explicit) > 0:
                return float(explicit)
        except (TypeError, ValueError):
            pass
        try:
            maker = float(getattr(settings, "maker_fee_rate", 0.0) or 0.0)
            taker = float(getattr(settings, "taker_fee_rate", 0.0) or 0.0)
            if maker > 0 or taker > 0:
                if bool(getattr(settings, "post_only", True)):
                    rt = float(taker or maker) + float(maker or taker)
                else:
                    rt = 2.0 * float(taker or maker)
                # Clamp to sane band so inflated config fees don't freeze trails forever
                buf = rt + 0.001  # +0.10% pad
                return max(0.0085, min(buf, 0.012))
        except (TypeError, ValueError):
            pass
    return float(DEFAULT_TRAIL_FEE_BUFFER_PCT)


def elite_atr_bracket_levels(
    entry: float,
    atr: float,
    *,
    short: bool = False,
    sl_mult: float = ELITE_ATR_SL_MULT,
    tp_mult: float = ELITE_ATR_TP_MULT,
    sl_min_pct: float = 0.01,
    tp_min_pct: float = 0.02,
    sl_max_pct: float = 0.012,
) -> Tuple[float, float]:
    """Elite brackets with SL/TP pct floors and SL ceiling (default 1.2%)."""
    e = float(entry)
    a = float(atr)
    if e <= 0 or a <= 0 or a != a:
        raise ValueError("elite_atr_bracket_levels requires positive entry and atr")
    sl_dist = max(e * float(sl_min_pct), float(sl_mult) * a)
    sl_cap = e * float(sl_max_pct)
    if sl_cap > 0:
        sl_dist = min(sl_dist, sl_cap)
    tp_dist = max(e * float(tp_min_pct), float(tp_mult) * a, 2.0 * sl_dist)
    if short:
        return e + sl_dist, e - tp_dist
    return e - sl_dist, e + tp_dist


def fee_lock_sl(
    entry: float,
    *,
    short: bool = False,
    fee_buffer_pct: float = DEFAULT_TRAIL_FEE_BUFFER_PCT,
) -> float:
    """SL floor = entry * (1 ± FEE_BUFFER) so a trail hit covers RT fees (~≥$0 net)."""
    e = float(entry)
    buf = float(fee_buffer_pct)
    if short:
        return e * (1.0 - buf)
    return e * (1.0 + buf)


def maybe_fee_lock_sl(
    entry: float,
    mark: float,
    current_sl: Optional[float],
    *,
    short: bool = False,
    arm_pct: Optional[float] = None,
    fee_buffer_pct: Optional[float] = None,
    settings: Any = None,
    progress_pct: Optional[float] = None,
    progress_arm: float = 60.0,
    min_floor_pct: float = 0.0125,
    peak_arm_pct: float = 0.012,
) -> Optional[float]:
    """Arm fee-lock after UPL >= FEE_BUFFER, progress >= 60%, or peak UPL >= +1.20%.

    Floor = entry*(1+max(FEE_BUFFER, min_floor_pct)) → Tier-1 +1.25% net-safe lock.
    """
    e = float(entry)
    if e <= 0:
        return None
    buf = float(
        fee_buffer_pct
        if fee_buffer_pct is not None
        else trail_fee_buffer_pct(settings)
    )
    arm = float(arm_pct) if arm_pct is not None else buf
    m = float(mark)
    upl = (e - m) / e if short else (m - e) / e
    prog_ok = (
        progress_pct is not None and float(progress_pct) >= float(progress_arm)
    )
    peak_ok = upl >= float(peak_arm_pct)
    floor_gate = upl >= float(min_floor_pct)
    if upl < arm and not prog_ok and not peak_ok and not floor_gate:
        return None
    # Progress ≥60% or peak ≥+1.20% or UPL ≥ floor: lock at ≥+1.25%
    if prog_ok or peak_ok or floor_gate:
        buf = max(buf, float(min_floor_pct))
    lock = fee_lock_sl(e, short=short, fee_buffer_pct=buf)
    if current_sl is None:
        return lock
    cur = float(current_sl)
    # Never allow SL below fee floor (long) / above fee floor (short)
    if short:
        floor = lock
        tightened = min(cur, floor)
        return tightened if tightened < cur - 1e-15 else (floor if cur > floor else None)
    floor = lock
    if cur < floor:
        return floor  # raise to fee floor
    return None  # already at/above floor; further trail handled elsewhere


def check_elite_rvol_spread(
    rvol: Optional[float],
    bid: Optional[float],
    ask: Optional[float],
    *,
    rvol_min: float = 1.8,
    max_spread_pct: float = 0.0025,
) -> Tuple[bool, str]:
    """Entry only if RVOL >= rvol_min AND spread <= max_spread_pct."""
    if rvol is None:
        return False, "SKIPPED: rvol_missing"
    try:
        rv = float(rvol)
    except (TypeError, ValueError):
        return False, "SKIPPED: rvol_missing"
    if rv != rv or rv < float(rvol_min):
        return False, f"SKIPPED: rvol ({rv:.2f}<{float(rvol_min):.2f})"
    if bid is None or ask is None:
        return False, "SKIPPED: no_quote_spread"
    try:
        bid_f = float(bid)
        ask_f = float(ask)
    except (TypeError, ValueError):
        return False, "SKIPPED: no_quote_spread"
    if bid_f <= 0 or ask_f <= 0 or ask_f < bid_f:
        return False, "SKIPPED: no_quote_spread"
    mid = (bid_f + ask_f) / 2.0
    if mid <= 0:
        return False, "SKIPPED: no_quote_spread"
    spread = (ask_f - bid_f) / mid
    if spread > float(max_spread_pct) + 1e-15:
        return (
            False,
            f"SKIPPED: spread ({spread*100:.4f}%>{float(max_spread_pct)*100:.4f}%)",
        )
    return True, f"rvol_spread ok rvol={rv:.2f} spread={spread*100:.4f}%"
