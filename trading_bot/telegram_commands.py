"""Interactive Telegram command long-poll (authorized chat only).

User-initiated replies only — never unsolicited status spam.
Commands: /status /pause /resume /pnl /kill /mode /confirm_live
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_CT = ZoneInfo("America/Chicago")

CommandHandler = Callable[[str, List[str]], Awaitable[str]]

KNOWN_COMMANDS = frozenset(
    {"status", "pause", "resume", "pnl", "kill", "mode", "confirm_live"}
)

LIVE_CONFIRM_TTL_SECONDS = 90.0

REPLY_MODE_LIVE_PENDING = (
    "Type /confirm_live to switch to REAL capital execution."
)
REPLY_CONFIRM_EXPIRED = (
    "Confirmation expired or missing — run /mode live first."
)
ALERT_LIVE_ACTIVATED = "LIVE MODE ACTIVATED — real capital."
ALERT_PAPER_RESTORED = "PAPER MODE RESTORED."



def parse_command(text: str) -> Optional[tuple[str, List[str]]]:
    """Parse `/cmd@bot args` → (cmd, args) or None if not a known command."""
    if not text:
        return None
    raw = text.strip()
    if not raw.startswith("/"):
        return None
    parts = raw.split()
    head = parts[0][1:]  # drop leading /
    if "@" in head:
        head = head.split("@", 1)[0]
    cmd = head.strip().lower()
    if cmd not in KNOWN_COMMANDS:
        return None
    return cmd, parts[1:]


def format_status_reply(
    *,
    paper_cash: float,
    paper_equity: float,
    positions: Sequence[Dict[str, Any]],
    paused: bool,
    strategy_mode: str,
    last_tick_age_seconds: Optional[float],
    pid: int,
    paper: bool = True,
    entry_proximity: Optional[Dict[str, Any]] = None,
    proximity_symbol: Optional[str] = None,
    proximity_price: Optional[float] = None,
) -> str:
    """Brief /status reply text (optional entry-proximity bar)."""
    from trading_bot.utils.indicators import make_progress_bar

    nl = chr(10)
    mode = "PAPER" if paper else "LIVE"
    pause_s = "PAUSED (no new buys)" if paused else "running"
    if last_tick_age_seconds is None:
        tick_s = "n/a"
    else:
        tick_s = f"{float(last_tick_age_seconds):.1f}s"
    lines = [
        f"CruzBot {mode} status",
        f"cash=${float(paper_cash):.2f} equity=${float(paper_equity):.2f}",
        f"pause={pause_s}",
        f"strategy={strategy_mode}",
        f"last_tick_age={tick_s}",
        f"pid={pid}",
    ]
    if entry_proximity is not None:
        direction = str(entry_proximity.get("direction") or "HOLD")
        try:
            score = float(entry_proximity.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        lines.append(f"Target Setup: {direction}")
        lines.append(f"Entry Proximity: {make_progress_bar(score)}")
        if proximity_symbol:
            if proximity_price is not None and float(proximity_price) > 0:
                lines.append(
                    f"focus={proximity_symbol} @ ${float(proximity_price):.4g}"
                )
            else:
                lines.append(f"focus={proximity_symbol}")
    if not positions:
        lines.append("positions: (none)")
    else:
        lines.append(f"positions ({len(positions)}):")
        for p in positions:
            sym = p.get("symbol") or "?"
            qty = float(p.get("qty") or 0)
            entry = p.get("avg_entry_price")
            mv = p.get("market_value")
            entry_s = f"${float(entry):.4g}" if entry is not None else "?"
            mv_s = f"${float(mv):.2f}" if mv is not None else "?"
            lines.append(f"  {sym} qty={qty:.6g} entry={entry_s} mv={mv_s}")
    return nl.join(lines)


def format_mode_reply(*, paper: bool, cash: float, equity: float) -> str:
    """Brief /mode reply: MODE: PAPER|LIVE plus cash/equity one-liner."""
    mode = "PAPER" if paper else "LIVE"
    return f"MODE: {mode} | cash=${float(cash):.2f} equity=${float(equity):.2f}"


def day_trades_from_ledger(
    db_path: Optional[str] = None,
    *,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Build performance_report trade rows from today's SELL fills in paper_ledger."""
    import sqlite3
    from pathlib import Path

    path = Path(db_path) if db_path else Path(__file__).resolve().parents[1] / "data" / "paper_ledger.db"
    if not path.exists():
        return []
    now_dt = now or datetime.now(_CT)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=_CT)
    else:
        now_dt = now_dt.astimezone(_CT)
    day_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    start_ts = day_start.timestamp()

    try:
        conn = sqlite3.connect(str(path))
        rows = list(
            conn.execute(
                "SELECT ts, kind, symbol, side, qty, price, notional, fee, pnl "
                "FROM events WHERE ts >= ? AND kind IN ('BUY','SELL','FILL') ORDER BY id",
                (start_ts,),
            )
        )
        conn.close()
    except Exception as exc:
        logger.debug("day_trades_from_ledger failed: %s", exc)
        return []

    # Pair: use SELL rows with pnl; cost from notional or prior BUY
    buys_by_sym: Dict[str, List[Dict[str, Any]]] = {}
    trades: List[Dict[str, Any]] = []
    for ts, kind, symbol, side, qty, price, notional, fee, pnl in rows:
        side_u = (side or "").upper()
        kind_u = (kind or "").upper()
        is_buy = kind_u == "BUY" or side_u == "BUY"
        is_sell = kind_u == "SELL" or side_u == "SELL"
        when = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        sym = str(symbol or "")
        if is_buy and not is_sell:
            buys_by_sym.setdefault(sym, []).append(
                {
                    "when": when,
                    "cost": float(notional or 0) or (float(qty or 0) * float(price or 0)),
                    "price": float(price or 0),
                    "qty": float(qty or 0),
                }
            )
            continue
        if is_sell:
            cost = 0.0
            pending = buys_by_sym.get(sym) or []
            if pending:
                b = pending.pop(0)
                cost = float(b.get("cost") or 0)
            exit_n = float(notional or 0) or (float(qty or 0) * float(price or 0))
            trades.append(
                {
                    "when": when,
                    "symbol": sym,
                    "cost": cost,
                    "exit": exit_n,
                    "pnl": float(pnl or 0),
                }
            )
    return trades


class OpsControlState:
    """Shared pause / kill / live-confirm flags for the trading loop."""

    def __init__(self) -> None:
        self.paused: bool = False
        self.kill_requested: bool = False
        self.kill_liquidate: bool = False
        self._pending_live_confirm_until: float = 0.0

    def set_pause(self, value: bool) -> None:
        self.paused = bool(value)

    def request_kill(self, *, liquidate: bool = True) -> None:
        self.kill_requested = True
        self.kill_liquidate = bool(liquidate)

    def arm_live_confirm(self, ttl: float = LIVE_CONFIRM_TTL_SECONDS) -> float:
        """Arm pending LIVE confirm; returns TTL seconds used."""
        ttl_f = float(ttl)
        self._pending_live_confirm_until = time.monotonic() + ttl_f
        return ttl_f

    def clear_live_confirm(self) -> None:
        self._pending_live_confirm_until = 0.0

    def has_pending_live_confirm(self, now: Optional[float] = None) -> bool:
        """True if a non-expired /mode live confirm is pending."""
        deadline = float(self._pending_live_confirm_until or 0.0)
        if deadline <= 0:
            return False
        now_m = time.monotonic() if now is None else float(now)
        if now_m > deadline:
            self._pending_live_confirm_until = 0.0
            return False
        return True

    def pending_live_confirm_remaining(self, now: Optional[float] = None) -> float:
        if not self.has_pending_live_confirm(now=now):
            return 0.0
        now_m = time.monotonic() if now is None else float(now)
        return max(0.0, float(self._pending_live_confirm_until) - now_m)


class TelegramCommandListener:
    """Long-poll getUpdates; only TELEGRAM_CHAT_ID; reply briefly per command."""

    def __init__(
        self,
        *,
        bot_token: str,
        chat_id: str,
        handlers: Dict[str, CommandHandler],
        enabled: bool = True,
        timeout: float = 25.0,
        poll_timeout: int = 25,
    ) -> None:
        self.bot_token = (bot_token or "").strip()
        self.chat_id = str(chat_id or "").strip()
        self.handlers = handlers
        self.enabled = bool(enabled)
        self.timeout = timeout
        self.poll_timeout = int(poll_timeout)
        self._offset: int = 0
        self._running = False

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.bot_token and self.chat_id)

    async def _send_reply(self, text: str) -> None:
        if not self.configured:
            return
        try:
            import httpx
        except ImportError:
            logger.warning("httpx missing — telegram command reply skipped")
            return
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    url,
                    json={
                        "chat_id": self.chat_id,
                        "text": (text or "")[:3500],
                        "disable_web_page_preview": True,
                    },
                )
                if resp.status_code >= 400:
                    logger.warning("Telegram command reply status %s", resp.status_code)
        except Exception as exc:
            logger.warning("Telegram command reply failed: %s", exc)

    async def _get_updates(self) -> List[Dict[str, Any]]:
        try:
            import httpx
        except ImportError:
            return []
        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
        params = {
            "offset": self._offset,
            "timeout": self.poll_timeout,
            "allowed_updates": ["message"],
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout + self.poll_timeout + 5) as client:
                resp = await client.get(url, params=params)
                if resp.status_code >= 400:
                    logger.warning("getUpdates status %s", resp.status_code)
                    return []
                data = resp.json()
                if not data.get("ok"):
                    return []
                return list(data.get("result") or [])
        except Exception as exc:
            logger.debug("getUpdates error: %s", exc)
            return []

    async def handle_update(self, update: Dict[str, Any]) -> None:
        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        if chat_id != self.chat_id:
            logger.debug("Ignoring telegram update from chat_id=%s", chat_id)
            return
        text = str(msg.get("text") or "")
        parsed = parse_command(text)
        if parsed is None:
            return
        cmd, args = parsed
        handler = self.handlers.get(cmd)
        if handler is None:
            await self._send_reply(f"Unknown command /{cmd}")
            return
        try:
            reply = await handler(cmd, args)
        except Exception as exc:
            logger.exception("telegram command /%s failed", cmd)
            reply = f"/{cmd} error: {exc}"
        if reply:
            await self._send_reply(reply)

    async def run(self, stop_event: Any) -> None:
        """Background long-poll until stop_event is set."""
        if not self.configured:
            logger.info("Telegram commands disabled or not configured — listener idle")
            return
        self._running = True
        logger.info(
            "Telegram commands armed (chat_id=%s) — /status /pause /resume /pnl /kill /mode /confirm_live",
            self.chat_id,
        )
        # Drop pending updates so we don't reply to stale commands after restart
        try:
            import httpx

            url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, params={"offset": -1, "timeout": 0})
                if resp.status_code < 400:
                    data = resp.json()
                    results = list((data or {}).get("result") or [])
                    if results:
                        self._offset = int(results[-1].get("update_id", 0)) + 1
        except Exception as exc:
            logger.debug("telegram offset warm-up skipped: %s", exc)

        while self._running and not stop_event.is_set():
            try:
                updates = await self._get_updates()
                for upd in updates:
                    uid = int(upd.get("update_id") or 0)
                    if uid >= self._offset:
                        self._offset = uid + 1
                    await self.handle_update(upd)
            except Exception as exc:
                logger.warning("telegram poll loop error: %s", exc)
                await _sleep_interruptible(stop_event, 3.0)
            # brief yield if getUpdates returned immediately
            if stop_event.is_set():
                break
        self._running = False
        logger.info("Telegram command listener stopped")

    def stop(self) -> None:
        self._running = False


async def _sleep_interruptible(stop_event: Any, seconds: float) -> None:
    import asyncio

    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except Exception:
        pass


def current_pid() -> int:
    return os.getpid()
