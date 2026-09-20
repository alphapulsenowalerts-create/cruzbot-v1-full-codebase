#!/usr/bin/env python3
"""Generate a 7-day short-form technical tweet queue (<=280 chars each).

Writes /workspace/data/weekly_tweet_queue.json
Does not post — review then publish manually.
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path

STORE = "https://whop.com/apexsignalsnow"
PRODUCT = "https://whop.com/apexsignalsnow/cruzbot-core"
CHECKOUT_67 = "https://whop.com/checkout/plan_pznSDWf4pJ2Kk"
CHECKOUT_297 = "https://whop.com/checkout/plan_Bqj3ZooqoBkqY"
HASHTAGS = "#algotrading #python #asyncio #crypto"
DEFAULT_OUT = Path("/workspace/data/weekly_tweet_queue.json")


def build_posts(start: date | None = None) -> list[dict]:
    start = start or date.today()
    topics = [
        (1, "Why CVD divergence beats standard momentum indicators in 5m scalping",
         f"Day 1/7 — CVD > RSI on 5m scalps.\nGreen candle + negative CVD = absorption — we block longs.\nFramework → {PRODUCT}\n{HASHTAGS}"),
        (2, "Handling WebSocket feed disconnects without losing local state in Python",
         f"Day 2/7 — WS reconnects without amnesia.\nKeep candles/CVD in memory; backoff, resubscribe, rebuild only the open bar.\n{CHECKOUT_67}\n{HASHTAGS}"),
        (3, "Why we use SQLite WAL mode for low-latency bot trade logging",
         f"Day 3/7 — SQLite WAL for fills.\nReaders keep going while the bot appends. No Postgres tax on a single box.\n{PRODUCT}\n{HASHTAGS}"),
        (4, "Short-liquidation cascades: how to detect $50k+ sweeps in 60s",
         f"Day 4/7 — $50k+ short-liq sweeps / 60s.\nRolling perp liq notional arms POST_ONLY retest bids — gates, not FOMO entries.\n{CHECKOUT_67}\n{HASHTAGS}"),
        (5, "Setting up dynamic ATR trailing stops in Python",
         f"Day 5/7 — Dynamic ATR trails.\ntrail=close−k·ATR; only ratchet favorably. Pair with a daily DD circuit.\n{PRODUCT}\n{HASHTAGS}"),
        (6, "Unit testing WebSocket order managers with pytest mock feeds",
         f"Day 6/7 — pytest mock WS order managers.\nRecorded frames, assert post-only + reconnects. 170+ tests in the pack.\n{CHECKOUT_67}\n{HASHTAGS}"),
        (7, "Full architecture breakdown of CruzBot Core",
         f"Day 7/7 — CruzBot Core architecture.\nasyncio→WS/lead-lag→CVD/liq→5m retest→POST_ONLY→SQLite→Telegram C2.\n$67 {CHECKOUT_67}\n$297 {CHECKOUT_297}\n{HASHTAGS}"),
    ]
    out = []
    for i, (day, topic, text) in enumerate(topics):
        if len(text) > 280:
            raise SystemExit(f"day {day} too long: {len(text)}")
        out.append({
            "day": day,
            "date": (start + timedelta(days=i)).isoformat(),
            "topic": topic,
            "text": text,
            "hashtags": HASHTAGS,
            "links": {
                "store": STORE,
                "product": PRODUCT,
                "checkout_67": CHECKOUT_67,
                "checkout_297": CHECKOUT_297,
            },
            "status": "queued",
            "char_count": len(text),
        })
    return out


def main() -> None:
    out_path = Path(os.environ.get("WEEKLY_TWEET_QUEUE_PATH", str(DEFAULT_OUT)))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    posts = build_posts()
    payload = {
        "generated_for": "@AlphaPulseNow",
        "product": "Apex Signals Now Trading Bot / CruzBot Core framework",
        "note": "Review before posting. Not financial advice. All posts <=280 chars.",
        "posts": posts,
    }
    out_path.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"Wrote {len(posts)} posts → {out_path}")
    for p in posts:
        print(f"  Day {p['day']}: {p['char_count']} chars — {p['topic'][:60]}")


if __name__ == "__main__":
    main()
