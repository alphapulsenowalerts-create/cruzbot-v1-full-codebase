"""Grok sentiment filter: normalize responses, gate evaluate_entry without network."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trading_bot.models import Action, AgentObservation, IndicatorSnapshot
from trading_bot.strategy import (
    GROK_HOLD_BLOCK_CONFIDENCE,
    GrokSentimentFilter,
    grok_allows_long,
    normalize_grok_sentiment,
)
from trading_bot.strategy_volume_sweet_spot import VolumeSweetSpotEngine


def _sweet_obs(**extra_overrides) -> AgentObservation:
    extras = {
        "volume_ratio": 2.5,
        "retest_ok": True,
        "breakout_volume": 1000.0,
        "pullback_volume": 100.0,
        "swing_low": 2450.0,
        "delta_proxy": "close>open",
        "breakout_rvol": 2.5,
        "resistance": 2600.0,
        "entry_reason": "RVOL Breakout + Low-Volume VWAP Retest",
    }
    extras.update(extra_overrides)
    ind = IndicatorSnapshot(
        symbol="ETH-USD",
        close=2500.0,
        volume=100.0,
        vwap=2495.0,
        rsi=55.0,
        macd=1.0,
        macd_signal=0.5,
        macd_hist=0.4,
        ema_fast=2498.0,
        ema_slow=2490.0,
        atr=10.0,
        ema_cross="none",
        extras=extras,
    )
    return AgentObservation(symbol="ETH-USD", indicators=ind)


def test_normalize_chat_completions_envelope():
    raw = {
        "choices": [
            {
                "message": {
                    "content": '{"action": "BUY", "confidence": 0.82}',
                }
            }
        ]
    }
    out = normalize_grok_sentiment(raw)
    assert out == {"action": "BUY", "confidence": 0.82}


def test_normalize_already_shaped():
    assert normalize_grok_sentiment({"action": "sell", "confidence": 0.5}) == {
        "action": "SELL",
        "confidence": 0.5,
    }


def test_grok_allows_long_missing_filter():
    ok, detail = grok_allows_long(None)
    assert ok is True
    assert "missing" in detail


def test_grok_blocks_sell_and_confident_hold():
    filt = GrokSentimentFilter()
    filt.latest_sentiment = {"action": "SELL", "confidence": 0.3}
    ok, _ = grok_allows_long(filt)
    assert ok is False

    filt.latest_sentiment = {
        "action": "HOLD",
        "confidence": GROK_HOLD_BLOCK_CONFIDENCE + 0.1,
    }
    ok, _ = grok_allows_long(filt)
    assert ok is False

    filt.latest_sentiment = {"action": "HOLD", "confidence": 0.1}
    ok, _ = grok_allows_long(filt)
    assert ok is True

    filt.latest_sentiment = {"action": "BUY", "confidence": 0.9}
    ok, _ = grok_allows_long(filt)
    assert ok is True


@pytest.mark.asyncio
async def test_query_grok_fast_normalizes_and_uses_httpx_mock():
    filt = GrokSentimentFilter()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": '{"action":"SELL","confidence":0.7}'}}]
    }
    filt.client.post = AsyncMock(return_value=mock_resp)
    with patch("trading_bot.strategy.XAI_API_KEY", "test-key-not-real"):
        out = await filt.query_grok_fast({"symbols": {"ETH-USD": {"rsi": 50}}})
    assert out == {"action": "SELL", "confidence": 0.7}
    filt.client.post.assert_awaited_once()
    await filt.close()


def test_evaluate_entry_does_not_call_network():
    """evaluate_entry / reason must only read latest_sentiment — no HTTP."""
    filt = GrokSentimentFilter()
    filt.latest_sentiment = {"action": "BUY", "confidence": 0.9}
    filt.client.post = AsyncMock(side_effect=AssertionError("network called"))
    engine = VolumeSweetSpotEngine(sentiment_filter=filt)
    decision = engine.evaluate_entry(_sweet_obs())
    assert decision.action == Action.BUY
    filt.client.post.assert_not_called()


def test_evaluate_entry_gates_on_sell_sentiment():
    filt = GrokSentimentFilter()
    filt.latest_sentiment = {"action": "SELL", "confidence": 0.55}
    filt.client.post = AsyncMock(side_effect=AssertionError("network called"))
    engine = VolumeSweetSpotEngine(sentiment_filter=filt)
    decision = engine.evaluate_entry(_sweet_obs())
    assert decision.action == Action.HOLD
    assert "grok" in (decision.reasoning or "").lower()
    filt.client.post.assert_not_called()


def test_evaluate_entry_gates_on_confident_hold():
    filt = GrokSentimentFilter()
    filt.latest_sentiment = {"action": "HOLD", "confidence": 0.95}
    engine = VolumeSweetSpotEngine(sentiment_filter=filt)
    decision = engine.evaluate_entry(_sweet_obs())
    assert decision.action == Action.HOLD
    assert "HOLD" in (decision.reasoning or "")


def test_evaluate_entry_missing_filter_allows():
    engine = VolumeSweetSpotEngine(sentiment_filter=None)
    decision = engine.evaluate_entry(_sweet_obs())
    assert decision.action == Action.BUY
