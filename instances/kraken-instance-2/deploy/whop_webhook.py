"""Lightweight Whop purchase webhook → Telegram sale alert (Apex Signals Now).

Env (never hardcode secrets):
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
  WHOP_WEBHOOK_PORT   (default 5001)
  WHOP_WEBHOOK_PATH   (default /whop/webhook)

Run:
  python deploy/whop_webhook.py
  # or: WHOP_WEBHOOK_PORT=5001 python -m deploy.whop_webhook  (if packaged)

Health: GET http://0.0.0.0:5001/health
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, Optional, Tuple

from aiohttp import ClientSession, web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("whop_webhook")

DEFAULT_PORT = 5001
DEFAULT_PATH = "/whop/webhook"
PRODUCT_NAME = "Apex Signals Now Trading Bot"
PLAN_67 = 67
PLAN_297 = 297


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _parse_amount(payload: Dict[str, Any]) -> Optional[float]:
    """Best-effort extract USD amount from Whop-like payloads."""
    candidates = []
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    for blob in (payload, data, data.get("payment") or {}, data.get("checkout") or {}):
        if not isinstance(blob, dict):
            continue
        for key in (
            "final_amount",
            "amount_after_fees",
            "total",
            "amount",
            "usd_amount",
            "price",
            "subtotal",
        ):
            if key in blob and blob[key] is not None:
                candidates.append(blob[key])
        # nested money objects
        for key in ("final_total", "total_amount", "payment_amount"):
            val = blob.get(key)
            if isinstance(val, dict):
                candidates.append(val.get("amount") or val.get("value"))
            elif val is not None:
                candidates.append(val)
    for raw in candidates:
        try:
            amt = float(raw)
            # Whop sometimes sends cents
            if amt >= 1000 and amt == int(amt):
                # ambiguous; prefer dollars if looks like 6700 cents for $67
                if abs(amt / 100 - PLAN_67) < 0.01 or abs(amt / 100 - PLAN_297) < 0.01:
                    return amt / 100.0
            return amt
        except (TypeError, ValueError):
            continue
    # plan id / name hints
    text = json.dumps(payload).lower()
    if "plan_bqj3zooqobkqy" in text or "297" in text:
        return float(PLAN_297)
    if "plan_pznsdwf4pj2kk" in text or "67" in text:
        return float(PLAN_67)
    return None


def _is_sale_event(payload: Dict[str, Any]) -> bool:
    event = str(
        payload.get("event")
        or payload.get("type")
        or payload.get("action")
        or ""
    ).lower()
    if event in {
        "payment.succeeded",
        "payment_succeeded",
        "membership.went_valid",
        "membership_went_valid",
        "purchase.completed",
        "order.created",
        "payment.created",
    }:
        return True
    # nested
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    status = str(data.get("status") or payload.get("status") or "").lower()
    if status in {"succeeded", "paid", "complete", "completed", "valid"}:
        return True
    if "payment.succeeded" in json.dumps(payload).lower():
        return True
    return False


def build_sale_message(payload: Dict[str, Any]) -> str:
    amount = _parse_amount(payload)
    if amount is not None and abs(amount - PLAN_297) < 0.51:
        price = f"${PLAN_297}"
    elif amount is not None and abs(amount - PLAN_67) < 0.51:
        price = f"${PLAN_67}"
    elif amount is not None and amount > 0:
        price = f"${amount:.0f}" if float(amount).is_integer() else f"${amount:.2f}"
    else:
        price = f"${PLAN_67}"
    return (
        f"🎉 NEW SALE ALERT! Customer purchased {PRODUCT_NAME} ({price}) on Whop!"
    )


async def send_telegram(text: str) -> Tuple[bool, str]:
    token = _env("TELEGRAM_BOT_TOKEN")
    chat_id = _env("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False, "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with ClientSession() as session:
        async with session.post(
            url,
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=20,
        ) as resp:
            body = await resp.text()
            if resp.status >= 300:
                return False, f"telegram HTTP {resp.status}: {body[:200]}"
            return True, "ok"


async def handle_health(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "apex-whop-webhook", "brand": PRODUCT_NAME})


async def handle_whop(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except Exception:
        raw = await request.text()
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"raw": raw}
    if not isinstance(payload, dict):
        payload = {"data": payload}

    logger.info("whop webhook keys=%s event=%s", list(payload.keys())[:12], payload.get("event") or payload.get("type"))

    if not _is_sale_event(payload):
        return web.json_response({"ok": True, "handled": False, "reason": "not_a_sale_event"})

    # Ignore local tunnel / monitor pings (never Telegram)
    if payload.get("health_check") or payload.get("_health"):
        return web.json_response({"ok": True, "handled": False, "reason": "health_check"})
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    prod = data.get("product") if isinstance(data.get("product"), dict) else {}
    title = str(prod.get("title") or data.get("title") or "").strip().lower()
    if title in {"ping", "health", "tunnel-check"}:
        return web.json_response({"ok": True, "handled": False, "reason": "health_ping"})

    msg = build_sale_message(payload)
    ok, detail = await send_telegram(msg)
    logger.info("sale alert sent=%s detail=%s msg=%s", ok, detail, msg)
    return web.json_response({"ok": ok, "handled": True, "telegram": detail, "message": msg})


def create_app(path: Optional[str] = None) -> web.Application:
    route = path or _env("WHOP_WEBHOOK_PATH", DEFAULT_PATH)
    if not route.startswith("/"):
        route = "/" + route
    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/", handle_health)
    app.router.add_post(route, handle_whop)
    # common aliases
    app.router.add_post("/webhook", handle_whop)
    app.router.add_post("/whop", handle_whop)
    return app


def main() -> None:
    port = int(_env("WHOP_WEBHOOK_PORT", str(DEFAULT_PORT)) or DEFAULT_PORT)
    path = _env("WHOP_WEBHOOK_PATH", DEFAULT_PATH)
    app = create_app(path)
    logger.info(
        "Starting Apex Signals Now Whop webhook on 0.0.0.0:%s path=%s",
        port,
        path,
    )
    web.run_app(app, host="0.0.0.0", port=port, print=None)


if __name__ == "__main__":
    main()
