"""Once-daily Telegram PnL digest — schedule + snapshot helpers (America/Chicago)."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_CT = ZoneInfo("America/Chicago")
DEFAULT_DEDUPE_NAME = "last_daily_digest_date.txt"


@dataclass
class DigestSnapshot:
    """Live numbers for the cheeky morning summary."""

    date_ct: str  # display e.g. 09/19/2026
    date_key: str  # dedupe key YYYY-MM-DD in SUMMARY_TIMEZONE
    starting_equity: float
    current_equity: float
    realized_pnl_24h: float
    realized_pnl_pct: float
    trades_closed: int
    wins: int
    losses: int
    open_exposure: float

    def format_message(self) -> str:
        pnl = float(self.realized_pnl_24h)
        if pnl >= 0:
            pnl_s = f"+${pnl:,.2f}"
            pct_s = f"+{float(self.realized_pnl_pct):.2f}%"
        else:
            pnl_s = f"-${abs(pnl):,.2f}"
            pct_s = f"{float(self.realized_pnl_pct):.2f}%"
        return (
            f"📊 WEEKLY DIGEST 101 — [{self.date_ct}]\n"
            f"Starting Equity: ${float(self.starting_equity):,.2f}\n"
            f"Current Equity: ${float(self.current_equity):,.2f}\n"
            f"24h Realized PnL: {pnl_s} ({pct_s})\n"
            f"Trades Closed: {int(self.trades_closed)} "
            f"({int(self.wins)} Win / {int(self.losses)} Loss)\n"
            f"Active Open Exposure: ${float(self.open_exposure):,.2f}"
        )


def chicago_now() -> datetime:
    return datetime.now(_CT)


def resolve_tz(tz_name: str = "America/Chicago") -> ZoneInfo:
    try:
        return ZoneInfo((tz_name or "America/Chicago").strip() or "America/Chicago")
    except Exception:
        logger.warning("Invalid SUMMARY_TIMEZONE=%r — falling back to America/Chicago", tz_name)
        return _CT


def seconds_until_summary(
    hour: int = 7,
    minute: int = 0,
    tz_name: str = "America/Chicago",
    now: Optional[datetime] = None,
) -> float:
    """Seconds until next SUMMARY_HOUR:SUMMARY_MINUTE in SUMMARY_TIMEZONE (min 1s)."""
    tz = resolve_tz(tz_name)
    dt = now or datetime.now(tz)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    else:
        dt = dt.astimezone(tz)
    h = max(0, min(23, int(hour)))
    m = max(0, min(59, int(minute)))
    target = dt.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= dt:
        target = target + timedelta(days=1)
    return max(1.0, (target - dt).total_seconds())


def next_summary_datetime(
    hour: int = 7,
    minute: int = 0,
    tz_name: str = "America/Chicago",
    now: Optional[datetime] = None,
) -> datetime:
    tz = resolve_tz(tz_name)
    dt = now or datetime.now(tz)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    else:
        dt = dt.astimezone(tz)
    delay = seconds_until_summary(hour, minute, tz_name, now=dt)
    return dt + timedelta(seconds=delay)


def dedupe_path_for(project_root: Union[str, Path]) -> Path:
    return Path(project_root) / "data" / DEFAULT_DEDUPE_NAME


def already_sent_for_date(path: Path, date_key: str) -> bool:
    try:
        if not path.exists():
            return False
        return path.read_text(encoding="utf-8").strip() == str(date_key).strip()
    except Exception as exc:
        logger.debug("daily digest dedupe read failed: %s", exc)
        return False


def mark_sent_for_date(path: Path, date_key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(date_key).strip() + "\n", encoding="utf-8")


def load_paper_book(path: Union[str, Path]) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception as exc:
        logger.warning("paper book load failed (%s): %s", p, exc)
        return {}


def open_exposure_from_positions(positions: Any) -> float:
    """Sum abs(qty*mark or entry notional / market_value) of open positions."""
    total = 0.0
    if positions is None:
        return 0.0
    items: Sequence[Any]
    if isinstance(positions, Mapping):
        items = list(positions.values())
    elif isinstance(positions, (list, tuple)):
        items = positions
    else:
        return 0.0
    for pos in items:
        try:
            if isinstance(pos, Mapping):
                qty = float(pos.get("qty") or 0)
                mv = pos.get("market_value")
                if mv is not None:
                    total += abs(float(mv))
                    continue
                mark = pos.get("mark") or pos.get("mark_price") or pos.get("current_price")
                entry = pos.get("avg_entry_price") or pos.get("entry_price") or 0
                px = float(mark) if mark is not None else float(entry or 0)
                total += abs(qty * px)
            else:
                mv = getattr(pos, "market_value", None)
                if mv is not None:
                    total += abs(float(mv))
                    continue
                qty = float(getattr(pos, "qty", 0) or 0)
                entry = float(getattr(pos, "avg_entry_price", 0) or 0)
                total += abs(qty * entry)
        except (TypeError, ValueError):
            continue
    return float(total)


def closed_sells_24h(
    ledger_path: Union[str, Path],
    *,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """SELL/closed fills from paper_ledger.db in the trailing 24h window."""
    path = Path(ledger_path)
    if not path.exists():
        return []
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    else:
        now_dt = now_dt.astimezone(timezone.utc)
    cutoff = (now_dt - timedelta(hours=24)).timestamp()
    try:
        conn = sqlite3.connect(str(path))
        rows = list(
            conn.execute(
                "SELECT ts, kind, symbol, side, qty, price, notional, fee, pnl "
                "FROM events WHERE ts >= ? AND kind IN ('BUY','SELL','FILL') "
                "ORDER BY id",
                (cutoff,),
            )
        )
        conn.close()
    except Exception as exc:
        logger.debug("closed_sells_24h ledger read failed: %s", exc)
        return []

    out: List[Dict[str, Any]] = []
    for ts, kind, symbol, side, qty, price, notional, fee, pnl in rows:
        side_u = (side or "").upper()
        kind_u = (kind or "").upper()
        is_sell = kind_u == "SELL" or side_u == "SELL"
        if not is_sell:
            continue
        out.append(
            {
                "ts": float(ts or 0),
                "symbol": str(symbol or ""),
                "qty": float(qty or 0),
                "price": float(price or 0),
                "notional": float(notional or 0),
                "fee": float(fee or 0),
                "pnl": float(pnl or 0),
            }
        )
    return out


def closed_sells_24h_from_trade_memory(
    db_path: Union[str, Path],
    *,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Fallback: trading_bot*.db trade_memory rows in trailing 24h."""
    path = Path(db_path)
    if not path.exists():
        return []
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    else:
        now_dt = now_dt.astimezone(timezone.utc)
    cutoff = now_dt - timedelta(hours=24)
    try:
        conn = sqlite3.connect(str(path))
        rows = list(
            conn.execute(
                "SELECT ts, symbol, action, outcome, pnl FROM trade_memory ORDER BY id DESC"
            )
        )
        conn.close()
    except Exception as exc:
        logger.debug("trade_memory read failed: %s", exc)
        return []
    out: List[Dict[str, Any]] = []
    for ts, symbol, action, outcome, pnl in rows:
        try:
            when = datetime.fromisoformat(str(ts))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            else:
                when = when.astimezone(timezone.utc)
        except Exception:
            continue
        if when < cutoff:
            continue
        act = (action or "").upper()
        # Count exit/sell-ish outcomes only
        if act not in ("SELL", "EXIT", "STOP", "TP", "CLOSE") and (outcome or "").upper() not in (
            "WIN",
            "LOSS",
            "STOP",
            "TP",
        ):
            # If action looks like a recorded trade with pnl, still count
            if pnl is None:
                continue
        out.append(
            {
                "ts": when.timestamp(),
                "symbol": str(symbol or ""),
                "pnl": float(pnl or 0),
            }
        )
    return out


def build_digest_snapshot(
    *,
    paper_book_path: Union[str, Path],
    ledger_path: Union[str, Path],
    sqlite_path: Optional[Union[str, Path]] = None,
    starting_equity: Optional[float] = None,
    current_equity: Optional[float] = None,
    positions: Any = None,
    tz_name: str = "America/Chicago",
    now: Optional[datetime] = None,
) -> DigestSnapshot:
    """Assemble live digest numbers from paper book + ledger (24h realized)."""
    tz = resolve_tz(tz_name)
    now_local = now or datetime.now(tz)
    if now_local.tzinfo is None:
        now_local = now_local.replace(tzinfo=tz)
    else:
        now_local = now_local.astimezone(tz)

    book = load_paper_book(paper_book_path)
    start_eq = (
        float(starting_equity)
        if starting_equity is not None
        else float(book.get("day_start_equity") or book.get("equity") or 0.0)
    )
    cur_eq = (
        float(current_equity)
        if current_equity is not None
        else float(book.get("equity") or start_eq)
    )
    if positions is None:
        positions = book.get("positions") or {}
    exposure = open_exposure_from_positions(positions)

    sells = closed_sells_24h(ledger_path, now=now_local.astimezone(timezone.utc))
    if not sells and sqlite_path:
        sells = closed_sells_24h_from_trade_memory(
            sqlite_path, now=now_local.astimezone(timezone.utc)
        )

    realized = sum(float(s.get("pnl") or 0) for s in sells)
    wins = sum(1 for s in sells if float(s.get("pnl") or 0) >= 0)
    losses = sum(1 for s in sells if float(s.get("pnl") or 0) < 0)
    closed = len(sells)
    pct = (realized / start_eq * 100.0) if start_eq else 0.0

    return DigestSnapshot(
        date_ct=now_local.strftime("%m/%d/%Y"),
        date_key=now_local.strftime("%Y-%m-%d"),
        starting_equity=start_eq,
        current_equity=cur_eq,
        realized_pnl_24h=realized,
        realized_pnl_pct=pct,
        trades_closed=closed,
        wins=wins,
        losses=losses,
        open_exposure=exposure,
    )
