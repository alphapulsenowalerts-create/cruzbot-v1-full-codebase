#!/usr/bin/env python3
"""Decide if paper track record is good enough to recommend live (advisory only)."""
from __future__ import annotations
import json, sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "paper_ledger.db"
MIN_ROUND_TRIPS = 5
MIN_NET = 0.0  # net after fees >= flat

def main() -> None:
    if not DB.exists():
        print(json.dumps({"ready": False, "reason": "no ledger yet", "fills": 0}))
        return
    con = sqlite3.connect(DB)
    rows = list(con.execute(
        "SELECT kind,symbol,side,qty,price,notional,fee,pnl,note FROM events ORDER BY id"
    ))
    con.close()
    buys = [r for r in rows if r[0] in ("BUY", "FILL") and str(r[2]).upper().startswith("B")]
    sells = [r for r in rows if r[0] in ("SELL", "FILL") and str(r[2]).upper().startswith("S")]
    fees = sum(float(r[6] or 0) for r in rows)
    pnl = sum(float(r[7] or 0) for r in rows)
    # Approximate round-trips = min(buys, sells)
    rts = min(len(buys), len(sells))
    net = pnl - fees if pnl else -fees
    # If pnl column unused, estimate from paired notionals later — for now require explicit pnl or sells
    ready = rts >= MIN_ROUND_TRIPS and net >= MIN_NET and len(sells) >= MIN_ROUND_TRIPS
    print(json.dumps({
        "ready_for_live_recommendation": ready,
        "round_trips_approx": rts,
        "buys": len(buys),
        "sells": len(sells),
        "fees": round(fees, 4),
        "pnl_recorded": round(pnl, 4),
        "net_after_fees": round(net, 4),
        "criteria": {
            "min_round_trips": MIN_ROUND_TRIPS,
            "min_net_after_fees": MIN_NET,
            "note": "Natural signals only; Intro ~0.9% taker assumed",
        },
        "reason": (
            "OK to ask Al for go-live"
            if ready
            else f"Need >= {MIN_ROUND_TRIPS} closed paper round-trips with net >= flat after fees (now rts={rts}, net={net:.4f})"
        ),
    }, indent=2))

if __name__ == "__main__":
    main()
