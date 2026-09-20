"""Hard structural entry guardrails for Apex Signals Now paper/live path.

1. Dedup lock — max 1 open position per coin + no duplicate BUY within N seconds
2. Maker limit only — POST_ONLY LIMIT; market orders rejected
3. Spread filter — skip BUY when (ask-bid)/mid > MAX_SPREAD_PCT
4. Fee-to-target — min TP distance >= max(MIN_TP_PCT, FEE_TO_TARGET_MULT * 2 * maker)
"""

from __future__ import annotations

from typing import Optional, Tuple


def required_min_tp_pct(
    min_tp_pct: float,
    maker_fee_rate: float,
    fee_to_target_mult: float,
    *,
    net_buffer_pct: float = 0.01,
    rt_fee_pct: float | None = None,
) -> float:
    """Stricter of MIN_TP_PCT, mult×RT fee, and (RT fee + net buffer).

    Default intent: TP >= max(2%, 3×~0.5% RT, RT+1% net buffer).
    """
    rt = float(rt_fee_pct) if rt_fee_pct is not None else 2.0 * float(maker_fee_rate)
    fee_floor = float(fee_to_target_mult) * rt
    net_floor = rt + float(net_buffer_pct)
    return max(float(min_tp_pct), fee_floor, net_floor)


def check_fee_to_target(
    entry: float,
    take_profit: Optional[float],
    *,
    min_tp_pct: float,
    maker_fee_rate: float,
    fee_to_target_mult: float,
    net_buffer_pct: float = 0.01,
    rt_fee_pct: Optional[float] = None,
) -> Tuple[bool, str]:
    """Return (ok, reason). Reject when TP cannot clear fees + net buffer."""
    if entry is None or entry <= 0:
        return False, "fee_to_target: invalid entry"
    if take_profit is None:
        return False, "fee_to_target: missing take_profit"
    tp_pct = (float(take_profit) - float(entry)) / float(entry)
    required = required_min_tp_pct(
        min_tp_pct,
        maker_fee_rate,
        fee_to_target_mult,
        net_buffer_pct=net_buffer_pct,
        rt_fee_pct=rt_fee_pct,
    )
    if tp_pct + 1e-12 < required:
        return (
            False,
            f"fee_to_target: TP {tp_pct:.2%} < required {required:.2%}",
        )
    return True, f"fee_to_target ok TP={tp_pct:.2%} (>= {required:.2%})"


def check_spread(
    bid: Optional[float],
    ask: Optional[float],
    *,
    max_spread_pct: float,
) -> Tuple[bool, str]:
    """Return (ok, reason). Prefer skip with no_quote_spread when quotes missing."""
    if bid is None or ask is None:
        return False, "no_quote_spread"
    try:
        bid_f = float(bid)
        ask_f = float(ask)
    except (TypeError, ValueError):
        return False, "no_quote_spread"
    if bid_f <= 0 or ask_f <= 0 or ask_f < bid_f:
        return False, "no_quote_spread"
    mid = (bid_f + ask_f) / 2.0
    if mid <= 0:
        return False, "no_quote_spread"
    spread = (ask_f - bid_f) / mid
    if spread > float(max_spread_pct) + 1e-15:
        return (
            False,
            f"spread_filter: {(spread * 100):.4f}% > max {(float(max_spread_pct) * 100):.4f}%",
        )
    return True, f"spread ok {(spread * 100):.4f}%"


def check_buy_dedupe(
    seconds_since_last: Optional[float],
    *,
    window_seconds: float,
) -> Tuple[bool, str]:
    """Return (ok, reason). Block if last BUY attempt/fill was within window."""
    if seconds_since_last is None:
        return True, "buy_dedupe ok (no prior buy)"
    window = float(window_seconds)
    if window <= 0:
        return True, "buy_dedupe disabled"
    if float(seconds_since_last) < window:
        remaining = window - float(seconds_since_last)
        return (
            False,
            f"buy_dedupe: last buy {seconds_since_last:.0f}s ago "
            f"(window={window:.0f}s, {remaining:.0f}s left)",
        )
    return True, "buy_dedupe ok"


def guardrails_startup_message(
    *,
    buy_dedupe_seconds: float,
    post_only: bool,
    max_spread_pct: float,
    min_tp_pct: float,
    maker_fee_rate: float,
    fee_to_target_mult: float,
    allow_pyramiding: bool,
) -> str:
    req = required_min_tp_pct(min_tp_pct, maker_fee_rate, fee_to_target_mult)
    return (
        "STRUCTURAL GUARDRAILS ARMED | "
        f"1) dedupe lock: 1 pos/coin pyramiding={'ON' if allow_pyramiding else 'OFF'} "
        f"+ buy_dedupe={buy_dedupe_seconds:.0f}s | "
        f"2) maker limit only: post_only={post_only} (market_orders_disabled) | "
        f"3) spread filter: max={(max_spread_pct * 100):.4f}% | "
        f"4) fee-to-target: required_min_tp={req:.2%} "
        f"(min_tp={min_tp_pct:.2%}, mult={fee_to_target_mult}× RT maker "
        f"{(2 * maker_fee_rate):.2%})"
    )


# Assumed round-trip taker cost on a ~$500 spot order (~1.0% total).
BEAR_CHOP_RT_TAKER_FEE_PCT = 0.005  # ~0.5% round-trip taker assumption


def bear_chop_min_tp_pct(
    fee_to_target_mult: float = 3.0,
    *,
    rt_taker_fee_pct: float = BEAR_CHOP_RT_TAKER_FEE_PCT,
    min_tp_pct: float = 0.02,
    net_buffer_pct: float = 0.01,
) -> float:
    """Min TP in BEAR_CHOP: max(min_tp, mult×RT, RT+net_buffer)."""
    rt = float(rt_taker_fee_pct)
    return max(
        float(min_tp_pct),
        float(fee_to_target_mult) * rt,
        rt + float(net_buffer_pct),
    )


def check_bear_chop_taker_fee_tp(
    entry: float,
    take_profit: Optional[float],
    *,
    fee_to_target_mult: float = 2.5,
    rt_taker_fee_pct: float = BEAR_CHOP_RT_TAKER_FEE_PCT,
    short: bool = False,
) -> Tuple[bool, str]:
    """Reject BEAR_CHOP LONGs whose TP cannot clear RT taker fees with buffer.

    Example: ~1.0% RT taker × 2.5 mult → require >= +2.5% TP move.
    """
    if entry is None or float(entry) <= 0:
        return False, "bear_fee_tp: invalid entry"
    if take_profit is None:
        return False, "bear_fee_tp: missing take_profit"
    entry_f = float(entry)
    tp_f = float(take_profit)
    if short:
        tp_pct = (entry_f - tp_f) / entry_f
    else:
        tp_pct = (tp_f - entry_f) / entry_f
    required = bear_chop_min_tp_pct(
        fee_to_target_mult, rt_taker_fee_pct=rt_taker_fee_pct
    )
    if tp_pct + 1e-12 < required:
        return (
            False,
            f"bear_fee_tp: TP {tp_pct:.2%} < required {required:.2%} "
            f"(RT taker~{float(rt_taker_fee_pct):.2%}×{float(fee_to_target_mult):g})",
        )
    return True, f"bear_fee_tp ok TP={tp_pct:.2%} (>= {required:.2%})"

