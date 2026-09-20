"""Append-only paper trade ledger (SQLite) for natural-signal review."""
from __future__ import annotations
import sqlite3, json, time
from pathlib import Path
from typing import Any, Dict, Optional

DB = Path(__file__).resolve().parents[1] / "data" / "paper_ledger.db"

def _conn() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB)
    c.execute(
        """CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            kind TEXT NOT NULL,
            symbol TEXT,
            side TEXT,
            qty REAL,
            price REAL,
            notional REAL,
            fee REAL,
            pnl REAL,
            note TEXT,
            raw TEXT
        )"""
    )
    c.commit()
    return c

def log_event(
    kind: str,
    *,
    symbol: str = "",
    side: str = "",
    qty: float = 0.0,
    price: float = 0.0,
    notional: float = 0.0,
    fee: float = 0.0,
    pnl: float = 0.0,
    note: str = "",
    raw: Optional[Dict[str, Any]] = None,
) -> None:
    c = _conn()
    c.execute(
        "INSERT INTO events (ts,kind,symbol,side,qty,price,notional,fee,pnl,note,raw) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            time.time(),
            kind,
            symbol,
            side,
            qty,
            price,
            notional,
            fee,
            pnl,
            note,
            json.dumps(raw or {}),
        ),
    )
    c.commit()
    c.close()

def update_last_fill_pnl(symbol: str, side: str, pnl: float) -> bool:
    """Set realized pnl on the most recent matching BUY/SELL fill row."""
    c = _conn()
    row = c.execute(
        "SELECT id FROM events WHERE kind IN ('BUY','SELL','FILL') AND symbol=? AND UPPER(side)=? ORDER BY id DESC LIMIT 1",
        (symbol, side.upper()),
    ).fetchone()
    if not row:
        c.close()
        return False
    c.execute("UPDATE events SET pnl=? WHERE id=?", (float(pnl), row[0]))
    c.commit()
    c.close()
    return True


def summarize(taker_fee: float = 0.009) -> Dict[str, Any]:
    c = _conn()
    rows = list(c.execute("SELECT kind,symbol,side,qty,price,notional,fee,pnl,note,ts FROM events ORDER BY id"))
    c.close()
    fills = [r for r in rows if r[0] in ("FILL", "BUY", "SELL")]
    scans = [r for r in rows if r[0] == "SCAN"]
    realized = sum(r[7] or 0 for r in rows)
    fees = sum(r[6] or 0 for r in rows)
    return {
        "events": len(rows),
        "scans": len(scans),
        "fills": len(fills),
        "realized_pnl": round(realized, 4),
        "fees": round(fees, 4),
        "net_after_fees": round(realized - fees, 4) if False else round(realized, 4),
        "assumed_taker_fee_rate": taker_fee,
        "last_events": [
            {
                "kind": r[0],
                "symbol": r[1],
                "side": r[2],
                "qty": r[3],
                "price": r[4],
                "notional": r[5],
                "fee": r[6],
                "pnl": r[7],
                "note": r[8],
            }
            for r in rows[-10:]
        ],
    }

if __name__ == "__main__":
    print(json.dumps(summarize(), indent=2))
