"""Focused tests: BTC regime, RVOL gate, ATR brackets, fee-lock, blacklist."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from trading_bot.market_regime import (
    STATE_BEAR_CHOP,
    STATE_BULL_OK,
    SKIP_BEAR_CHOP,
    SKIP_DUMP_30M,
    check_alt_long_allowed,
    compute_regime_from_bars,
)
from trading_bot.pair_blacklist import PairBlacklist
from trading_bot.utils.decision_filters import (
    check_elite_rvol_spread,
    elite_atr_bracket_levels,
    maybe_fee_lock_sl,
)
from trading_bot.scanner_auto_buy import evaluate_scanner_auto_buy


def _bars(closes, start=100.0):
    out = []
    for i, c in enumerate(closes):
        out.append(
            {
                "timestamp": f"2026-01-01T00:{i:02d}:00Z",
                "open": float(c),
                "high": float(c) * 1.001,
                "low": float(c) * 0.999,
                "close": float(c),
                "volume": 1000.0,
            }
        )
    return out


def test_regime_bear_chop_blocks_alt_long():
    # Flat then drop below EMA50 path: declining closes
    closes = [100 - i * 0.5 for i in range(60)]
    state, dump, meta = compute_regime_from_bars(_bars(closes))
    assert state == STATE_BEAR_CHOP
    ok, reason = check_alt_long_allowed("ETH-USD", state=state, dump_30m=False)
    assert ok is False and reason == SKIP_BEAR_CHOP
    ok_btc, _ = check_alt_long_allowed("BTC-USD", state=state, dump_30m=False)
    assert ok_btc is True


def test_regime_dump_30m_blocks_alts():
    closes_15 = [100.0] * 60
    # 1m dump >1.2%
    closes_1m = [100.0] * 25 + [98.5] * 10  # ~1.5% drop
    state, dump, meta = compute_regime_from_bars(_bars(closes_15), bars_1m=_bars(closes_1m))
    assert dump is True
    ok, reason = check_alt_long_allowed("SOL-USD", state=STATE_BULL_OK, dump_30m=True)
    assert ok is False and reason == SKIP_DUMP_30M


def test_elite_rvol_and_spread_gate():
    ok, _ = check_elite_rvol_spread(2.0, 100.0, 100.1, rvol_min=1.8, max_spread_pct=0.0025)
    assert ok is True
    ok, reason = check_elite_rvol_spread(1.2, 100.0, 100.1, rvol_min=1.8)
    assert ok is False and "rvol" in reason
    ok, reason = check_elite_rvol_spread(2.0, 100.0, 100.5, rvol_min=1.8, max_spread_pct=0.0025)
    assert ok is False and "spread" in reason


def test_scanner_enforces_rvol_even_aggressive():
    ok, status = evaluate_scanner_auto_buy(
        proximity=80.0, threshold=35.0, profile="aggressive",
        paper=True, paused=False, already_open=False, open_position_count=0,
        rvol=1.0, rvol_min=1.8, bypass_volume_spike=True,
    )
    assert ok is False and status == "SKIPPED: rvol"
    ok2, status2 = evaluate_scanner_auto_buy(
        proximity=80.0, threshold=35.0, profile="aggressive",
        paper=True, paused=False, already_open=False, open_position_count=0,
        rvol=2.0, rvol_min=1.8, bypass_volume_spike=True,
    )
    assert ok2 is True and status2 == "AUTO-FIRED"


def test_elite_atr_brackets():
    sl, tp = elite_atr_bracket_levels(100.0, 2.0, short=False, sl_mult=1.5, tp_mult=2.5)
    assert abs(sl - 97.0) < 1e-9
    assert abs(tp - 105.0) < 1e-9
    sl_s, tp_s = elite_atr_bracket_levels(100.0, 2.0, short=True, sl_mult=1.5, tp_mult=2.5)
    assert abs(sl_s - 103.0) < 1e-9
    assert abs(tp_s - 95.0) < 1e-9


def test_fee_lock_at_0_6pct():
    # +0.5% not enough
    assert maybe_fee_lock_sl(100.0, 100.5, 97.0) is None
    # +0.6% locks to 100.3
    lock = maybe_fee_lock_sl(100.0, 100.61, 97.0)
    assert lock is not None and abs(lock - 100.3) < 1e-9
    # short
    lock_s = maybe_fee_lock_sl(100.0, 99.39, 103.0, short=True)
    assert lock_s is not None and abs(lock_s - 99.7) < 1e-9


def test_pair_blacklist_three_losses(tmp_path: Path):
    db = tmp_path / "trades.db"
    bl = PairBlacklist(db, enabled=True)
    now = time.time()
    for i in range(3):
        until = bl.record_close(
            "ETH-USD", entry=100.0, exit=99.0, net_pnl=-1.0, timestamp=now - i
        )
    assert until is not None
    ok, reason = bl.check_entry("ETH-USD")
    assert ok is False and "blacklist" in reason
    # BTC unrelated clear
    assert bl.check_entry("BTC-USD")[0] is True
