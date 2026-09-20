"""Async Discord / Telegram notifier — fills, exits, and requested P&L only."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_CT = ZoneInfo("America/Chicago")


def _fmt_qty(qty: float) -> str:
    """Position / fill size — max 2 decimals, comma-separated, never sci-notation.

    Examples: 10395.01039501 → 10,395.01 ; 166667000 → 166,667,000
    """
    q = float(qty)
    if q != q:  # NaN
        return "0"
    # Cap at 2 decimal places, then strip trailing zeros for whole numbers.
    s = f"{q:,.2f}"
    if s.endswith(".00"):
        return s[:-3]
    if s.endswith("0") and "." in s:
        return s[:-1]
    return s


def _fmt_money(amount: float) -> str:
    """Bankroll / notional / PnL — always 2 decimal places."""
    return f"{float(amount):.2f}"


def _fmt_price(amount: float) -> str:
    """Market prices — never scientific notation.

    >= $1.00 → 2 decimals; $0.01–$1 → 4 decimals;
    < $0.01 → fixed decimals (up to 10), strip trailing zeros.
    """
    x = float(amount)
    if x != x:
        return "0.00"
    a = abs(x)
    if a >= 1.0:
        return f"{x:,.2f}"
    if a >= 0.01:
        return f"{x:.4f}"
    # Avoid e-notation; keep enough fractional digits for micro-priced alts.
    s = f"{x:.12f}".rstrip("0").rstrip(".")
    if s.startswith("."):
        s = "0" + s
    elif s.startswith("-."):
        s = "-0" + s[1:]
    if "." in s:
        whole, frac = s.split(".", 1)
        frac = frac[:10].rstrip("0")
        s = whole + (("." + frac) if frac else "")
    return s if s not in ("", "-", "-0") else f"{x:.10f}".rstrip("0").rstrip(".")




def _fmt_pnl_signed(amount: float) -> str:
    a = float(amount)
    if a < 0:
        return f"-${abs(a):.2f}"
    return f"+${a:.2f}"


def _fmt_date_ct(when: Optional[datetime] = None) -> str:
    if when is None:
        dt = datetime.now(_CT)
    else:
        dt = when
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_CT)
        else:
            dt = dt.astimezone(_CT)
    return dt.strftime("%m/%d/%Y")


def _fmt_time_ct(when: Optional[datetime] = None) -> str:
    if when is None:
        dt = datetime.now(_CT)
    else:
        dt = when
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_CT)
        else:
            dt = dt.astimezone(_CT)
    return dt.strftime("%I:%M %p").lstrip("0")


def _map_exit_reason(reason: str) -> str:
    raw = (reason or "").strip()
    r = raw.lower()
    if raw in ("TIME_EXIT_MAKER_BE",) or "time_exit_maker_be" in r:
        return "TIME EXIT | Limit Maker Posted"
    if raw in ("TIME_EXIT_MAKER_TIMEOUT_SL",) or "time_exit_maker_timeout" in r:
        return "TIME EXIT | Fee Cushion → SL"
    if "time_exit_maker_arm" in r or "fee cushion" in r:
        return "TIME EXIT | Fee Cushion Active"
    if r in ("tp1", "tp2", "atr take-profit") or (r.startswith("tp") and "time" not in r):
        return "TP Hit"
    if r in ("sl", "atr stop", "trail", "stop") or r.startswith("sl"):
        return "SL Hit"
    if "time" in r:
        return "Time Exit"
    if reason:
        return reason
    return "Exit"


class Notifier:
    """
    Telegram/Discord alerts STRICTLY for:
      - BUY fills
      - SELL / exit fills
      - Requested performance reports

    All system noise (kill switch, circuit, macro, status) → console/log only.
    """

    def __init__(
        self,
        *,
        discord_webhook_url: str = "",
        telegram_bot_token: str = "",
        telegram_chat_id: str = "",
        timeout: float = 8.0,
        enabled: bool = True,
        quiet: bool = True,
    ) -> None:
        self.discord_webhook_url = (discord_webhook_url or "").strip()
        self.telegram_bot_token = (telegram_bot_token or "").strip()
        self.telegram_chat_id = (telegram_chat_id or "").strip()
        self.timeout = timeout
        self.enabled = enabled
        # quiet always treated as True for system noise (Al addendum)
        self.quiet = True if quiet else True

    @property
    def configured(self) -> bool:
        return bool(
            self.discord_webhook_url
            or (self.telegram_bot_token and self.telegram_chat_id)
        )

    async def send(
        self,
        message: str,
        *,
        title: str = "",
        extra: Optional[Dict[str, Any]] = None,
        high_priority: bool = False,
    ) -> None:
        """Broadcast a plain-text alert. No-op if nothing configured.

        high_priority=True forces Telegram notification sound (disable_notification=False).
        """
        if not self.enabled or not self.configured:
            logger.debug("Notifier no-op (unset webhooks): %s %s", title, message[:80])
            return

        text = f"{title}\n{message}" if title else message
        if extra:
            bits = ", ".join(f"{k}={v}" for k, v in extra.items())
            text = f"{text}\n({bits})"

        try:
            import httpx
        except ImportError:
            logger.warning("httpx missing — notifier skipped")
            return

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            if self.discord_webhook_url:
                try:
                    payload = {"content": text[:1900]}
                    resp = await client.post(self.discord_webhook_url, json=payload)
                    if resp.status_code >= 400:
                        logger.warning("Discord webhook status %s", resp.status_code)
                except Exception as exc:
                    logger.warning("Discord notify failed: %s", exc)

            if self.telegram_bot_token and self.telegram_chat_id:
                try:
                    url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
                    payload = {
                        "chat_id": self.telegram_chat_id,
                        "text": text[:3500],
                        "disable_web_page_preview": True,
                        # High-priority mode alerts must ping; never silently mute.
                        "disable_notification": False if high_priority else False,
                    }
                    resp = await client.post(url, json=payload)
                    if resp.status_code >= 400:
                        logger.warning("Telegram status %s", resp.status_code)
                except Exception as exc:
                    logger.warning("Telegram notify failed: %s", exc)

    async def mode_change_alert(self, message: str) -> None:
        """High-priority Telegram/Discord ping when PAPER/LIVE mode actually flips."""
        await self.send(message, high_priority=True)

    async def trade_entry(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: Optional[float] = None,
        *,
        paper: bool = True,
        fee: Optional[float] = None,
        cash_after: Optional[float] = None,
        equity_after: Optional[float] = None,
        live_cash: Optional[float] = None,
        live_equity: Optional[float] = None,
        paper_cash: Optional[float] = None,
        paper_equity: Optional[float] = None,
        paper_cap: float = 1000.0,
        when: Optional[datetime] = None,
        fee_rate: float = 0.005,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        entry_reason: Optional[str] = None,
        est_round_trip_fee: Optional[float] = None,
        post_only: bool = True,
        **_ignored: Any,
    ) -> None:
        """BUY fill — exact Al template."""
        q = float(qty or 0)
        px = float(price) if price is not None else None
        if px is None or q <= 0:
            logger.debug("Skip trade_entry missing price/qty %s", symbol)
            return
        cost = q * px
        if cost < 1.0:
            logger.debug("Skip dust trade_entry alert %s cost=%.6f", symbol, cost)
            return

        tp_s = f"${_fmt_price(take_profit)}" if take_profit is not None else "n/a"
        sl_s = f"${_fmt_price(stop_loss)}" if stop_loss is not None else "n/a"
        nl = chr(10)
        # Exact structure (no title prefix — emoji line is the header)
        body = (
            f"🔵 BUY FILLED | {symbol}{nl}"
            f"------------------------------------{nl}"
            f"• Entry Price: ${_fmt_price(px)}{nl}"
            f"• Total Spent: ${_fmt_money(cost)} ({_fmt_qty(q)} coins){nl}"
            f"• Target TP: {tp_s}{nl}"
            f"• Stop Loss: {sl_s}{nl}"
            f"• Order Type: Limit Maker"
        )
        await self.send(body)

    async def trade_exit(
        self,
        symbol: str,
        side: str,
        qty: float,
        *,
        pnl: Optional[float] = None,
        reason: str = "",
        price: Optional[float] = None,
        paper: bool = True,
        fee: Optional[float] = None,
        entry_price: Optional[float] = None,
        buy_cost_with_fees: Optional[float] = None,
        cash_after: Optional[float] = None,
        equity_after: Optional[float] = None,
        live_cash: Optional[float] = None,
        live_equity: Optional[float] = None,
        paper_cash: Optional[float] = None,
        paper_equity: Optional[float] = None,
        paper_cap: float = 1000.0,
        when: Optional[datetime] = None,
        fee_rate: float = 0.005,
        post_only: bool = True,
        gross_pnl_pct: Optional[float] = None,
        est_rt_fees: Optional[float] = None,
        order_type_label: Optional[str] = None,
        **_ignored: Any,
    ) -> None:
        """SELL / exit fill — exact Al template."""
        q = float(qty or 0)
        px = float(price) if price is not None else None
        if px is None or q <= 0:
            logger.debug("Skip trade_exit missing price/qty %s", symbol)
            return
        proceeds = q * px
        if proceeds < 1.0:
            logger.debug("Skip dust trade_exit alert %s", symbol)
            return

        exit_fee = (
            float(fee)
            if fee is not None
            else proceeds * float(fee_rate or 0.005)
        )
        if buy_cost_with_fees is not None:
            bought = float(buy_cost_with_fees)
        elif entry_price is not None:
            entry_notional = float(entry_price) * q
            bought = entry_notional + entry_notional * float(fee_rate or 0.005)
        else:
            bought = None

        if pnl is None and bought is not None:
            pnl = (proceeds - exit_fee) - bought
        pnl_v = float(pnl or 0.0)

        if pnl_v >= 0:
            header = f"🟢 PROFIT TAKE | {symbol}"
        else:
            header = f"🔴 STOPPED OUT | {symbol}"

        # Prefer paper book cash/equity for "Current Cash/Equity"
        cur_cash = paper_cash if paper_cash is not None else cash_after
        cur_eq = paper_equity if paper_equity is not None else equity_after
        cash_s = f"${_fmt_money(cur_cash)}" if cur_cash is not None else "n/a"
        eq_s = f"${_fmt_money(cur_eq)}" if cur_eq is not None else "n/a"
        entry_s = (
            f"${_fmt_price(entry_price)}" if entry_price is not None else "n/a"
        )
        why = _map_exit_reason(reason)
        nl = chr(10)

        # Fee-cushion / maker time-exit telemetry
        extra = ""
        try:
            if gross_pnl_pct is not None:
                extra += f"{nl}• Gross Gain: {float(gross_pnl_pct) * 100.0:+.2f}%"
            if est_rt_fees is not None:
                extra += f"{nl}• Est. Roundtrip Fees: ${_fmt_money(float(est_rt_fees))}"
            if order_type_label:
                extra += f"{nl}• Order Type: {order_type_label}"
        except Exception:
            extra = ""

        body = (
            f"{header}{nl}"
            f"------------------------------------{nl}"
            f"• Entry Price: {entry_s}{nl}"
            f"• Exit Price: ${_fmt_price(px)}{nl}"
            f"• Reason: {why}{nl}"
            f"• NET P&L: {_fmt_pnl_signed(pnl_v)} (Fees Deducted)"
            f"{extra}{nl}"
            f"• Current Cash: {cash_s}{nl}"
            f"• Current Equity: {eq_s}"
        )
        await self.send(body)

    async def performance_report(
        self,
        *,
        trades: Sequence[Dict[str, Any]],
        paper_cash: float,
        paper_equity: float,
        report_date: Optional[datetime] = None,
    ) -> None:
        """
        Daily / on-demand summary — exact Al template.

        Each trade dict keys: when (datetime), symbol, cost, exit, pnl
        """
        nl = chr(10)
        date_s = _fmt_date_ct(report_date)
        lines: List[str] = [
            "📊 CRUZBOT PERFORMANCE REPORT",
            f"Date: {date_s}",
            "------------------------------------",
            "Date/Time     | Symbol   | Cost   | Exit   | Net P&L",
            "--------------------------------------------------",
        ]
        wins = losses = 0
        day_pnl = 0.0
        for t in trades:
            when = t.get("when")
            sym = str(t.get("symbol") or "")
            cost = float(t.get("cost") or 0)
            exit_n = float(t.get("exit") or 0)
            pnl = float(t.get("pnl") or 0)
            day_pnl += pnl
            if pnl >= 0:
                wins += 1
            else:
                losses += 1
            time_s = _fmt_time_ct(when if isinstance(when, datetime) else None)
            # pad lightly for monospace readability
            lines.append(
                f"{time_s:<12} | {sym:<8} | ${_fmt_money(cost)} | ${_fmt_money(exit_n)} | {_fmt_pnl_signed(pnl)}"
            )
        total = wins + losses
        win_rate = (wins / total * 100.0) if total else 0.0
        lines.append("--------------------------------------------------")
        lines.append(f"• Total Trades: {total}")
        lines.append(f"• Win Rate: {win_rate:.0f}% ({wins}W / {losses}L)")
        lines.append(f"• Day Net P&L: {_fmt_pnl_signed(day_pnl)}")
        lines.append(
            f"• Paper Bankroll: ${_fmt_money(paper_cash)} | ${_fmt_money(paper_equity)}"
        )
        await self.send(nl.join(lines))

    # --- System noise: ALWAYS silenced on Telegram (console/log only) ---

    async def stop_loss(self, symbol: str, detail: str = "") -> None:
        logger.info("Telegram silenced STOP LOSS %s | %s", symbol, detail)

    async def circuit_breaker(self, reason: str) -> None:
        logger.info("Telegram silenced CIRCUIT BREAKER: %s", reason)

    async def macro_pause(self, event_title: str, detail: str = "") -> None:
        logger.info("Telegram silenced MACRO_PAUSE: %s | %s", event_title, detail)

    async def kill_switch(self, detail: str = "Kill-switch engaged") -> None:
        logger.info("Telegram silenced KILL SWITCH: %s", detail)

    async def take_profit(
        self,
        symbol: str,
        detail: str = "",
        *,
        pnl: Optional[float] = None,
        pnl_pct: Optional[float] = None,
        qty: Optional[float] = None,
    ) -> None:
        """Immediate TAKE PROFIT HIT alert (always sends)."""
        if pnl is not None:
            money = _fmt_pnl_signed(float(pnl))
            if pnl_pct is not None:
                body = (
                    f"🟢 TAKE PROFIT HIT | {symbol} | PnL: {money} "
                    f"({float(pnl_pct):+.2f}%)"
                )
            else:
                body = f"🟢 TAKE PROFIT HIT | {symbol} | PnL: {money}"
        elif detail:
            body = f"🟢 TAKE PROFIT HIT | {symbol} | {detail}"
        else:
            body = f"🟢 TAKE PROFIT HIT | {symbol}"
        await self.send(body)

    async def shit_lets_see_summary(
        self,
        *,
        starting_equity: float,
        current_equity: float,
        realized_pnl_24h: float,
        realized_pnl_pct: float,
        trades_closed: int,
        wins: int,
        losses: int,
        open_exposure: float,
        report_date: Optional[datetime] = None,
        message: Optional[str] = None,
    ) -> None:
        """Once-daily morning PnL digest — actually sends Telegram (Al format)."""
        if message:
            body = str(message)
        else:
            date_s = _fmt_date_ct(report_date)
            pnl = float(realized_pnl_24h)
            if pnl >= 0:
                pnl_s = f"+${pnl:,.2f}"
                pct_s = f"+{float(realized_pnl_pct):.2f}%"
            else:
                pnl_s = f"-${abs(pnl):,.2f}"
                pct_s = f"{float(realized_pnl_pct):.2f}%"
            nl = chr(10)
            body = (
                f"📊 WEEKLY DIGEST 101 — [{date_s}]{nl}"
                f"Starting Equity: ${float(starting_equity):,.2f}{nl}"
                f"Current Equity: ${float(current_equity):,.2f}{nl}"
                f"24h Realized PnL: {pnl_s} ({pct_s}){nl}"
                f"Trades Closed: {int(trades_closed)} "
                f"({int(wins)} Win / {int(losses)} Loss){nl}"
                f"Active Open Exposure: ${float(open_exposure):,.2f}"
            )
        await self.send(body)

    async def daily_pnl_digest(
        self,
        *,
        day_pl: float,
        day_pl_pct: float,
        equity: float,
        fills: int = 0,
        extra: str = "",
    ) -> None:
        # Shutdown-path digest stays quiet (restarts must not spam Telegram).
        # Morning digest is scheduled separately via shit_lets_see_summary (Weekly Digest 101).
        logger.info(
            "Telegram silenced SHUTDOWN digest day_pl=%.2f (%.2f%%) equity=%.2f fills=%s %s",
            day_pl,
            day_pl_pct * 100,
            equity,
            fills,
            extra,
        )


def build_notifier(
    *,
    discord_webhook_url: str = "",
    telegram_bot_token: str = "",
    telegram_chat_id: str = "",
    quiet: bool = True,
) -> Notifier:
    return Notifier(
        discord_webhook_url=discord_webhook_url,
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id,
        quiet=True,  # Al: fills/exits/reports only
    )
