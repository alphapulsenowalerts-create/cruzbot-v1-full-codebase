"""Entry-proximity progress bar + non-blocking proximity scoring (no network)."""

from __future__ import annotations

from types import SimpleNamespace

from trading_bot.strategy_volume_sweet_spot import VolumeSweetSpotEngine
from trading_bot.telegram_commands import format_status_reply
from trading_bot.utils.indicators import make_progress_bar


def test_make_progress_bar_edges():
    assert make_progress_bar(0) == "[░░░░░░░░░░] 0.0%"
    assert make_progress_bar(50) == "[█████░░░░░] 50.0%"
    assert make_progress_bar(100) == "[██████████] 100.0%"
    # clamp
    assert make_progress_bar(-10) == "[░░░░░░░░░░] 0.0%"
    assert make_progress_bar(150) == "[██████████] 100.0%"


def test_proximity_high_when_gates_aligned():
    eng = VolumeSweetSpotEngine(
        rvol_breakout_mult=2.0,
        l2_imbalance_min_ratio=1.2,
        liq_sweep_short_usd=50_000.0,
    )
    prox = eng.get_entry_proximity(
        snapshot={
            "symbol": "BTC-USD",
            "close": 100_000.0,
            "vwap": 99_900.0,
            "volume_ratio": 2.4,
            "retest_ok": True,
            "breakout_volume": 1000.0,
            "pullback_volume": 200.0,
            "l2_imbalance_ratio": 1.5,
            "adx": 32.0,
            "chop": 40.0,
            "setup_bar_open": 99_800.0,
            "setup_bar_close": 100_000.0,
            "cvd_snapshot": {"cvd_5m": 80_000.0, "ready": True},
            "liq_snapshot": {"short_liq_1m_usd": 60_000.0, "ready": True},
        }
    )
    assert prox["direction"] == "LONG"
    assert 70.0 <= float(prox["score"]) <= 99.9
    parts = prox["parts"]
    assert parts["volume"] >= 30.0
    assert parts["cvd_liq"] >= 15.0


def test_proximity_green_negative_cvd_lowers_score():
    eng = VolumeSweetSpotEngine(rvol_breakout_mult=2.0, liq_sweep_short_usd=50_000.0)
    base = {
        "close": 100.0,
        "vwap": 99.8,
        "volume_ratio": 2.0,
        "retest_ok": True,
        "l2_imbalance_ratio": 1.4,
        "adx": 28.0,
        "chop": 45.0,
        "setup_bar_open": 99.0,
        "setup_bar_close": 100.5,  # green
        "liq_snapshot": {"short_liq_1m_usd": 50_000.0, "ready": True},
    }
    good = eng.get_entry_proximity(
        snapshot={**base, "cvd_snapshot": {"cvd_5m": 50_000.0, "ready": True}}
    )
    bad = eng.get_entry_proximity(
        snapshot={**base, "cvd_snapshot": {"cvd_5m": -50_000.0, "ready": True}}
    )
    assert float(bad["score"]) < float(good["score"])
    assert float(bad["parts"]["cvd_pts"]) < float(good["parts"]["cvd_pts"])


def test_proximity_short_liq_raises_score():
    eng = VolumeSweetSpotEngine(liq_sweep_short_usd=50_000.0)
    low = eng.get_entry_proximity(
        snapshot={
            "volume_ratio": 1.0,
            "close": 100.0,
            "vwap": 100.0,
            "liq_snapshot": {"short_liq_1m_usd": 5_000.0, "ready": True},
        }
    )
    high = eng.get_entry_proximity(
        snapshot={
            "volume_ratio": 1.0,
            "close": 100.0,
            "vwap": 100.0,
            "liq_snapshot": {"short_liq_1m_usd": 50_000.0, "ready": True},
        }
    )
    assert float(high["parts"]["liq_pts"]) > float(low["parts"]["liq_pts"])
    assert float(high["score"]) > float(low["score"])


def test_proximity_caps_below_100_unless_buy_ready():
    eng = VolumeSweetSpotEngine()
    prox = eng.get_entry_proximity(
        snapshot={
            "volume_ratio": 5.0,
            "retest_ok": True,
            "close": 100.0,
            "vwap": 100.0,
            "l2_imbalance_ratio": 3.0,
            "adx": 50.0,
            "chop": 10.0,
            "cvd_snapshot": {"cvd_5m": 1_000_000.0, "ready": True},
            "liq_snapshot": {"short_liq_1m_usd": 500_000.0, "ready": True},
            "setup_bar_open": 99.0,
            "setup_bar_close": 100.0,
        }
    )
    assert float(prox["score"]) <= 99.9
    ready = eng.get_entry_proximity(snapshot={"volume_ratio": 0.1}, buy_ready=True)
    assert float(ready["score"]) == 99.9


def test_proximity_uses_mocked_leadlag_snapshots():
    class FakeLeadLag:
        def get_cvd_snapshot(self, symbol):
            return {"cvd_5m": -10_000.0, "ready": True}

        def get_liq_snapshot(self, symbol):
            return {"short_liq_1m_usd": 10_000.0, "ready": True}

    eng = VolumeSweetSpotEngine(leadlag=FakeLeadLag())
    ind = SimpleNamespace(
        close=2500.0,
        vwap=2495.0,
        volume=100.0,
        extras={
            "volume_ratio": 2.2,
            "retest_ok": True,
            "setup_bar_open": 2490.0,
            "setup_bar_close": 2500.0,  # green → neg CVD hurts
            "l2_imbalance_ratio": 1.3,
        },
    )
    obs = SimpleNamespace(symbol="ETH-USD", indicators=ind, recent_bars=[])
    prox = eng.get_entry_proximity(obs)
    assert "score" in prox
    assert float(prox["parts"]["cvd_pts"]) <= 2.0
    assert float(prox["parts"]["liq_pts"]) < 15.0


def test_format_status_reply_includes_proximity_bar():
    text = format_status_reply(
        paper_cash=200.0,
        paper_equity=200.0,
        positions=[],
        paused=False,
        strategy_mode="volume_sweet_spot",
        last_tick_age_seconds=1.0,
        pid=1,
        paper=True,
        entry_proximity={"direction": "LONG", "score": 70.0},
        proximity_symbol="BTC-USD",
        proximity_price=100000.0,
    )
    assert "Target Setup: LONG" in text
    assert "Entry Proximity:" in text
    assert "[███████░░░] 70.0%" in text
    assert "focus=BTC-USD" in text
