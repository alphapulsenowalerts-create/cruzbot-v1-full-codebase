#!/usr/bin/env python3
"""One natural paper cycle; log SCAN / FILL events to paper ledger. No forced trades."""
from __future__ import annotations
import asyncio, sys, re
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from trading_bot.config import Settings
from scripts.paper_ledger import log_event

async def main():
    s = Settings()
    assert s.paper_trading_mode, "Refusing: paper mode must be on"
    # Run main.py --once as subprocess to reuse full stack
    import subprocess, os
    env = os.environ.copy()
    env["PAPER_TRADING_MODE"] = "true"
    env["BROKER"] = "coinbase"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "--once"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    # Parse decisions / fills from logs
    holds = len(re.findall(r"PREFILTER_SKIP|action.: .HOLD|\"action\": \"HOLD\"", out))
    buys = re.findall(r"SUBMIT BUY (\S+) qty=([0-9.]+).*limit=([0-9.]+)", out)
    sells = re.findall(r"SUBMIT SELL (\S+) qty=([0-9.]+).*limit=([0-9.]+)", out)
    fills = re.findall(r"(FILLED|paper simulated fill).*", out, re.I)
    log_event(
        "SCAN",
        note=f"exit={proc.returncode} holds~{holds} buy_submits={len(buys)} sell_submits={len(sells)} fill_lines={len(fills)}",
        raw={"returncode": proc.returncode, "tail": out[-4000:]},
    )
    taker = float(env.get("TAKER_FEE_RATE", "0.009"))
    for sym, qty, limit in buys:
        notional = float(qty) * float(limit)
        fee = notional * taker
        log_event("BUY", symbol=sym, side="BUY", qty=float(qty), price=float(limit), notional=notional, fee=fee, note="natural submit")
    for sym, qty, limit in sells:
        notional = float(qty) * float(limit)
        fee = notional * taker
        log_event("SELL", symbol=sym, side="SELL", qty=float(qty), price=float(limit), notional=notional, fee=fee, note="natural submit")
    print(out[-2500:])
    print("LEDGER_LOGGED", {"holds": holds, "buys": len(buys), "sells": len(sells), "rc": proc.returncode})

asyncio.run(main())
