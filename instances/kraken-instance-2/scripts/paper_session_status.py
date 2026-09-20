#!/usr/bin/env python3
"""Print paper session status + ledger summary."""
from __future__ import annotations
import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trading_bot.config import Settings, HARD_SYMBOL_ALLOWLIST
from trading_bot.brokers.coinbase import CoinbaseBroker
from scripts.paper_ledger import summarize

async def main():
    s = Settings()
    b = CoinbaseBroker(s)
    await b.connect()
    out = {
        "paper_mode": s.paper_trading_mode,
        "allowlist": sorted(HARD_SYMBOL_ALLOWLIST),
        "caps": {"per_trade": s.max_notional_per_trade_usd, "total": getattr(s, 'max_total_exposure_usd', None)},
        "cash": round(float(b._paper_cash), 2),
        "equity": round(float(b._paper_equity), 2),
        "slippage_bps": s.slippage_bps,
        "quiet_notifier": s.quiet_notifier,
        "macro_pause_enabled": s.macro_pause_enabled,
        "macro_calendar_configured": bool(s.macro_calendar_url),
        "discord_configured": bool(s.discord_webhook_url),
        "telegram_configured": bool(s.telegram_bot_token and s.telegram_chat_id),
        "fees_assumed": {"taker": 0.009, "maker": 0.005, "note": "Intro tier while spot volume=0"},
        "ledger": summarize(),
    }
    print(json.dumps(out, indent=2))

asyncio.run(main())
