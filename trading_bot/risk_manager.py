"""Risk management: allowlist, absolute caps, fractional crypto sizing, DD breaker."""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Optional

from trading_bot.config import HARD_SYMBOL_ALLOWLIST, Settings
from trading_bot.structural_guardrails import check_fee_to_target
from trading_bot.utils.indicators import atr_scaled_notional
from trading_bot.models import (
    AccountState,
    Action,
    Decision,
    Position,
    OrderRequest,
    OrderSide,
    OrderType,
    RiskVerdict,
)

logger = logging.getLogger(__name__)


def floor_qty(qty: float, precision: int) -> float:
    """Floor quantity to exchange-style decimal precision (no whole-coin int())."""
    if qty <= 0:
        return 0.0
    prec = max(0, int(precision))
    quant = Decimal("1").scaleb(-prec)
    return float(Decimal(str(qty)).quantize(quant, rounding=ROUND_DOWN))


class RiskManager:
    """
    Enforces Al's locked production risk:
    - Hard symbol allowlist only
    - Max $50 notional per trade, $200 total live exposure
    - Stops: volume_sweet_spot uses structural SL + R-multiple TP (no ATR trail);
      legacy vwap_scalp uses ATR SL/TP/trail
    - 1–2% equity risk per trade (ceiling 2%)
    - 3% daily drawdown circuit breaker
    - Fractional crypto sizing by notional / base_size precision
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._day: Optional[date] = None
        self._day_start_equity: Optional[float] = None
        self._circuit_breaker = False
        self._circuit_reason = ""

    @property
    def _circuit_reason_public(self) -> str:
        return self._circuit_reason

    def reset_day_if_needed(self, account: AccountState, now: Optional[datetime] = None) -> None:
        now = now or datetime.now(timezone.utc)
        d = now.date()
        if self._day != d:
            self._day = d
            self._day_start_equity = account.equity
            self._circuit_breaker = False
            self._circuit_reason = ""
            logger.info("Risk day reset: start_equity=%.2f", account.equity)

    @property
    def circuit_breaker_active(self) -> bool:
        return self._circuit_breaker

    def update_drawdown(self, account: AccountState) -> bool:
        """Return True if circuit breaker just tripped or is active."""
        self.reset_day_if_needed(account)
        start = self._day_start_equity or account.equity
        if start <= 0:
            return self._circuit_breaker
        dd = (start - account.equity) / start
        limit = self.settings.daily_drawdown_limit_pct
        if dd >= limit:
            if not self._circuit_breaker:
                self._circuit_reason = (
                    f"Daily drawdown {dd:.2%} >= limit {limit:.2%}; trading locked for the day"
                )
                logger.error("CIRCUIT BREAKER: %s", self._circuit_reason)
            self._circuit_breaker = True
        return self._circuit_breaker

    def suggest_stops(
        self,
        action: Action,
        entry: float,
        atr: Optional[float],
        explicit_sl: Optional[float] = None,
        explicit_tp: Optional[float] = None,
    ) -> tuple[Optional[float], Optional[float], Optional[float]]:
        """Return (stop_loss, take_profit, trailing_distance).

        volume_sweet_spot: prefer explicit structural SL/TP; trailing_distance always None.
        legacy vwap_scalp: ATR multiples + trail.
        """
        if entry <= 0:
            return explicit_sl, explicit_tp, None

        sweet = bool(getattr(self.settings, "is_sweet_spot", False))
        if sweet:
            # Never attach ATR trail in sweet-spot mode
            if action == Action.BUY:
                sl = explicit_sl
                tp = explicit_tp
            elif action == Action.SELL:
                sl = explicit_sl
                tp = explicit_tp
            else:
                return None, None, None
            return (
                round(sl, 8) if sl is not None else None,
                round(tp, 8) if tp is not None else None,
                None,
            )

        atr_val = atr if atr and atr > 0 else entry * 0.01
        sl_mult = self.settings.stop_loss_atr_mult
        tp_mult = self.settings.take_profit_atr_mult
        trail = atr_val * self.settings.trailing_stop_atr_mult

        if action == Action.BUY:
            sl = explicit_sl if explicit_sl is not None else entry - atr_val * sl_mult
            tp = explicit_tp if explicit_tp is not None else entry + atr_val * tp_mult
        elif action == Action.SELL:
            sl = explicit_sl if explicit_sl is not None else entry + atr_val * sl_mult
            tp = explicit_tp if explicit_tp is not None else entry - atr_val * tp_mult
        else:
            return None, None, None

        return round(sl, 4), round(tp, 4), round(trail, 4)

    def size_position(
        self,
        equity: float,
        entry: float,
        stop_loss: float,
        *,
        max_risk_pct: Optional[float] = None,
        remaining_exposure_usd: Optional[float] = None,
        atr: Optional[float] = None,
    ) -> tuple[float, float, float]:
        """
        Size qty from stop distance so $risk <= max_risk_pct * equity,
        then apply absolute $50/trade and remaining $200 exposure caps.
        Optional ATR scaling shrinks/grows notional within [min, $50] hard cap.
        Uses fractional crypto qty (floored to qty_precision) — never int() whole-coins.
        """
        risk_pct = max_risk_pct if max_risk_pct is not None else self.settings.max_risk_per_trade_pct
        risk_pct = min(risk_pct, self.settings.max_risk_per_trade_pct_ceiling, 0.02)
        risk_pct = max(risk_pct, 0.01) if risk_pct > 0 else 0.01

        stop_dist = abs(entry - stop_loss)
        if stop_dist <= 0 or entry <= 0 or equity <= 0:
            return 0.0, 0.0, 0.0

        risk_budget = equity * risk_pct
        qty = risk_budget / stop_dist

        # Cap by max position % of equity
        max_notional_pct = equity * self.settings.max_position_pct
        # Absolute per-trade notional cap ($50 locked)
        max_notional_abs = float(self.settings.max_notional_per_trade_usd)
        max_notional = min(max_notional_pct, max_notional_abs)

        # Dynamic ATR sizing: further cap target notional (never raises above $50 hard cap)
        if bool(getattr(self.settings, "atr_sizing_enabled", False)):
            min_n = float(getattr(self.settings, "min_notional_usd", 10.0) or 10.0)
            atr_target = atr_scaled_notional(
                atr,
                entry,
                base_notional=max_notional_abs,
                atr_ref_pct=float(getattr(self.settings, "atr_ref_pct", 0.01) or 0.01),
                min_notional=min_n,
                max_notional=max_notional_abs,
                enabled=True,
            )
            max_notional = min(max_notional, atr_target)

        if remaining_exposure_usd is not None:
            max_notional = min(max_notional, max(0.0, remaining_exposure_usd))

        if max_notional <= 0:
            return 0.0, 0.0, 0.0

        max_qty_by_notional = max_notional / entry
        qty = min(qty, max_qty_by_notional)

        precision = int(getattr(self.settings, "qty_precision", 8) or 8)
        qty = floor_qty(qty, precision)

        if qty <= 0:
            return 0.0, risk_budget, risk_pct

        # Re-check notional after flooring; shrink one tick if we slightly exceeded cap
        notional = qty * entry
        min_notional = float(getattr(self.settings, "min_notional_usd", 10.0) or 10.0)
        if notional + 1e-9 < min_notional:
            return 0.0, risk_budget, risk_pct
        if notional > max_notional + 1e-9:
            qty = floor_qty(max_notional / entry, precision)
            if qty <= 0:
                return 0.0, risk_budget, risk_pct

        risk_amount = qty * stop_dist
        actual_pct = risk_amount / equity if equity else 0.0
        return qty, risk_amount, actual_pct

    def evaluate(
        self,
        decision: Decision,
        account: AccountState,
        *,
        entry_price: float,
        atr: Optional[float] = None,
        open_exposure_usd: float = 0.0,
        open_position: Optional[Position] = None,
        open_position_count: int = 0,
    ) -> RiskVerdict:
        """Approve/reject and size a decision before execution.

        SELL exits size to the full open long (not a fresh short).
        BUY is rejected when already long unless allow_pyramiding is True.
        """
        self.reset_day_if_needed(account)
        cb_active = self.update_drawdown(account)

        if decision.action == Action.HOLD:
            return RiskVerdict(approved=False, reason="HOLD — no order")

        # Circuit breaker blocks NEW entries only — exits still go through
        if cb_active and decision.action == Action.BUY:
            return RiskVerdict(
                approved=False,
                reason=self._circuit_reason or "circuit breaker active",
                circuit_breaker_active=True,
            )

        symbol = Settings.normalize_symbol(decision.symbol)
        if symbol not in HARD_SYMBOL_ALLOWLIST:
            return RiskVerdict(
                approved=False,
                reason=f"symbol {symbol} not on hard allowlist — rejected",
            )

        if entry_price <= 0:
            return RiskVerdict(approved=False, reason="invalid entry price")

        pos_qty = float(getattr(open_position, "qty", 0) or 0) if open_position else 0.0
        already_long = pos_qty > 1e-12

        # --- EXIT PATH: SELL closes existing long at full remaining qty ---
        if decision.action == Action.SELL:
            if not already_long:
                return RiskVerdict(
                    approved=False,
                    reason="flat — no open long to sell (suppress short)",
                )
            precision = int(getattr(self.settings, "qty_precision", 8) or 8)
            qty = floor_qty(pos_qty, precision)
            # Partial exit (TP1): decision.quantity caps sell size
            if decision.quantity is not None and float(decision.quantity) > 0:
                qty = floor_qty(min(qty, float(decision.quantity)), precision)
            if qty <= 0:
                return RiskVerdict(approved=False, reason="open position qty too small to sell")
            # Prefer bracket levels already on the position; fall back to decision/ATR
            sl = getattr(open_position, "stop_loss", None) if open_position else None
            tp = getattr(open_position, "take_profit", None) if open_position else None
            trail = getattr(open_position, "trail_distance", None) if open_position else None
            sweet = bool(getattr(self.settings, "is_sweet_spot", False))
            if sweet:
                trail = None  # ATR trail dead in sweet-spot path
            if sl is None or tp is None:
                sug_sl, sug_tp, sug_trail = self.suggest_stops(
                    Action.BUY,  # long brackets from original entry perspective
                    float(getattr(open_position, "avg_entry_price", entry_price) or entry_price),
                    atr,
                    decision.stop_loss,
                    decision.take_profit,
                )
                sl = sl if sl is not None else sug_sl
                tp = tp if tp is not None else sug_tp
                if not sweet:
                    trail = trail if trail is not None else sug_trail
                else:
                    trail = None
            return RiskVerdict(
                approved=True,
                reason=(
                    "approved exit — partial qty"
                    if decision.quantity is not None
                    else "approved exit — full open qty"
                ),
                sized_qty=qty,
                risk_amount=0.0,
                risk_pct=0.0,
                stop_loss=sl,
                take_profit=tp,
                trailing_stop_distance=trail,
            )

        # --- ENTRY PATH: BUY ---
        allow_pyramid = bool(getattr(self.settings, "allow_pyramiding", False))
        if already_long and not allow_pyramid:
            return RiskVerdict(
                approved=False,
                reason=f"already long {symbol} qty={pos_qty:.8f} — no pyramid",
            )

        max_concurrent = int(getattr(self.settings, "max_concurrent_positions", 2) or 2)
        # Count current opens; a new BUY on a flat symbol increases count by 1
        effective_count = int(open_position_count)
        if already_long and allow_pyramid:
            pass  # pyramiding same symbol does not add a new concurrent slot
        elif not already_long and effective_count >= max_concurrent:
            return RiskVerdict(
                approved=False,
                reason=(
                    f"max concurrent positions {max_concurrent} reached "
                    f"(open={effective_count})"
                ),
            )

        max_total = float(self.settings.max_total_exposure_usd)
        remaining = max_total - max(0.0, open_exposure_usd)
        if remaining <= 0:
            return RiskVerdict(
                approved=False,
                reason=f"max total exposure ${max_total:.0f} reached (open ${open_exposure_usd:.2f})",
            )

        sl, tp, trail = self.suggest_stops(
            decision.action,
            entry_price,
            atr,
            decision.stop_loss,
            decision.take_profit,
        )
        if bool(getattr(self.settings, "is_sweet_spot", False)):
            trail = None
        if sl is None:
            return RiskVerdict(approved=False, reason="could not determine stop loss")

        # Fee-to-target: TP must clear max(MIN_TP_PCT, FEE_TO_TARGET_MULT * 2 * maker)
        ok_fee, fee_reason = check_fee_to_target(
            entry_price,
            tp,
            min_tp_pct=float(getattr(self.settings, "min_tp_pct", 0.025) or 0.025),
            maker_fee_rate=float(getattr(self.settings, "maker_fee_rate", 0.005) or 0.005),
            fee_to_target_mult=float(getattr(self.settings, "fee_to_target_mult", 2.5) or 2.5),
        )
        if not ok_fee:
            logger.info("SKIP BUY %s | %s", symbol, fee_reason)
            return RiskVerdict(
                approved=False,
                reason=fee_reason,
                stop_loss=sl,
                take_profit=tp,
                trailing_stop_distance=trail,
            )

        qty, risk_amount, risk_pct = self.size_position(
            account.equity,
            entry_price,
            sl,
            remaining_exposure_usd=remaining,
            atr=atr,
        )

        ceiling = self.settings.max_risk_per_trade_pct_ceiling
        if qty > 0 and risk_pct > ceiling + 1e-9:
            return RiskVerdict(
                approved=False,
                reason=f"risk {risk_pct:.2%} exceeds ceiling {ceiling:.2%}",
                sized_qty=qty,
                risk_amount=risk_amount,
                risk_pct=risk_pct,
                stop_loss=sl,
                take_profit=tp,
                trailing_stop_distance=trail,
            )

        if qty <= 0:
            return RiskVerdict(
                approved=False,
                reason=(
                    f"cannot size fractional position within "
                    f"${self.settings.max_notional_per_trade_usd:.0f}/trade and "
                    f"{ceiling:.0%} risk caps"
                ),
                sized_qty=0.0,
                risk_amount=risk_amount,
                risk_pct=risk_pct,
                stop_loss=sl,
                take_profit=tp,
                trailing_stop_distance=trail,
            )

        notional = qty * entry_price
        max_trade = float(self.settings.max_notional_per_trade_usd)
        if notional > max_trade + 1e-6:
            precision = int(getattr(self.settings, "qty_precision", 8) or 8)
            qty = floor_qty(max_trade / entry_price, precision)
            if qty <= 0:
                return RiskVerdict(
                    approved=False,
                    reason=f"cannot size within ${max_trade:.0f} notional cap",
                    stop_loss=sl,
                    take_profit=tp,
                )
            notional = qty * entry_price
            risk_amount = qty * abs(entry_price - sl)
            risk_pct = risk_amount / account.equity if account.equity else 0.0

        if notional > account.buying_power + 1e-6:
            precision = int(getattr(self.settings, "qty_precision", 8) or 8)
            affordable = floor_qty(account.buying_power / entry_price, precision)
            if affordable <= 0:
                return RiskVerdict(
                    approved=False,
                    reason="insufficient buying power",
                    stop_loss=sl,
                    take_profit=tp,
                )
            qty = affordable
            risk_amount = qty * abs(entry_price - sl)
            risk_pct = risk_amount / account.equity if account.equity else 0.0
            if risk_pct > ceiling + 1e-9:
                return RiskVerdict(
                    approved=False,
                    reason=f"affordable size still exceeds risk ceiling ({risk_pct:.2%})",
                    sized_qty=qty,
                    risk_amount=risk_amount,
                    risk_pct=risk_pct,
                    stop_loss=sl,
                    take_profit=tp,
                )

        return RiskVerdict(
            approved=True,
            reason="approved",
            sized_qty=qty,
            risk_amount=risk_amount,
            risk_pct=risk_pct,
            stop_loss=sl,
            take_profit=tp,
            trailing_stop_distance=trail,
        )

    def to_order_request(
        self,
        decision: Decision,
        verdict: RiskVerdict,
        *,
        limit_price: float,
        paper: bool = True,
    ) -> Optional[OrderRequest]:
        if not verdict.approved or verdict.sized_qty <= 0:
            return None
        symbol = Settings.normalize_symbol(decision.symbol)
        if symbol not in HARD_SYMBOL_ALLOWLIST:
            return None
        side = OrderSide.BUY if decision.action == Action.BUY else OrderSide.SELL
        post_only = bool(getattr(self.settings, "post_only", True))
        return OrderRequest(
            symbol=symbol,
            side=side,
            qty=verdict.sized_qty,
            order_type=OrderType.LIMIT,
            limit_price=limit_price,
            stop_loss=verdict.stop_loss,
            take_profit=verdict.take_profit,
            paper=paper,
            post_only=post_only,
        )
