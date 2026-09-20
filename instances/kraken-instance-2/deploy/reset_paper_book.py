#!/usr/bin/env python3
"""Reset CruzBot paper book JSON wallets to a flat cash baseline.

Usage:
  python deploy/reset_paper_book.py [--cash 1600] [--path PATH ...]

Defaults to ACCOUNT_EQUITY from env/.env when --cash omitted (else 1600).
Does not touch SQLite trade_memory / paper_ledger.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _default_cash() -> float:
    for key in ("ACCOUNT_EQUITY", "PAPER_RESET_CASH"):
        raw = os.environ.get(key)
        if raw:
            try:
                return float(raw)
            except ValueError:
                pass
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            if s.startswith("export "):
                s = s[7:].strip()
            k, _, v = s.partition("=")
            if k.strip() == "ACCOUNT_EQUITY":
                try:
                    return float(v.strip().strip('"').strip("'"))
                except ValueError:
                    break
    return 1600.0


def reset_book(path: Path, cash: float) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    before = None
    if path.exists():
        try:
            before = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            before = {"_unreadable": True}
    payload = {
        "cash": float(cash),
        "equity": float(cash),
        "day_start_equity": float(cash),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "positions": {},
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return {"path": str(path), "before": before, "after": payload}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cash", type=float, default=None, help="Baseline cash/equity (default ACCOUNT_EQUITY or 1600)")
    ap.add_argument(
        "--path",
        action="append",
        dest="paths",
        help="Paper book JSON path (repeatable). Default: PAPER_BOOK_PATH + common locals.",
    )
    args = ap.parse_args()
    cash = float(args.cash) if args.cash is not None else _default_cash()

    paths: list[Path] = []
    if args.paths:
        paths = [Path(p) for p in args.paths]
    else:
        env_book = os.environ.get("PAPER_BOOK_PATH")
        if env_book:
            p = Path(env_book)
            if not p.is_absolute():
                p = PROJECT_ROOT / p
            paths.append(p)
        for rel in ("data/paper_book.json", "data/paper_book_2.json"):
            p = PROJECT_ROOT / rel
            if p.exists() or rel.endswith("_2.json") and (PROJECT_ROOT / "data").exists():
                if p not in paths:
                    # Always include paper_book.json; include _2 if present or instance tree
                    if rel.endswith("_2.json"):
                        if p.exists() or "instance_2" in str(PROJECT_ROOT):
                            paths.append(p)
                    else:
                        paths.append(p)

    # de-dupe while preserving order
    seen = set()
    uniq = []
    for p in paths:
        rp = p.resolve() if p.exists() else p
        key = str(rp)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)

    if not uniq:
        print("No paper book paths to reset", file=sys.stderr)
        return 1

    for p in uniq:
        result = reset_book(p, cash)
        b = result["before"] or {}
        a = result["after"]
        print(
            f"reset {result['path']}: "
            f"cash {b.get('cash','?')} -> {a['cash']}, "
            f"equity {b.get('equity','?')} -> {a['equity']}, "
            f"day_start {b.get('day_start_equity','?')} -> {a['day_start_equity']}, "
            f"positions cleared"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
