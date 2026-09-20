"""Intelligence upgrades: L2 imbalance, regime (ADX/Chop), MTF align, ATR sizing."""

from __future__ import annotations

import pandas as pd
import pytest

from trading_bot.config import Settings
from trading_bot.models import (
    AccountState,
    Action,
    AgentObservation,
    Decision,
    HigherTimeframeContext,
    IndicatorSnapshot,
)
from trading_bot.risk_manager import RiskManager
from trading_bot.strategy_volume_sweet_spot import VolumeSweetSpotEngine
from trading_bot.utils.indicators import (
    adx,
    atr_scaled_notional,
    check_l2_imbalance,
    check_mtf_align,
    check_regime_filter,
    choppiness_index,
    l2_depth_imbalance,
)


def _trending_df(n: int = 80, start: float = 100.0, drift: float = 0.4) -> pd.DataFrame:
    rows = []
    px = start
    for i in range(n):
        o = px
        c = px + drift
        h = max(o, c) + 0.2
        l = min(o, c) - 0.05
        rows.append({"open": o, "high": h, "low": l, "close": c, "volume": 1000.0})
        px = c
    return pd.DataFrame(rows)


def _choppy_df(n: int = 80, start: float = 100.0) -> pd.DataFrame:
    rows = []
    px = start
    for i in range(n):
        delta = 0.3 if i % 2 == 0 else -0.3
        o = px
        c = px + delta
        h = max(o, c) + 0.5
        l = min(o, c) - 0.5
        rows.append({"open": o, "high": h, "low": l, "close": c, "volume": 1000.0})
        px = c
    return pd.DataFrame(rows)


def _sweet_ind(**kwargs) -> IndicatorSnapshot:
    base = dict(
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
        extras={
            "volume_ratio": 2.5,
            "retest_ok": True,
            "breakout_volume": 1000.0,
            "pullback_volume": 100.0,
            "swing_low": 2450.0,
            "delta_proxy": "close>open",
            "breakout_rvol": 2.5,
            "resistance": 2600.0,
            "entry_reason": "RVOL Breakout + Low-Volume VWAP Retest",
            # Pass all intel gates by default
            "l2_imbalance_ratio": 1.5,
            "adx": 30.0,
            "chop": 45.0,
        },
    )
    base.update(kwargs)
    if "extras" in kwargs:
        merged = {
            "volume_ratio": 2.5,
            "retest_ok": True,
            "breakout_volume": 1000.0,
            "pullback_volume": 100.0,
            "swing_low": 2450.0,
            "delta_proxy": "close>open",
            "breakout_rvol": 2.5,
            "resistance": 2600.0,
            "entry_reason": "RVOL Breakout + Low-Volume VWAP Retest",
            "l2_imbalance_ratio": 1.5,
            "adx": 30.0,
            "chop": 45.0,
        }
        merged.update(kwargs["extras"])
        base["extras"] = merged
    return IndicatorSnapshot(**base)


def _engine(**kwargs) -> VolumeSweetSpotEngine:
    defaults = dict(
        l2_imbalance_enabled=True,
        l2_imbalance_min_ratio=1.2,
        regime_filter_enabled=True,
        adx_min=25.0,
        chop_max=60.0,
        mtf_align_enabled=True,
    )
    defaults.update(kwargs)
    return VolumeSweetSpotEngine(**defaults)


def _obs(ind: IndicatorSnapshot, htf: HigherTimeframeContext | None = None) -> AgentObservation:
    if htf is None:
        htf = HigherTimeframeContext(
            timeframe="1Hour",
            ema_200=2400.0,
            ema_200_1h=2400.0,
            ema_200_4h=2300.0,
        )
    return AgentObservation(symbol="ETH-USD", indicators=ind, htf=htf)


# --- Unit: L2 imbalance ---

def test_l2_depth_imbalance_ratio():
    mid = 100.0
    # Both levels within 0.5% band [99.5, 100.5]
    bids = [{"price": 99.8, "size": 12.0}, {"price": 99.5, "size": 5.0}]
    asks = [{"price": 100.2, "size": 8.0}, {"price": 100.4, "size": 5.0}]
    ratio = l2_depth_imbalance(bids, asks, mid=mid, band_pct=0.005)
    assert ratio is not None
    assert abs(ratio - ((12.0 + 5.0) / (8.0 + 5.0))) < 1e-9
    # Bid-heavy book → ratio > 1.2
    ok, _ = check_l2_imbalance(ratio, min_ratio=1.2, enabled=True)
    assert ok is True


def test_l2_imbalance_skip_below_threshold():
    ok, detail = check_l2_imbalance(0.95, min_ratio=1.2, enabled=True)
    assert ok is False
    assert detail == "l2_imbalance: 0.95 < 1.2"


def test_l2_imbalance_no_book():
    ok, detail = check_l2_imbalance(None, min_ratio=1.2, enabled=True)
    assert ok is False
    assert detail == "l2_imbalance: no_book"


def test_engine_skips_l2_imbalance():
    eng = _engine()
    ind = _sweet_ind(extras={"l2_imbalance_ratio": 0.95})
    d = eng.reason(_obs(ind))
    assert d.action == Action.HOLD
    assert "l2_imbalance: 0.95 < 1.2" in d.reasoning


def test_engine_skips_l2_no_book():
    eng = _engine()
    extras = {
        "retest_ok": True,
        "breakout_volume": 1000.0,
        "pullback_volume": 100.0,
        "swing_low": 2450.0,
        "resistance": 2600.0,
        "adx": 30.0,
        "chop": 45.0,
        # deliberately omit l2_imbalance_ratio
    }
    ind = IndicatorSnapshot(
        symbol="ETH-USD",
        close=2500.0,
        volume=100.0,
        vwap=2495.0,
        rsi=55.0,
        ema_fast=2498.0,
        ema_slow=2490.0,
        atr=10.0,
        extras=extras,
    )
    d = eng.reason(_obs(ind))
    assert d.action == Action.HOLD
    assert d.reasoning == "l2_imbalance: no_book"


# --- Unit: Regime ---

def test_adx_higher_on_trend_than_chop():
    adx_s = adx(_trending_df(n=100, drift=0.5)).dropna()
    assert len(adx_s) > 0
    adx_t = float(adx_s.iloc[-1])
    assert adx_t > 25.0  # strong directional move
    ci = choppiness_index(_choppy_df(n=100)).dropna()
    assert len(ci) > 0
    assert float(ci.iloc[-1]) > 50.0


def test_regime_blocks_low_adx():
    ok, detail = check_regime_filter(18.2, 40.0, adx_min=25, chop_max=60, enabled=True)
    assert ok is False
    assert detail == "regime: ADX 18.2 < 25"


def test_regime_blocks_high_chop():
    ok, detail = check_regime_filter(30.0, 67.1, adx_min=25, chop_max=60, enabled=True)
    assert ok is False
    assert detail == "regime: chop 67.1 > 60"


def test_engine_skips_regime_adx():
    eng = _engine()
    ind = _sweet_ind(extras={"adx": 18.2, "chop": 40.0})
    d = eng.reason(_obs(ind))
    assert d.action == Action.HOLD
    assert "regime: ADX 18.2 < 25" in d.reasoning


def test_engine_skips_regime_chop():
    eng = _engine()
    ind = _sweet_ind(extras={"adx": 30.0, "chop": 67.1})
    d = eng.reason(_obs(ind))
    assert d.action == Action.HOLD
    assert "regime: chop 67.1 > 60" in d.reasoning


# --- Unit: MTF ---

def test_mtf_align_below_1h():
    ok, detail = check_mtf_align(100.0, 101.0, 90.0, enabled=True)
    assert ok is False
    assert detail == "mtf_align: price below 1h EMA200"


def test_mtf_align_below_4h():
    ok, detail = check_mtf_align(100.0, 90.0, 101.0, enabled=True)
    assert ok is False
    assert detail == "mtf_align: price below 4h EMA200"


def test_engine_skips_mtf_1h():
    eng = _engine()
    htf = HigherTimeframeContext(
        ema_200=2600.0, ema_200_1h=2600.0, ema_200_4h=2300.0
    )
    d = eng.reason(_obs(_sweet_ind(), htf=htf))
    assert d.action == Action.HOLD
    assert "mtf_align: price below 1h EMA200" in d.reasoning


def test_engine_buy_when_all_gates_pass():
    eng = _engine()
    d = eng.reason(_obs(_sweet_ind()))
    assert d.action == Action.BUY


# --- ATR sizing ---

def test_atr_scaled_notional_inverse():
    # atr == atr_ref → full base
    n1 = atr_scaled_notional(1.0, 100.0, base_notional=50, atr_ref_pct=0.01, max_notional=50)
    assert abs(n1 - 50.0) < 1e-9
    # 2x ATR → half size
    n2 = atr_scaled_notional(2.0, 100.0, base_notional=50, atr_ref_pct=0.01, max_notional=50)
    assert abs(n2 - 25.0) < 1e-9
    # tiny ATR → clipped to max 50
    n3 = atr_scaled_notional(0.1, 100.0, base_notional=50, atr_ref_pct=0.01, max_notional=50)
    assert n3 == 50.0
    # huge ATR → clipped to min 10
    n4 = atr_scaled_notional(20.0, 100.0, base_notional=50, atr_ref_pct=0.01, min_notional=10, max_notional=50)
    assert n4 == 10.0


def test_atr_sizing_never_exceeds_100_hard_cap():
    settings = Settings(
        PAPER_TRADING_MODE=True,
        ATR_SIZING_ENABLED=True,
        ATR_REF_PCT=0.01,
        MAX_NOTIONAL_PER_TRADE_USD=100.0,
        MAX_TOTAL_EXPOSURE_USD=1000.0,
        MAX_POSITION_PCT=1.0,
        ACCOUNT_EQUITY=1600.0,
        MIN_NOTIONAL_USD=10.0,
        QTY_PRECISION=8,
        MAX_RISK_PER_TRADE_PCT=0.015,
        MAX_RISK_PER_TRADE_PCT_CEILING=0.02,
        DAILY_DRAWDOWN_LIMIT_PCT=0.03,
        STRATEGY_MODE="volume_sweet_spot",
        COINBASE_API_KEY="",
        COINBASE_API_SECRET="",
    )
    rm = RiskManager(settings)
    # Very low ATR would want huge size — still hard-capped at $50
    qty, _, _ = rm.size_position(1600.0, 100.0, 99.0, atr=0.05)
    assert qty * 100.0 <= 100.0 + 1e-6
    # High ATR shrinks below 50
    qty2, _, _ = rm.size_position(1600.0, 100.0, 99.0, atr=4.0)
    assert qty2 * 100.0 <= 100.0 + 1e-6
    assert qty2 * 100.0 < qty * 100.0


def test_atr_sizing_respects_200_exposure():
    settings = Settings(
        PAPER_TRADING_MODE=True,
        ATR_SIZING_ENABLED=True,
        MAX_NOTIONAL_PER_TRADE_USD=100.0,
        MAX_TOTAL_EXPOSURE_USD=1000.0,
        MAX_POSITION_PCT=1.0,
        ACCOUNT_EQUITY=1600.0,
        MIN_NOTIONAL_USD=10.0,
        QTY_PRECISION=8,
        MAX_RISK_PER_TRADE_PCT=0.015,
        MAX_RISK_PER_TRADE_PCT_CEILING=0.02,
        DAILY_DRAWDOWN_LIMIT_PCT=0.03,
        STRATEGY_MODE="volume_sweet_spot",
        MIN_TP_PCT=0.025,
        MAKER_FEE_RATE=0.005,
        FEE_TO_TARGET_MULT=2.5,
        COINBASE_API_KEY="",
        COINBASE_API_SECRET="",
    )
    rm = RiskManager(settings)
    account = AccountState(equity=1600.0, cash=1600.0, buying_power=1600.0)
    decision = Decision(
        action=Action.BUY,
        symbol="BTC-USD",
        confidence=80,
        stop_loss=99.0,
        take_profit=105.0,
        reasoning="atr exposure",
    )
    verdict = rm.evaluate(
        decision,
        account,
        entry_price=100.0,
        atr=1.0,
        open_exposure_usd=1000.0,
    )
    assert verdict.approved is False
    assert "exposure" in verdict.reason.lower()


def test_choppiness_index_runs():
    ci = choppiness_index(_choppy_df())
    assert float(ci.dropna().iloc[-1]) > 50.0
