"""Structured console logging + SQLite (optional Postgres) persistence."""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from trading_bot.models import Decision, OrderRequest, OrderResult, utcnow

logger = logging.getLogger(__name__)


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


class TradeLogger:
    """Persists decisions / orders / signals to SQLite; optional Postgres DSN."""

    def __init__(self, sqlite_path: str, postgres_dsn: str = "") -> None:
        self.sqlite_path = sqlite_path
        self.postgres_dsn = postgres_dsn
        Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_sqlite()
        self._pg = None
        if postgres_dsn:
            self._try_connect_postgres()

    def _try_connect_postgres(self) -> None:
        try:
            import psycopg  # type: ignore

            self._pg = psycopg.connect(self.postgres_dsn)
            with self._pg.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS events (
                        id SERIAL PRIMARY KEY,
                        ts TIMESTAMPTZ NOT NULL,
                        event_type TEXT NOT NULL,
                        symbol TEXT,
                        payload JSONB NOT NULL
                    )
                    """
                )
            self._pg.commit()
            logger.info("Postgres event store connected")
        except Exception as exc:
            logger.warning("Postgres unavailable (%s); continuing with SQLite only", exc)
            self._pg = None

    def _init_sqlite(self) -> None:
        with self._sqlite() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    symbol TEXT,
                    payload TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_symbol ON events(symbol)"
            )

    @contextmanager
    def _sqlite(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.sqlite_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def log_event(
        self,
        event_type: str,
        payload: Dict[str, Any],
        symbol: Optional[str] = None,
    ) -> None:
        ts = utcnow().isoformat()
        body = json.dumps(payload, default=str)
        logger.info("EVENT %s %s %s", event_type, symbol or "-", body[:200])
        with self._sqlite() as conn:
            conn.execute(
                "INSERT INTO events (ts, event_type, symbol, payload) VALUES (?, ?, ?, ?)",
                (ts, event_type, symbol, body),
            )
        if self._pg is not None:
            try:
                with self._pg.cursor() as cur:
                    cur.execute(
                        "INSERT INTO events (ts, event_type, symbol, payload) VALUES (%s, %s, %s, %s::jsonb)",
                        (ts, event_type, symbol, body),
                    )
                self._pg.commit()
            except Exception as exc:
                logger.warning("Postgres write failed: %s", exc)

    def log_decision(self, decision: Decision) -> None:
        self.log_event("decision", decision.model_dump(mode="json"), decision.symbol)

    def log_signal(self, symbol: str, payload: Dict[str, Any]) -> None:
        self.log_event("signal", payload, symbol)

    def log_order(self, order: OrderRequest, result: Optional[OrderResult] = None) -> None:
        payload: Dict[str, Any] = {"request": order.model_dump(mode="json")}
        if result:
            payload["result"] = result.model_dump(mode="json")
        self.log_event("order", payload, order.symbol)

    def close(self) -> None:
        if self._pg is not None:
            try:
                self._pg.close()
            except Exception:
                pass
            self._pg = None
