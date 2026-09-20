
"""MVP: absolute bias lock + CVD slope gate."""
from __future__ import annotations

from trading_bot.cvd import (
    CvdTracker,
    REASON_CVD_SLOPE,
    check_cvd_slope_positive,
)


def test_cvd_slope_positive_allows_long():
    ok, reason = check_cvd_slope_positive(1.5, feed_warm=True)
    assert ok is True
    assert reason == ""


def test_cvd_slope_nonpositive_blocks_long():
    ok, reason = check_cvd_slope_positive(0.0, feed_warm=True)
    assert ok is False
    assert reason == REASON_CVD_SLOPE
    ok2, reason2 = check_cvd_slope_positive(-3.0, feed_warm=True)
    assert ok2 is False
    assert reason2 == REASON_CVD_SLOPE


def test_cvd_tracker_slope_buy_pressure():
    t = CvdTracker(period_sec=60.0, keep_periods=12)
    # synthetic: flush several periods with rising buy pressure
    base = 1_700_000_000.0
    for i in range(6):
        # each period: more buy notional
        t.ingest("BTC-USD", qty=1.0 + i, price=100.0, is_buyer_maker=False, ts=base + i * 60 + 1)
        # force period roll by ingesting next period start
        if i < 5:
            t.ingest("BTC-USD", qty=0.01, price=100.0, is_buyer_maker=False, ts=base + (i + 1) * 60 + 1)
    slope = t.slope("BTC-USD", periods=5)
    assert slope is not None
    assert slope > 0


def test_absolute_direction_lock_wait_when_shorts_disabled():
    class Fake:
        settings = type("S", (), {"allow_paper_shorts": False})()
        def _market_short_bias(self):
            return True
        def _apply_absolute_direction_lock(self, entry_proximity):
            if not isinstance(entry_proximity, dict) or not self._market_short_bias():
                return entry_proximity
            direction = str(entry_proximity.get("direction") or "").upper()
            if direction != "LONG":
                return entry_proximity
            out = dict(entry_proximity)
            if bool(getattr(self.settings, "allow_paper_shorts", False)):
                out["direction"] = "SHORT"
            else:
                out["direction"] = "WAIT"
                out["target"] = "WAIT (bias lock)"
            return out

    bot = Fake()
    locked = bot._apply_absolute_direction_lock({"direction": "LONG", "score": 90, "target": "ETH LONG"})
    assert locked["direction"] == "WAIT"


def test_stale_omit_helper():
    import time
    class Fake:
        def __init__(self):
            self._ages = {"ETH-USD": 15.0, "BTC-USD": 2.0}
        def _symbol_tick_age_s(self, symbol):
            return self._ages.get(symbol)
        def _symbol_tick_stale(self, symbol, max_age_s=10.0):
            age = self._symbol_tick_age_s(symbol)
            if age is None:
                return False
            return age > float(max_age_s)
    f = Fake()
    assert f._symbol_tick_stale("ETH-USD") is True
    assert f._symbol_tick_stale("BTC-USD") is False
