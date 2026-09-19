#!/usr/bin/env python3
"""Reset paper trading wallet equity/cash to ACCOUNT_EQUITY (default $1600)."""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def reset_book(path: Path, equity: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cash": float(equity),
        "equity": float(equity),
        "day_start_equity": float(equity),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "positions": {},
    }
    if path.exists():
        bak = path.with_suffix(path.suffix + f".bak_pre_reset_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        bak.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.write_text(json.dumps(payload, indent=2) + "
", encoding="utf-8")
    print(f"paper_book -> {path} equity=${equity:.2f}")


def reset_sqlite(db_path: Path, equity: float) -> None:
    if not db_path.exists():
        print(f"skip sqlite (missing): {db_path}")
        return
    con = sqlite3.connect(str(db_path))
    try:
        cur = con.cursor()
        tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        # Best-effort: common ledger/meta tables if present
        if "paper_meta" in tables:
            cur.execute(
                "INSERT INTO paper_meta(key, value) VALUES('starting_equity', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(equity),),
            )
        con.commit()
        print(f"sqlite touched: {db_path} tables={sorted(tables)}")
    finally:
        con.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--equity", type=float, default=1600.0)
    ap.add_argument("--book", type=Path, default=ROOT / "data" / "paper_book.json")
    ap.add_argument("--db", type=Path, default=ROOT / "data" / "trading_bot.db")
    args = ap.parse_args()
    reset_book(args.book, args.equity)
    reset_sqlite(args.db, args.equity)


if __name__ == "__main__":
    main()
