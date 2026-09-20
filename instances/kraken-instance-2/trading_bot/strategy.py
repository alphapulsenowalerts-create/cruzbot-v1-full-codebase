"""Grok sentiment filter — non-blocking background refresh; <1ms lookup at entry."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

XAI_API_KEY = os.getenv("XAI_API_KEY")

# HOLD above this confidence blocks longs (SELL always blocks).
GROK_HOLD_BLOCK_CONFIDENCE = 0.6


def normalize_grok_sentiment(raw: Any) -> dict:
    """Force {action, confidence} from chat-completions JSON or already-normalized dict."""
    default = {"action": "HOLD", "confidence": 0.0}
    if not isinstance(raw, dict):
        return dict(default)

    # Already in the expected shape
    if "action" in raw and "choices" not in raw:
        try:
            action = str(raw.get("action") or "HOLD").upper()
            if action not in ("BUY", "SELL", "HOLD"):
                action = "HOLD"
            conf = float(raw.get("confidence") or 0.0)
            conf = max(0.0, min(1.0, conf))
            return {"action": action, "confidence": conf}
        except (TypeError, ValueError):
            return dict(default)

    # OpenAI/xAI chat completions envelope
    try:
        content = raw["choices"][0]["message"]["content"]
        if isinstance(content, str):
            text = content.strip()
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                text = text[start : end + 1]
            parsed = json.loads(text)
        elif isinstance(content, dict):
            parsed = content
        else:
            return dict(default)
        return normalize_grok_sentiment(parsed)
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
        return dict(default)


class GrokSentimentFilter:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=3.0)
        self.latest_sentiment = {"action": "HOLD", "confidence": 0.0}

    async def query_grok_fast(self, payload_data: dict) -> dict:
        if not XAI_API_KEY:
            logger.warning("XAI_API_KEY missing. Defaulting to HOLD.")
            return {"action": "HOLD", "confidence": 0.0}

        url = "https://api.x.ai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {XAI_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "grok-2-latest",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        'Return raw JSON only: '
                        '{"action": "BUY"|"SELL"|"HOLD", "confidence": 0.0-1.0}'
                    ),
                },
                {"role": "user", "content": f"Indicators: {payload_data}"},
            ],
            "temperature": 0.1,
            "max_tokens": 50,
            "response_format": {"type": "json_object"},
        }

        try:
            response = await self.client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            # Normalize chat-completions envelope → {action, confidence}
            return normalize_grok_sentiment(response.json())
        except Exception as e:
            logger.error(f"Grok API request failed: {e}. Defaulting to HOLD.")
            return {"action": "HOLD", "confidence": 0.0}

    async def background_update_loop(self, get_market_data_func, interval_seconds=900):
        while True:
            try:
                data = await get_market_data_func()
                self.latest_sentiment = await self.query_grok_fast(data)
                logger.info(f"Updated Grok Sentiment: {self.latest_sentiment}")
            except Exception as e:
                logger.error(f"Error in Grok background loop: {e}")
            await asyncio.sleep(interval_seconds)

    async def close(self):
        await self.client.aclose()


def grok_allows_long(
    sentiment_filter: Optional[GrokSentimentFilter],
    *,
    hold_block_confidence: float = GROK_HOLD_BLOCK_CONFIDENCE,
) -> tuple[bool, str]:
    """
    <1ms in-memory gate. Missing filter → allow (do not brick entries).
    Blocks long when action is SELL, or HOLD with confidence above threshold.
    """
    if sentiment_filter is None:
        return True, "grok: missing filter → allow"
    sent = getattr(sentiment_filter, "latest_sentiment", None)
    if not isinstance(sent, dict):
        return True, "grok: missing sentiment → allow"
    action = str(sent.get("action") or "HOLD").upper()
    try:
        conf = float(sent.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    # Gate longs: block on Grok SELL, or HOLD with confidence > threshold (missing → allow).
    if action == "SELL":
        return False, f"grok: SELL confidence={conf:.2f} — block long"
    if action == "HOLD" and conf > hold_block_confidence:
        return (
            False,
            f"grok: HOLD confidence={conf:.2f} > {hold_block_confidence:.2f} — block long",
        )
    return True, f"grok: {action} confidence={conf:.2f} — allow"

# ---------------------------------------------------------------------------
# Phase 2: Nightly market regime (ATR + ADX) — load / hot-reload / gate helpers
# No Grok dependency. Labels: TRENDING | RANGING | HIGH_VOLATILITY
# ---------------------------------------------------------------------------

REGIME_TRENDING = "TRENDING"
REGIME_RANGING = "RANGING"
REGIME_HIGH_VOLATILITY = "HIGH_VOLATILITY"
VALID_MARKET_REGIMES = (REGIME_TRENDING, REGIME_RANGING, REGIME_HIGH_VOLATILITY)
DEFAULT_MARKET_REGIME = REGIME_RANGING


def normalize_market_regime(raw: Any, *, default: str = DEFAULT_MARKET_REGIME) -> str:
    """Map free-form regime strings to TRENDING / RANGING / HIGH_VOLATILITY."""
    if raw is None:
        return default
    s = str(raw).strip().upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "TRENDING": REGIME_TRENDING,
        "TREND": REGIME_TRENDING,
        "RANGING": REGIME_RANGING,
        "RANGE": REGIME_RANGING,
        "LOW_VOL": REGIME_RANGING,
        "MID_VOL": REGIME_RANGING,
        "HIGH_VOLATILITY": REGIME_HIGH_VOLATILITY,
        "HIGH_VOL": REGIME_HIGH_VOLATILITY,
        "HIGHVOL": REGIME_HIGH_VOLATILITY,
        "VOLATILE": REGIME_HIGH_VOLATILITY,
    }
    return aliases.get(s, default if s not in VALID_MARKET_REGIMES else s)


def regime_trade_cap_mult(regime: Any) -> float:
    """HIGH_VOLATILITY → 0.5 effective MAX_TRADE_CAP; otherwise 1.0."""
    if normalize_market_regime(regime) == REGIME_HIGH_VOLATILITY:
        return 0.5
    return 1.0


def market_regime_blocks_retest(regime: Any, *, enabled: bool = True) -> tuple[bool, str]:
    """
    Strict gate: when enabled and HIGH_VOLATILITY, block standard 5m mean-reversion
    retest BUY entries. Missing/unknown → treat as RANGING (allow).
    """
    if not enabled:
        return False, "market_regime: gate disabled"
    label = normalize_market_regime(regime)
    if label == REGIME_HIGH_VOLATILITY:
        return (
            True,
            "market_regime: HIGH_VOLATILITY — block 5m retest BUY "
            f"(cap_mult={regime_trade_cap_mult(label):.1f})",
        )
    return False, f"market_regime: {label} — allow retest (cap_mult=1.0)"


def load_market_regime_from_active_params(
    path: Any,
    *,
    default: str = DEFAULT_MARKET_REGIME,
) -> tuple[str, Optional[float], dict]:
    """
    Read market_regime (+ optional features) from active_params JSON.
    Returns (regime, mtime, raw_slice). Fail-soft → default.
    """
    try:
        p = Path(path) if path is not None else None
        if p is None or not p.exists():
            return default, None, {}
        mtime = float(p.stat().st_mtime)
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return default, mtime, {}
        regime = normalize_market_regime(raw.get("market_regime"), default=default)
        slice_ = {
            "market_regime": regime,
            "regime_updated_at": raw.get("regime_updated_at"),
            "regime_features": raw.get("regime_features"),
            "regime_trade_cap_mult": raw.get("regime_trade_cap_mult"),
        }
        return regime, mtime, slice_
    except Exception as exc:
        logger.warning("load_market_regime failed: %s", exc)
        return default, None, {}


class MarketRegimeCache:
    """In-memory regime with mtime hot-reload (<1ms lookup; file IO only on change)."""

    def __init__(
        self,
        path: Optional[Any] = None,
        *,
        initial: Optional[str] = None,
        default: str = DEFAULT_MARKET_REGIME,
    ) -> None:
        self.path = Path(path) if path else None
        self.default = default
        self.market_regime = normalize_market_regime(initial, default=default)
        self._mtime: Optional[float] = None
        self.regime_features: dict = {}
        self.regime_updated_at: Optional[str] = None
        if self.path is not None:
            self.reload(force=True)

    def reload(self, *, force: bool = False) -> str:
        if self.path is None:
            return self.market_regime
        try:
            if not self.path.exists():
                return self.market_regime
            mtime = float(self.path.stat().st_mtime)
            if not force and self._mtime is not None and mtime <= self._mtime:
                return self.market_regime
            regime, mt, slice_ = load_market_regime_from_active_params(
                self.path, default=self.default
            )
            self.market_regime = regime
            self._mtime = mt
            feats = slice_.get("regime_features")
            self.regime_features = feats if isinstance(feats, dict) else {}
            upd = slice_.get("regime_updated_at")
            self.regime_updated_at = str(upd) if upd else None
            logger.info(
                "MARKET_REGIME hot-reload → %s (mtime=%s)",
                self.market_regime,
                self._mtime,
            )
        except Exception as exc:
            logger.warning("MARKET_REGIME reload failed: %s", exc)
        return self.market_regime

    def maybe_reload(self) -> str:
        """Cheap path: only stat when path set; skip if mtime unchanged."""
        return self.reload(force=False)

    def set_regime(self, regime: Any, *, features: Optional[dict] = None) -> None:
        self.market_regime = normalize_market_regime(regime, default=self.default)
        if features is not None:
            self.regime_features = dict(features)

    @property
    def trade_cap_mult(self) -> float:
        return regime_trade_cap_mult(self.market_regime)
