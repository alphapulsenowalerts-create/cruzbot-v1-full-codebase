#!/usr/bin/env python3
"""Manual / cron smoke for the once-daily Telegram PnL digest.

Loads .env via Settings, builds live snapshot from paper book + ledger, sends Telegram.

Usage:
  .venv/bin/python scripts/send_daily_digest.py
  .venv/bin/python scripts/send_daily_digest.py --force   # ignore same-day dedupe
  .venv/bin/python scripts/send_daily_digest.py --dry-run # print only, no send
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trading_bot.config import PROJECT_ROOT, Settings  # noqa: E402
from trading_bot.daily_digest import (  # noqa: E402
    already_sent_for_date,
    build_digest_snapshot,
    dedupe_path_for,
    mark_sent_for_date,
    next_summary_datetime,
)
from trading_bot.notifier import build_notifier  # noqa: E402


async def _main() -> int:
    ap = argparse.ArgumentParser(description="Send CruzBot daily PnL digest now")
    ap.add_argument(
        "--force",
        action="store_true",
        help="Send even if data/last_daily_digest_date.txt already has today's CT date",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the message only; do not Telegram or write dedupe",
    )
    args = ap.parse_args()

    settings = Settings()
    book = Path(settings.paper_book_path)
    ledger = PROJECT_ROOT / "data" / "paper_ledger.db"
    sqlite_path = Path(settings.sqlite_path)
    tz = str(settings.summary_timezone or "America/Chicago")
    dedupe = dedupe_path_for(PROJECT_ROOT)

    snap = build_digest_snapshot(
        paper_book_path=book,
        ledger_path=ledger,
        sqlite_path=sqlite_path,
        tz_name=tz,
    )
    msg = snap.format_message()
    print(msg)
    print("---")
    nxt = next_summary_datetime(
        int(settings.summary_hour),
        int(settings.summary_minute),
        tz,
    )
    print(
        f"next_scheduled={nxt.strftime('%Y-%m-%d %H:%M')} {tz} "
        f"(SUMMARY_HOUR={settings.summary_hour} SUMMARY_MINUTE={settings.summary_minute}; "
        f"change .env + restart engine to reschedule)"
    )

    if args.dry_run:
        print("dry-run: not sending")
        return 0

    if not args.force and already_sent_for_date(dedupe, snap.date_key):
        print(f"skip: already sent for {snap.date_key} ({dedupe}) — use --force")
        return 0

    notifier = build_notifier(
        discord_webhook_url=settings.discord_webhook_url,
        telegram_bot_token=settings.telegram_bot_token,
        telegram_chat_id=settings.telegram_chat_id,
        quiet=True,
    )
    if not notifier.configured:
        print("error: Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)", file=sys.stderr)
        return 2

    await notifier.shit_lets_see_summary(
        starting_equity=snap.starting_equity,
        current_equity=snap.current_equity,
        realized_pnl_24h=snap.realized_pnl_24h,
        realized_pnl_pct=snap.realized_pnl_pct,
        trades_closed=snap.trades_closed,
        wins=snap.wins,
        losses=snap.losses,
        open_exposure=snap.open_exposure,
        message=msg,
    )
    mark_sent_for_date(dedupe, snap.date_key)
    print(f"sent ok; dedupe marked {snap.date_key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
