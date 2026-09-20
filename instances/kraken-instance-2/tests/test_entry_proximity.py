"""Tests for entry proximity scoring + Telegram progress bar on /status."""

from __future__ import annotations

from types import SimpleNamespace

from trading_bot.models import IndicatorSnapshot, AgentObservation
from trading_bot.strategy import GrokSentimentFilter
from trading_bot.strategy_volume_sweet_spot import VolumeSweetSpotEngine
from trading_bot.telegram_commands import format_status_reply
from trading_bot.utils.entry_proximity import (
    best_entry_proximity,
    get_entry_proximity,
    make_progress_bar,
)


def test_make_progress_bar_visual():
    assert make_progress_bar(0) == "░░░░░░░░░░"
    assert make_progress_bar(100) == "██████████"
    assert make_progress_bar(50) == "█████░░░░░"
    assert make_progress_bar(80, length=10) == "████████░░"
    assert make_progress_bar(25, length=4) == "█░░░"
    # Clamp
    assert make_progress_bar(-10) == "░░░░░░░░░░"
    assert make_progress_bar(150) == "██████████"
    assert "█" in make_progress_bar(70) and "░" in make_progress_bar(70)


def test_get_entry_proximity_missing_grok_is_hold_zero():
    prox = get_entry_proximity(
        close=100.0,
        vwap=100.05,
        extras={"retest_ok": True, "breakout_volume": 1000, "pullback_volume": 200},
        sentiment=None,
    )
    assert prox["direction"] in ("LONG", "WAIT")
    assert prox["components"]["grok"] == 0.0
    assert 0.0 <= prox["score"] <= 100.0
    assert "SHORT" not in prox["direction"]


def test_get_entry_proximity_grok_buy_bonus():
    base = get_entry_proximity(
        close=100.0,
        vwap=100.0,
        extras={"retest_ok": True, "breakout_volume": 1000, "pullback_volume": 200},
        sentiment={"action": "HOLD", "confidence": 0.9},
    )
    boosted = get_entry_proximity(
        close=100.0,
        vwap=100.0,
        extras={"retest_ok": True, "breakout_volume": 1000, "pullback_volume": 200},
        sentiment={"action": "BUY", "confidence": 1.0},
    )
    assert boosted["components"]["grok"] == 20.0
    assert boosted["score"] > base["score"]
    assert boosted["direction"] == "LONG"


def test_get_entry_proximity_cvd_and_liq_when_present():
    weak = get_entry_proximity(close=100.0, vwap=100.0, extras={})
    strong = get_entry_proximity(
        close=100.0,
        vwap=100.0,
        extras={
            "retest_ok": True,
            "breakout_volume": 800,
            "pullback_volume": 100,
            "phase1_allow": True,
            "cvd_period_delta": -50.0,
            "short_liq_spike": True,
        },
        sentiment={"action": "BUY", "confidence": 0.8},
        symbol="BTC-USD",
    )
    assert strong["components"]["cvd"] == 10.0
    assert strong["components"]["liq"] == 5.0
    assert strong["score"] > weak["score"]
    assert strong["direction"] == "LONG"
    assert "BTC-USD" in strong["target_setup"]
    assert strong["bar"] == make_progress_bar(strong["score"])


def test_never_scores_short():
    prox = get_entry_proximity(
        close=90.0,
        vwap=100.0,
        extras={"on_breakout_spike": True},
        sentiment={"action": "SELL", "confidence": 1.0},
    )
    assert prox["direction"] == "WAIT"
    assert prox["direction"] != "SHORT"


def test_volume_sweet_spot_engine_method():
    eng = VolumeSweetSpotEngine(sentiment_filter=None)
    obs = AgentObservation(
        symbol="ETH-USD",
        indicators=IndicatorSnapshot(
            symbol="ETH-USD",
            close=2500.0,
            volume=10.0,
            vwap=2500.0,
            extras={
                "retest_ok": True,
                "breakout_volume": 500,
                "pullback_volume": 100,
                "volume_ratio": 2.5,
            },
        ),
    )
    prox = eng.get_entry_proximity(obs)
    assert "score" in prox
    assert prox["direction"] in ("LONG", "WAIT")
    assert prox["components"]["grok"] == 0.0  # missing filter → 0 pts


def test_engine_with_idle_grok():
    sf = GrokSentimentFilter()
    sf.latest_sentiment = {"action": "HOLD", "confidence": 0.0}
    eng = VolumeSweetSpotEngine(sentiment_filter=sf)
    obs = AgentObservation(
        symbol="SOL-USD",
        indicators=IndicatorSnapshot(
            symbol="SOL-USD",
            close=150.0,
            volume=1.0,
            vwap=149.9,
            extras={"retest_ok": True, "breakout_volume": 200, "pullback_volume": 50},
        ),
    )
    prox = eng.get_entry_proximity(obs)
    assert prox["components"]["grok"] == 0.0


def test_format_status_reply_includes_proximity_bar():
    prox = get_entry_proximity(
        close=100.0,
        vwap=100.0,
        extras={
            "retest_ok": True,
            "breakout_volume": 1000,
            "pullback_volume": 200,
            "phase1_allow": True,
            "short_liq_spike": True,
        },
        sentiment={"action": "BUY", "confidence": 1.0},
        symbol="BTC-USD",
    )
    text = format_status_reply(
        paper_cash=1600.0,
        paper_equity=1600.0,
        positions=[],
        paused=False,
        strategy_mode="volume_sweet_spot",
        last_tick_age_seconds=1.2,
        paper=True,
        symbols=["BTC-USD", "ETH-USD"],
        max_notional_per_trade=100.0,
        max_total_exposure=1000.0,
        target_setup=prox["direction"],
        entry_proximity=prox,
    )
    assert "Apex Signals Now PAPER status" in text
    assert "CruzBot" not in text
    assert "pid=" not in text
    assert "Target Setup: LONG" in text or "Target Setup: WAIT" in text
    # Direction only on Target Setup line (no symbol/setup prose)
    setup_line = [ln for ln in text.splitlines() if ln.startswith("Target Setup:")][0]
    assert setup_line in ("Target Setup: LONG", "Target Setup: WAIT")
    prox_line = [ln for ln in text.splitlines() if ln.startswith("Entry Proximity:")][0]
    assert prox_line.startswith("Entry Proximity: [")
    assert "%" in prox_line
    # No redundant LONG/WAIT after the percent
    assert not prox_line.rstrip().endswith("LONG")
    assert not prox_line.rstrip().endswith("WAIT")
    assert "caps=$100/trade $1000 exposure" in text
    assert "focus=BTC-USD @ $100.00" in text
    assert text.splitlines()[-1] == "symbols=BTC-USD,ETH-USD"


def test_long_threshold_constant():
    from trading_bot.utils.entry_proximity import LONG_THRESHOLD

    assert LONG_THRESHOLD == 60.0
    just_under = get_entry_proximity(
        close=100.0,
        vwap=100.0,
        extras={"retest_ok": True, "breakout_volume": 1000, "pullback_volume": 200},
        sentiment={"action": "HOLD", "confidence": 0.0},
        long_threshold=LONG_THRESHOLD,
    )
    # 40 VWAP + 25 vol = 65 → LONG at default threshold
    assert just_under["score"] >= LONG_THRESHOLD
    assert just_under["direction"] == "LONG"
    waiting = get_entry_proximity(close=100.0, vwap=110.0, extras={})
    assert waiting["score"] < LONG_THRESHOLD
    assert waiting["direction"] == "WAIT"


def test_best_entry_proximity_picks_highest():
    weak = SimpleNamespace(
        symbol="AAA-USD",
        indicators=SimpleNamespace(close=100.0, vwap=110.0, extras={}),
    )
    strong = SimpleNamespace(
        symbol="BBB-USD",
        indicators=SimpleNamespace(
            close=100.0,
            vwap=100.0,
            extras={
                "retest_ok": True,
                "breakout_volume": 1000,
                "pullback_volume": 100,
                "phase1_allow": True,
                "short_liq_spike": True,
            },
        ),
    )
    best = best_entry_proximity(
        [weak, strong],
        sentiment={"action": "BUY", "confidence": 1.0},
    )
    assert best["symbol"] == "BBB-USD"
    assert best["score"] >= 60.0
