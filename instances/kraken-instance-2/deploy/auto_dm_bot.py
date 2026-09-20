#!/usr/bin/env python3
"""Dry-run-first X reply monitor for CruzBot Core.

Polls replies in the conversation rooted at TARGET_TWEET_ID and looks for the
whole word ``BOT`` (case-insensitive). By default it only reports intended
actions. Sending requires ``--live``; a loop additionally requires ``--loop``.

Credentials are read from X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, and
X_ACCESS_TOKEN_SECRET. As with the existing X tooling, the optional secrets
card at /home/box/sand-data/box-secrets.json is used only as a fallback.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TARGET_TWEET_ID = "2101219956553568625"
SEARCH_URL = "https://api.x.com/2/tweets/search/recent"
DM_URL_TEMPLATE = "https://api.x.com/2/dm_conversations/with/{user_id}/messages"
TWEET_URL = "https://api.x.com/2/tweets"
SECRETS_CARD = Path("/home/box/sand-data/box-secrets.json")
DEFAULT_DB = Path("/workspace/data/auto_dm_bot.sqlite")
BOT_WORD = re.compile(r"\bBOT\b", re.IGNORECASE)
DM_MESSAGE = "Thanks for checking out CruzBot Core! Here is the direct link to access the full Python framework and source code: https://whop.com/apexsignalsnow"
PUBLIC_REPLY = "Thanks for checking out CruzBot Core! Access the full Python framework here: https://whop.com/apexsignalsnow"

LOGGER = logging.getLogger("auto_dm_bot")


def load_card() -> dict[str, Any]:
    """Read the optional existing secrets card without printing secret values."""
    try:
        value = json.loads(SECRETS_CARD.read_text(encoding="utf-8"))
        card = value.get("card", {}) if isinstance(value, dict) else {}
        return card if isinstance(card, dict) else {}
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        return {}


def credentials() -> dict[str, str]:
    """Return OAuth1 credentials from env, falling back to the secrets card."""
    card = load_card()
    names = {
        "consumer_key": ("X_API_KEY", "X_CONSUMER_KEY"),
        "consumer_secret": ("X_API_SECRET", "X_CONSUMER_SECRET"),
        "access_token": ("X_ACCESS_TOKEN",),
        "access_token_secret": ("X_ACCESS_TOKEN_SECRET",),
    }
    result: dict[str, str] = {}
    for target, candidates in names.items():
        for name in candidates:
            value = os.environ.get(name) or card.get(name)
            if value:
                result[target] = str(value)
                break
    return result


def oauth_session(creds: dict[str, str]):
    """Build a requests session using OAuth1 user-context signing."""
    try:
        import requests
        from requests_oauthlib import OAuth1
    except ImportError as exc:  # pragma: no cover - depends on runtime install
        raise RuntimeError("install requests and requests-oauthlib to call the X API") from exc
    session = requests.Session()
    session.auth = OAuth1(
        creds["consumer_key"],
        creds["consumer_secret"],
        creds["access_token"],
        creds["access_token_secret"],
    )
    session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
    return session


def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE IF NOT EXISTS processed_users (
            user_id TEXT PRIMARY KEY,
            username TEXT,
            first_reply_id TEXT NOT NULL,
            processed_at TEXT NOT NULL,
            action TEXT NOT NULL
        )"""
    )
    connection.commit()
    return connection


def already_processed(connection: sqlite3.Connection, user_id: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM processed_users WHERE user_id = ? LIMIT 1", (user_id,)
    ).fetchone()
    return row is not None


def record_processed(
    connection: sqlite3.Connection,
    user_id: str,
    username: str | None,
    reply_id: str,
    action: str,
) -> None:
    connection.execute(
        """INSERT OR IGNORE INTO processed_users
           (user_id, username, first_reply_id, processed_at, action)
           VALUES (?, ?, ?, ?, ?)""",
        (user_id, username, reply_id, datetime.now(timezone.utc).isoformat(), action),
    )
    connection.commit()


def fetch_replies(session, max_results: int = 100) -> list[dict[str, Any]]:
    """Fetch recent replies in the target conversation and retain BOT matches."""
    params = {
        "query": f"conversation_id:{TARGET_TWEET_ID} -is:retweet",
        "max_results": max(10, min(int(max_results), 100)),
        "tweet.fields": "author_id,conversation_id,created_at,text,in_reply_to_user_id",
        "expansions": "author_id",
        "user.fields": "username,name",
    }
    response = session.get(SEARCH_URL, params=params, timeout=30)
    response.raise_for_status()
    payload = response.json()
    users = {
        str(user.get("id")): user
        for user in payload.get("includes", {}).get("users", [])
        if user.get("id")
    }
    matches: list[dict[str, Any]] = []
    for post in payload.get("data", []):
        text = str(post.get("text", ""))
        if not BOT_WORD.search(text):
            continue
        author_id = str(post.get("author_id") or "")
        if not author_id:
            continue
        user = users.get(author_id, {})
        matches.append(
            {
                "id": str(post.get("id") or ""),
                "text": text,
                "author_id": author_id,
                "username": user.get("username"),
                "name": user.get("name"),
                "created_at": post.get("created_at"),
            }
        )
    return matches


def send_dm(session, user_id: str) -> tuple[bool, str]:
    response = session.post(
        DM_URL_TEMPLATE.format(user_id=user_id),
        json={"text": DM_MESSAGE},
        timeout=30,
    )
    if response.ok:
        return True, "dm"
    return False, f"DM HTTP {response.status_code}"


def send_public_reply(session, post_id: str, username: str | None) -> tuple[bool, str]:
    mention = f"@{username} " if username else ""
    response = session.post(
        TWEET_URL,
        json={"text": mention + PUBLIC_REPLY, "reply": {"in_reply_to_tweet_id": post_id}},
        timeout=30,
    )
    if response.ok:
        return True, "public_reply"
    return False, f"public reply HTTP {response.status_code}"


def process_once(
    session,
    connection: sqlite3.Connection,
    *,
    live: bool,
    max_results: int = 100,
) -> dict[str, int]:
    matches = fetch_replies(session, max_results=max_results)
    seen_users: set[str] = set()
    counts = {"scanned_matches": len(matches), "skipped_processed": 0, "intended": 0, "sent": 0, "failed": 0}
    for reply in matches:
        user_id = reply["author_id"]
        if user_id in seen_users or already_processed(connection, user_id):
            counts["skipped_processed"] += 1
            continue
        seen_users.add(user_id)
        counts["intended"] += 1
        username = reply.get("username")
        label = f"@{username}" if username else user_id
        if not live:
            LOGGER.info("DRY-RUN: would DM %s for reply %s", label, reply["id"])
            continue
        try:
            dm_ok, result = send_dm(session, user_id)
        except Exception as exc:  # requests exceptions vary by installed version
            dm_ok, result = False, f"DM error: {exc}"
        if dm_ok:
            LOGGER.info("Sent DM to %s for reply %s", label, reply["id"])
            record_processed(connection, user_id, username, reply["id"], result)
            counts["sent"] += 1
            continue
        LOGGER.warning("%s; trying public reply to %s", result, label)
        try:
            public_ok, public_result = send_public_reply(session, reply["id"], username)
        except Exception as exc:  # requests exceptions vary by installed version
            public_ok, public_result = False, f"public reply error: {exc}"
        if public_ok:
            LOGGER.info("Sent public fallback reply to %s for reply %s", label, reply["id"])
            record_processed(connection, user_id, username, reply["id"], public_result)
            counts["sent"] += 1
        else:
            LOGGER.error("Could not contact %s: %s", label, public_result)
            counts["failed"] += 1
    return counts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="actually send DMs/fallback replies")
    parser.add_argument("--loop", action="store_true", help="poll repeatedly; interval is in seconds")
    parser.add_argument("--interval", type=float, default=60.0, help="poll interval for --loop (default: 60)")
    parser.add_argument("--once", action="store_true", help="run one poll (the default without --loop)")
    parser.add_argument("--max-results", type=int, default=100, help="X search page size, max 100")
    parser.add_argument("--db-path", type=Path, default=Path(os.environ.get("AUTO_DM_DB_PATH", DEFAULT_DB)))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    if args.interval <= 0:
        raise SystemExit("--interval must be positive")
    creds = credentials()
    required = {"consumer_key", "consumer_secret", "access_token", "access_token_secret"}
    missing = sorted(required - set(creds))
    if missing:
        LOGGER.error("Missing X OAuth1 credentials: %s", ", ".join(missing))
        return 2
    if not args.live:
        LOGGER.info("Dry-run mode: no DMs or public replies will be sent")
    try:
        session = oauth_session(creds)
    except RuntimeError as exc:
        LOGGER.error("%s", exc)
        return 2
    connection = init_db(args.db_path)
    try:
        while True:
            try:
                counts = process_once(session, connection, live=args.live, max_results=args.max_results)
                LOGGER.info("poll complete: %s", counts)
            except Exception as exc:
                LOGGER.error("poll failed: %s", exc)
                if args.loop:
                    time.sleep(args.interval)
                    continue
                return 1
            if not args.loop or args.once:
                return 0
            time.sleep(args.interval)
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
