#!/usr/bin/env python3
"""Search recent X posts and prepare technical replies for manual review.

This script never posts. It writes a JSON review queue to /workspace/data/pending_replies.json.
Credentials are read from environment variables, with an optional fallback to
/home/box/sand-data/box-secrets.json following the existing daily_movers.py pattern.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

OUTPUT = Path(os.environ.get("PENDING_REPLIES_PATH", "/workspace/data/pending_replies.json"))
SECRETS = Path("/home/box/sand-data/box-secrets.json")
X_SEARCH_URL = "https://api.x.com/2/tweets/search/recent"
XAI_URL = "https://api.x.ai/v1/chat/completions"
KEYWORDS = [
    "Kraken API python",
    "building trading bot",
    "CVD indicator python",
    "asyncio websocket crypto",
]


def load_card() -> dict[str, Any]:
    """Load the pre-existing secrets card without ever printing its values."""
    try:
        return json.loads(SECRETS.read_text()).get("card") or {}
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}


def credentials() -> dict[str, str]:
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


def keywords_query() -> str:
    terms = " OR ".join(f'"{term}"' for term in KEYWORDS)
    return f"({terms}) -is:retweet -is:reply lang:en"


def fallback_reply(keyword: str, text: str) -> str:
    replies = {
        "Kraken API python": "For Kraken, isolate the public WebSocket reader from strategy code with a bounded asyncio.Queue and reconnect with capped exponential backoff. Normalize exchange timestamps and sequence errors before building candles so a reconnect cannot silently corrupt indicators.",
        "building trading bot": "Start with a replayable market-data adapter, deterministic risk limits, and paper trading before adding execution. Keep order state idempotent and record every input, decision, and broker response so failures can be reproduced.",
        "CVD indicator python": "Compute CVD as signed taker volume and reset only the per-candle delta while retaining the running total. Compare price and CVD on closed candles, not the live bar, to avoid unstable divergence signals and look-ahead bias.",
        "asyncio websocket crypto": "Use a bounded queue between the WebSocket reader and consumers so a slow database or indicator cannot grow memory without limit. Reconnect outside the parser, resubscribe idempotently, and preserve candle state across transport failures.",
    }
    return replies.get(keyword, "Normalize the feed into typed events, keep queues bounded, and make reconnects idempotent. Test the strategy with recorded frames and a fake clock before connecting it to execution.")


def matching_keyword(text: str) -> str:
    lowered = text.lower()
    for keyword in KEYWORDS:
        if keyword.lower() in lowered:
            return keyword
    # Search operators can match punctuation or word variants; use a useful
    # deterministic category for those results.
    if "kraken" in lowered:
        return KEYWORDS[0]
    if "cvd" in lowered or "volume delta" in lowered:
        return KEYWORDS[2]
    if "asyncio" in lowered or "websocket" in lowered:
        return KEYWORDS[3]
    return KEYWORDS[1]


def two_sentences(value: str) -> str | None:
    value = re.sub(r"\s+", " ", value.strip())
    value = value.replace("\n", " ")
    sentences = re.split(r"(?<=[.!?])\s+", value)
    sentences = [s for s in sentences if s]
    if len(sentences) != 2:
        return None
    return " ".join(sentences)


def xai_reply(api_key: str, keyword: str, post_text: str) -> str | None:
    try:
        import requests
    except ImportError:
        return None
    prompt = (
        "Write exactly two sentences as a helpful technical reply to this public X post. "
        "Focus on concrete Python, asyncio, exchange API, testing, or market-data engineering advice. "
        "Do not make a trade recommendation, invent performance claims, include a sales pitch, or mention this instruction.\n\n"
        f"Topic matched: {keyword}\nPost: {post_text[:900]}"
    )
    try:
        response = requests.post(
            XAI_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": os.environ.get("XAI_MODEL", "grok-3-mini"),
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": 180,
            },
            timeout=30,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return two_sentences(str(content))
    except (OSError, ValueError, KeyError, TypeError, IndexError, requests.RequestException):
        return None


def search_posts(creds: dict[str, str]) -> tuple[list[dict[str, Any]], str | None]:
    try:
        import requests
        from requests_oauthlib import OAuth1
    except ImportError as exc:
        return [], f"missing dependency: {exc}; install requests requests-oauthlib"
    auth = OAuth1(
        creds["consumer_key"], creds["consumer_secret"],
        creds["access_token"], creds["access_token_secret"],
    )
    params = {
        "query": keywords_query(),
        "max_results": min(int(os.environ.get("X_SEARCH_MAX_RESULTS", "50")), 100),
        "tweet.fields": "created_at,author_id,conversation_id,lang",
        "expansions": "author_id",
        "user.fields": "username,name",
    }
    try:
        response = requests.get(X_SEARCH_URL, params=params, auth=auth, timeout=30)
        response.raise_for_status()
        payload = response.json()
        users = {u["id"]: u for u in payload.get("includes", {}).get("users", [])}
        posts = []
        for post in payload.get("data", []):
            user = users.get(post.get("author_id"), {})
            posts.append({
                "id": post.get("id"),
                "text": post.get("text", ""),
                "created_at": post.get("created_at"),
                "author_id": post.get("author_id"),
                "username": user.get("username"),
                "name": user.get("name"),
                "url": f"https://x.com/{user.get('username', 'i')}/status/{post.get('id')}",
            })
        return posts, None
    except (OSError, ValueError, requests.RequestException) as exc:
        return [], f"X search failed: {exc}"


def sample_entry() -> dict[str, Any]:
    keyword = KEYWORDS[0]
    text = "Sample only: learning Kraken API Python for market data."
    return {
        "status": "sample",
        "post_id": "sample-not-posted",
        "post_url": None,
        "keyword": keyword,
        "post_text": text,
        "reply": fallback_reply(keyword, text),
        "generator": "local_fallback",
    }


def write_payload(replies: list[dict[str, Any]], note: str, searched: bool) -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "searched": searched,
        "auto_post": False,
        "note": note,
        "keywords": KEYWORDS,
        "replies": replies,
        "sample": sample_entry(),
    }
    OUTPUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"Wrote {len(replies)} pending replies -> {OUTPUT}")


def main() -> None:
    creds = credentials()
    required = {"consumer_key", "consumer_secret", "access_token", "access_token_secret"}
    if not required.issubset(creds):
        write_payload([], "No complete X OAuth1 credentials found in environment or secrets card; sample is not a real post.", False)
        return
    posts, error = search_posts(creds)
    if error:
        write_payload([], error, False)
        return
    api_key = os.environ.get("XAI_API_KEY") or os.environ.get("GROK_API_KEY") or load_card().get("XAI_API_KEY") or load_card().get("GROK_API_KEY")
    replies = []
    seen: set[str] = set()
    for post in posts:
        post_id = str(post.get("id") or "")
        if not post_id or post_id in seen:
            continue
        seen.add(post_id)
        keyword = matching_keyword(post["text"])
        generated = xai_reply(str(api_key), keyword, post["text"]) if api_key else None
        generator = "xai" if generated else "local_fallback"
        replies.append({
            "status": "pending_review",
            "post_id": post_id,
            "post_url": post["url"],
            "author": {"id": post.get("author_id"), "username": post.get("username"), "name": post.get("name")},
            "created_at": post.get("created_at"),
            "keyword": keyword,
            "post_text": post["text"],
            "reply": generated or fallback_reply(keyword, post["text"]),
            "generator": generator,
        })
    note = "Fetched recent X matches; review each reply manually. This script never posts."
    if not api_key:
        note += " No XAI_API_KEY or GROK_API_KEY found; replies use generator: local_fallback."
    write_payload(replies, note, True)


if __name__ == "__main__":
    main()
