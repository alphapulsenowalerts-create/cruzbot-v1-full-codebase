"""SQLite-backed behavioral state: revenge lockouts + rolling trade memory."""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from trading_bot.models import TradeMemoryEntry, utcnow

logger = logging.getLogger(__name__)

DEFAULT_MEMORY_SIZE = 5


class BehavioralStateStore:
    """
    Persist:
      - per-symbol consecutive stop-loss counts + lockout_until
      - rolling trade memory (last N trades)

    Anti-revenge: after REVENGE_STOP_COUNT consecutive stop-losses on a symbol,
    lock that symbol for REVENGE_LOCKOUT_MINUTES.
    """

    def __init__(
        self,
        sqlite_path: str,
        *,
        revenge_stop_count: int = 2,
        revenge_lockout_minutes: int = 30,
        memory_size: int = DEFAULT_MEMORY_SIZE,
        post_stop_cooldown_min: int = 20,
    ) -> None:
        self.sqlite_path = sqlite_path
        self.revenge_stop_count = revenge_stop_count
        self.revenge_lockout_minutes = revenge_lockout_minutes
        self.memory_size = memory_size
        self.post_stop_cooldown_min = int(post_stop_cooldown_min)
        Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.sqlite_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS symbol_lockouts (
                    symbol TEXT PRIMARY KEY,
                    consecutive_stops INTEGER NOT NULL DEFAULT 0,
                    lockout_until TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trade_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    action TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    reason TEXT,
                    pnl REAL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trade_memory_ts ON trade_memory(ts DESC)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS post_stop_cooldown (
                    symbol TEXT PRIMARY KEY,
                    cooldown_until TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS buy_dedupe (
                    symbol TEXT PRIMARY KEY,
                    last_buy_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trade_failures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    signature_tag TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    block_until TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trade_failures_ts ON trade_failures(ts DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trade_failures_tag ON trade_failures(signature_tag)"
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS onchain_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    symbol TEXT,
                    event_type TEXT NOT NULL,
                    detail_json TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_onchain_events_ts ON onchain_events(ts DESC)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reslice_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    order_id TEXT,
                    old_price REAL,
                    new_price REAL,
                    depth_ahead REAL,
                    reason TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_reslice_events_ts ON reslice_events(ts DESC)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS coint_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    pair TEXT NOT NULL,
                    zscore REAL,
                    correlation REAL,
                    confidence REAL,
                    reason TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_coint_signals_ts ON coint_signals(ts DESC)"
            )
            logger.info(
                "state_store migration OK | tables=symbol_lockouts,trade_memory,"
                "post_stop_cooldown,buy_dedupe,trade_failures,"
                "onchain_events,reslice_events,coint_signals"
            )

    def is_locked_out(self, symbol: str, now: Optional[datetime] = None) -> bool:
        symbol = symbol.upper()
        now = now or utcnow()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT lockout_until FROM symbol_lockouts WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        if not row or not row["lockout_until"]:
            return False
        try:
            until = datetime.fromisoformat(row["lockout_until"])
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
        except ValueError:
            return False
        return now < until

    def lockout_remaining_seconds(self, symbol: str, now: Optional[datetime] = None) -> float:
        symbol = symbol.upper()
        now = now or utcnow()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT lockout_until FROM symbol_lockouts WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        if not row or not row["lockout_until"]:
            return 0.0
        try:
            until = datetime.fromisoformat(row["lockout_until"])
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
        except ValueError:
            return 0.0
        return max(0.0, (until - now).total_seconds())

    def record_stop_loss(self, symbol: str, now: Optional[datetime] = None) -> bool:
        """
        Increment consecutive stop count. Returns True if lockout was (re)activated.
        """
        symbol = symbol.upper()
        now = now or utcnow()
        locked = False
        with self._conn() as conn:
            row = conn.execute(
                "SELECT consecutive_stops, lockout_until FROM symbol_lockouts WHERE symbol = ?",
                (symbol,),
            ).fetchone()
            count = int(row["consecutive_stops"]) + 1 if row else 1
            lockout_until: Optional[str] = row["lockout_until"] if row else None
            if count >= self.revenge_stop_count:
                until = now + timedelta(minutes=self.revenge_lockout_minutes)
                lockout_until = until.isoformat()
                locked = True
                logger.warning(
                    "REVENGE_LOCKOUT %s after %s consecutive stops until %s",
                    symbol,
                    count,
                    lockout_until,
                )
            conn.execute(
                """
                INSERT INTO symbol_lockouts (symbol, consecutive_stops, lockout_until, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    consecutive_stops = excluded.consecutive_stops,
                    lockout_until = excluded.lockout_until,
                    updated_at = excluded.updated_at
                """,
                (symbol, count, lockout_until, now.isoformat()),
            )
        return locked

    def record_non_stop_outcome(self, symbol: str, now: Optional[datetime] = None) -> None:
        """Reset consecutive stop counter (e.g. win or non-stop exit). Does not clear active lockout."""
        symbol = symbol.upper()
        now = now or utcnow()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT lockout_until FROM symbol_lockouts WHERE symbol = ?",
                (symbol,),
            ).fetchone()
            lockout_until = row["lockout_until"] if row else None
            # Clear expired lockouts when resetting streak
            if lockout_until:
                try:
                    until = datetime.fromisoformat(lockout_until)
                    if until.tzinfo is None:
                        until = until.replace(tzinfo=timezone.utc)
                    if now >= until:
                        lockout_until = None
                except ValueError:
                    lockout_until = None
            conn.execute(
                """
                INSERT INTO symbol_lockouts (symbol, consecutive_stops, lockout_until, updated_at)
                VALUES (?, 0, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    consecutive_stops = 0,
                    lockout_until = excluded.lockout_until,
                    updated_at = excluded.updated_at
                """,
                (symbol, lockout_until, now.isoformat()),
            )

    def consecutive_stops(self, symbol: str) -> int:
        symbol = symbol.upper()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT consecutive_stops FROM symbol_lockouts WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        return int(row["consecutive_stops"]) if row else 0

    def add_trade_memory(
        self,
        *,
        symbol: str,
        action: str,
        outcome: str,
        reason: str = "",
        pnl: Optional[float] = None,
        ts: Optional[datetime] = None,
    ) -> TradeMemoryEntry:
        ts = ts or utcnow()
        entry = TradeMemoryEntry(
            symbol=symbol.upper(),
            action=action,
            outcome=outcome,
            reason=reason,
            pnl=pnl,
            timestamp=ts,
        )
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO trade_memory (ts, symbol, action, outcome, reason, pnl)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.timestamp.isoformat(),
                    entry.symbol,
                    entry.action,
                    entry.outcome,
                    entry.reason,
                    entry.pnl,
                ),
            )
            # Trim to last memory_size * 3 rows then keep newest memory_size in API
            conn.execute(
                """
                DELETE FROM trade_memory WHERE id NOT IN (
                    SELECT id FROM trade_memory ORDER BY id DESC LIMIT ?
                )
                """,
                (max(self.memory_size * 5, 25),),
            )
        return entry

    def get_trade_memory(self, limit: Optional[int] = None) -> List[TradeMemoryEntry]:
        n = limit if limit is not None else self.memory_size
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT ts, symbol, action, outcome, reason, pnl
                FROM trade_memory
                ORDER BY id DESC
                LIMIT ?
                """,
                (n,),
            ).fetchall()
        out: List[TradeMemoryEntry] = []
        for row in reversed(rows):  # chronological for prompt context
            try:
                ts = datetime.fromisoformat(row["ts"])
            except ValueError:
                ts = utcnow()
            out.append(
                TradeMemoryEntry(
                    symbol=row["symbol"],
                    action=row["action"],
                    outcome=row["outcome"],
                    reason=row["reason"] or "",
                    pnl=row["pnl"],
                    timestamp=ts,
                )
            )
        return out


    def record_post_stop_cooldown(
        self, symbol: str, now: Optional[datetime] = None, minutes: Optional[int] = None
    ) -> datetime:
        """Block new BUYs on symbol for post_stop_cooldown_min after stop/trail exit."""
        symbol = symbol.upper()
        now = now or utcnow()
        mins = int(minutes if minutes is not None else self.post_stop_cooldown_min)
        until = now + timedelta(minutes=max(0, mins))
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO post_stop_cooldown (symbol, cooldown_until, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    cooldown_until = excluded.cooldown_until,
                    updated_at = excluded.updated_at
                """,
                (symbol, until.isoformat(), now.isoformat()),
            )
        logger.info("POST_STOP_COOLDOWN %s until %s (%sm)", symbol, until.isoformat(), mins)
        return until

    def in_post_stop_cooldown(self, symbol: str, now: Optional[datetime] = None) -> bool:
        symbol = symbol.upper()
        now = now or utcnow()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT cooldown_until FROM post_stop_cooldown WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        if not row or not row["cooldown_until"]:
            return False
        try:
            until = datetime.fromisoformat(row["cooldown_until"])
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
        except ValueError:
            return False
        return now < until

    def post_stop_cooldown_remaining(self, symbol: str, now: Optional[datetime] = None) -> float:
        symbol = symbol.upper()
        now = now or utcnow()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT cooldown_until FROM post_stop_cooldown WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        if not row or not row["cooldown_until"]:
            return 0.0
        try:
            until = datetime.fromisoformat(row["cooldown_until"])
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
        except ValueError:
            return 0.0
        return max(0.0, (until - now).total_seconds())


    def record_buy_attempt(self, symbol: str, now: Optional[datetime] = None) -> datetime:
        """Stamp last BUY attempt/fill for dedupe lock (per symbol)."""
        symbol = symbol.upper()
        now = now or utcnow()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO buy_dedupe (symbol, last_buy_at, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    last_buy_at = excluded.last_buy_at,
                    updated_at = excluded.updated_at
                """,
                (symbol, now.isoformat(), now.isoformat()),
            )
        logger.info("BUY_DEDUPE stamp %s at %s", symbol, now.isoformat())
        return now

    def seconds_since_last_buy(self, symbol: str, now: Optional[datetime] = None) -> Optional[float]:
        symbol = symbol.upper()
        now = now or utcnow()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT last_buy_at FROM buy_dedupe WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        if not row or not row["last_buy_at"]:
            return None
        try:
            ts = datetime.fromisoformat(row["last_buy_at"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        return max(0.0, (now - ts).total_seconds())

    def in_buy_dedupe(
        self, symbol: str, window_seconds: float, now: Optional[datetime] = None
    ) -> bool:
        elapsed = self.seconds_since_last_buy(symbol, now=now)
        if elapsed is None:
            return False
        window = float(window_seconds or 0)
        if window <= 0:
            return False
        return elapsed < window

    def buy_dedupe_remaining(
        self, symbol: str, window_seconds: float, now: Optional[datetime] = None
    ) -> float:
        elapsed = self.seconds_since_last_buy(symbol, now=now)
        if elapsed is None:
            return 0.0
        window = float(window_seconds or 0)
        return max(0.0, window - elapsed)



    # --- trade_failures / post-mortem blacklist ---

    def record_trade_failure(
        self,
        *,
        symbol: str,
        signature_tag: str,
        snapshot: Dict[str, Any],
        block_minutes: float = 45.0,
        now: Optional[datetime] = None,
    ) -> int:
        """Persist SL post-mortem snapshot + arm temporary blacklist window."""
        symbol = symbol.upper()
        now = now or utcnow()
        block_until = now + timedelta(minutes=max(0.0, float(block_minutes)))
        payload = json.dumps(snapshot or {}, default=str)
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO trade_failures
                    (ts, symbol, signature_tag, snapshot_json, block_until, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    now.isoformat(),
                    symbol,
                    signature_tag,
                    payload,
                    block_until.isoformat(),
                    now.isoformat(),
                ),
            )
            row_id = int(cur.lastrowid)
        logger.info(
            "TRADE_FAILURE recorded id=%s %s tag=%s block_until=%s",
            row_id,
            symbol,
            signature_tag,
            block_until.isoformat(),
        )
        return row_id

    def get_active_failure_blacklist(
        self,
        *,
        lookback_hours: float = 72.0,
        now: Optional[datetime] = None,
        symbol: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return recent failures still inside their block_until window."""
        now = now or utcnow()
        since = now - timedelta(hours=max(0.1, float(lookback_hours)))
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, ts, symbol, signature_tag, snapshot_json, block_until
                FROM trade_failures
                WHERE ts >= ?
                ORDER BY id DESC
                LIMIT 200
                """,
                (since.isoformat(),),
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for row in rows:
            bu = row["block_until"]
            if not bu:
                continue
            try:
                until = datetime.fromisoformat(bu)
                if until.tzinfo is None:
                    until = until.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if now >= until:
                continue
            if symbol and row["symbol"].upper() != symbol.upper():
                # still include cross-symbol liquidity tags later in matcher
                pass
            snap: Dict[str, Any] = {}
            try:
                snap = json.loads(row["snapshot_json"] or "{}")
            except json.JSONDecodeError:
                snap = {}
            out.append(
                {
                    "id": row["id"],
                    "ts": row["ts"],
                    "symbol": row["symbol"],
                    "signature_tag": row["signature_tag"],
                    "tag": row["signature_tag"],
                    "snapshot": snap,
                    "block_until": bu,
                }
            )
        return out

    def clear_expired_trade_failures(self, now: Optional[datetime] = None) -> int:
        """Optional cleanup of very old rows (>30d)."""
        now = now or utcnow()
        cutoff = now - timedelta(days=30)
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM trade_failures WHERE ts < ?",
                (cutoff.isoformat(),),
            )
            return int(cur.rowcount or 0)

    def memory_for_prompt(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        return [e.model_dump(mode="json") for e in self.get_trade_memory(limit)]


    def record_onchain_event(
        self,
        *,
        event_type: str,
        symbol: str = "",
        detail: Optional[Dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> None:
        now = now or utcnow()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO onchain_events (ts, symbol, event_type, detail_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    now.isoformat(),
                    (symbol or "").upper(),
                    event_type,
                    json.dumps(detail or {}, default=str),
                    now.isoformat(),
                ),
            )

    def record_reslice_event(
        self,
        *,
        symbol: str,
        order_id: str = "",
        old_price: float = 0.0,
        new_price: float = 0.0,
        depth_ahead: float = 0.0,
        reason: str = "",
        now: Optional[datetime] = None,
    ) -> None:
        now = now or utcnow()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO reslice_events
                    (ts, symbol, order_id, old_price, new_price, depth_ahead, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now.isoformat(),
                    symbol.upper(),
                    order_id,
                    float(old_price),
                    float(new_price),
                    float(depth_ahead),
                    reason,
                    now.isoformat(),
                ),
            )

    def record_coint_signal(
        self,
        *,
        symbol: str,
        pair: str,
        zscore: float,
        correlation: float,
        confidence: float,
        reason: str = "",
        now: Optional[datetime] = None,
    ) -> None:
        now = now or utcnow()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO coint_signals
                    (ts, symbol, pair, zscore, correlation, confidence, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now.isoformat(),
                    symbol.upper(),
                    pair,
                    float(zscore),
                    float(correlation),
                    float(confidence),
                    reason,
                    now.isoformat(),
                ),
            )

