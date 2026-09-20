"""Pair blacklist via local SQLite trades.db (Elite Day-Trader).

On each close: persist symbol, entry, exit, net_pnl, timestamp.
Pre-trade: if last 3 closed trades for symbol in last 24h are ALL losses
→ skip that symbol for 12 hours (persist until_ts).
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_DB = "data/trades.db"
LOSS_LOOKBACK_HOURS = 24.0
LOSS_STREAK = 3
BLACKLIST_HOURS = 12.0


def _normalize(symbol: str) -> str:
    return (symbol or "").strip().upper().replace("/", "-").replace("_", "-")


class PairBlacklist:
    """SQLite-backed closed-trade history + timed symbol blacklist."""

    def __init__(self, db_path: Path | str, *, enabled: bool = True) -> None:
        self.path = Path(db_path)
        self.enabled = bool(enabled)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self.path), timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def _init_db(self) -> None:
        with self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS closed_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    entry REAL,
                    exit REAL,
                    net_pnl REAL NOT NULL,
                    timestamp REAL NOT NULL
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS blacklist (
                    symbol TEXT PRIMARY KEY,
                    until_ts REAL NOT NULL,
                    reason TEXT
                )
                """
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_closed_sym_ts "
                "ON closed_trades(symbol, timestamp)"
            )
            c.commit()

    def clear_closed_history(self) -> int:
        """Delete all closed_trades (+ blacklist). Used by /wipe_paper only."""
        with self._conn() as c:
            try:
                n = int(c.execute("SELECT COUNT(*) FROM closed_trades").fetchone()[0])
            except Exception:
                n = 0
            c.execute("DELETE FROM closed_trades")
            try:
                c.execute("DELETE FROM blacklist")
            except Exception:
                pass
            try:
                c.execute("DELETE FROM sqlite_sequence WHERE name='closed_trades'")
            except Exception:
                pass
            c.commit()
        logger.warning("[PAIR_BLACKLIST] cleared closed_trades n=%s", n)
        return n

    def record_close(
        self,
        symbol: str,
        *,
        entry: Optional[float],
        exit: Optional[float],
        net_pnl: float,
        timestamp: Optional[float] = None,
    ) -> Optional[float]:
        """Persist close; if 3 losses/24h, blacklist 12h. Returns until_ts if newly blacklisted."""
        if not self.enabled:
            return None
        sym = _normalize(symbol)
        ts = float(timestamp if timestamp is not None else time.time())
        with self._conn() as c:
            c.execute(
                "INSERT INTO closed_trades (symbol, entry, exit, net_pnl, timestamp) "
                "VALUES (?,?,?,?,?)",
                (sym, entry, exit, float(net_pnl), ts),
            )
            c.commit()
        return self._maybe_blacklist(sym, now=ts)

    def _maybe_blacklist(self, symbol: str, *, now: Optional[float] = None) -> Optional[float]:
        now = float(now if now is not None else time.time())
        cutoff = now - LOSS_LOOKBACK_HOURS * 3600.0
        with self._conn() as c:
            rows = c.execute(
                "SELECT net_pnl FROM closed_trades WHERE symbol=? AND timestamp>=? "
                "ORDER BY timestamp DESC LIMIT ?",
                (symbol, cutoff, LOSS_STREAK),
            ).fetchall()
            if len(rows) < LOSS_STREAK:
                return None
            if not all(float(r["net_pnl"]) < 0.0 for r in rows):
                return None
            until = now + BLACKLIST_HOURS * 3600.0
            c.execute(
                "INSERT INTO blacklist(symbol, until_ts, reason) VALUES(?,?,?) "
                "ON CONFLICT(symbol) DO UPDATE SET until_ts=excluded.until_ts, "
                "reason=excluded.reason",
                (symbol, until, f"{LOSS_STREAK}_losses_{int(LOSS_LOOKBACK_HOURS)}h"),
            )
            c.commit()
        logger.warning(
            "[BLACKLIST] %s 12h after 3 losses",
            symbol,
        )
        return until

    def is_blocked(self, symbol: str, *, now: Optional[float] = None) -> Tuple[bool, str]:
        if not self.enabled:
            return False, "blacklist: disabled"
        sym = _normalize(symbol)
        now = float(now if now is not None else time.time())
        with self._conn() as c:
            row = c.execute(
                "SELECT until_ts, reason FROM blacklist WHERE symbol=?", (sym,)
            ).fetchone()
            if not row:
                return False, "blacklist: clear"
            until = float(row["until_ts"])
            if until <= now:
                c.execute("DELETE FROM blacklist WHERE symbol=?", (sym,))
                c.commit()
                return False, "blacklist: expired"
            rem_h = max(0.0, (until - now) / 3600.0)
            return True, f"blacklist: until {until:.0f} ({rem_h:.1f}h left)"

    def check_entry(self, symbol: str) -> Tuple[bool, str]:
        """Pre-trade hook. Returns (allowed, reason)."""
        blocked, detail = self.is_blocked(symbol)
        if blocked:
            return False, detail
        return True, "blacklist: ok"

    def status_note(self) -> Optional[str]:
        if not self.enabled:
            return None
        now = time.time()
        with self._conn() as c:
            rows = c.execute(
                "SELECT symbol, until_ts FROM blacklist WHERE until_ts>?", (now,)
            ).fetchall()
        if not rows:
            return None
        parts = [
            f"{r['symbol']}:{(float(r['until_ts']) - now) / 3600.0:.1f}h" for r in rows
        ]
        return "bl=" + ",".join(parts)
