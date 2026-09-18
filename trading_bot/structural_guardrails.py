"""Hard structural entry guardrails for CruzBot paper/live path.

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
) -> float:
    """Stricter of MIN_TP_PCT and fee-to-target floor (mult * round-trip maker)."""
    fee_floor = float(fee_to_target_mult) * 2.0 * float(maker_fee_rate)
    return max(float(min_tp_pct), fee_floor)


def check_fee_to_target(
    entry: float,
    take_profit: Optional[float],
    *,
    min_tp_pct: float,
    maker_fee_rate: float,
    fee_to_target_mult: float,
) -> Tuple[bool, str]:
    """Return (ok, reason). Reject when TP distance < required floor."""
    if entry is None or entry <= 0:
        return False, "fee_to_target: invalid entry"
    if take_profit is None:
        return False, "fee_to_target: missing take_profit"
    tp_pct = (float(take_profit) - float(entry)) / float(entry)
    required = required_min_tp_pct(min_tp_pct, maker_fee_rate, fee_to_target_mult)
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
