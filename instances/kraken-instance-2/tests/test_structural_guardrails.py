"""Unit tests for the four structural entry guardrails."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trading_bot.config import Settings
from trading_bot.executor import Executor
from trading_bot.models import (
    AccountState,
    Action,
    Decision,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Quote,
)
from trading_bot.risk_manager import RiskManager
from trading_bot.state_store import BehavioralStateStore
from trading_bot.structural_guardrails import (
    check_buy_dedupe,
    check_fee_to_target,
    check_spread,
    required_min_tp_pct,
)


def _settings(**kwargs) -> Settings:
    base = dict(
        PAPER_TRADING_MODE=True,
        POST_ONLY=True,
        ALLOW_PYRAMIDING=False,
        MIN_TP_PCT=0.025,
        MAKER_FEE_RATE=0.005,
        FEE_TO_TARGET_MULT=2.5,
        BUY_DEDUPE_SECONDS=300,
        MAX_SPREAD_PCT=0.001,
        MAX_NOTIONAL_PER_TRADE_USD=100.0,
        MAX_TOTAL_EXPOSURE_USD=1000.0,
        ACCOUNT_EQUITY=1600.0,
        QTY_PRECISION=8,
        STRATEGY_MODE="volume_sweet_spot",
    )
    base.update(kwargs)
    return Settings(**base)


# --- 1) Dedup lock ---


def test_already_long_hard_reject():
    rm = RiskManager(_settings())
    account = AccountState(equity=1600.0, cash=1500.0, buying_power=1500.0)
    pos = Position(symbol="BTC-USD", qty=0.001, avg_entry_price=100000.0)
    decision = Decision(
        action=Action.BUY,
        symbol="BTC-USD",
        confidence=80,
        stop_loss=99000.0,
        take_profit=103000.0,
        reasoning="pyramid attempt",
    )
    verdict = rm.evaluate(
        decision, account, entry_price=100000.0, atr=500.0, open_position=pos
    )
    assert verdict.approved is False
    assert "already long" in verdict.reason.lower() or "pyramid" in verdict.reason.lower()


def test_buy_dedupe_within_window(tmp_path: Path):
    store = BehavioralStateStore(str(tmp_path / "s.db"))
    now = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
    store.record_buy_attempt("ETH-USD", now=now)
    elapsed = store.seconds_since_last_buy(
        "ETH-USD", now=now + timedelta(seconds=60)
    )
    ok, reason = check_buy_dedupe(elapsed, window_seconds=300)
    assert ok is False
    assert "buy_dedupe" in reason
    assert store.in_buy_dedupe("ETH-USD", 300, now=now + timedelta(seconds=60))


def test_buy_dedupe_after_window(tmp_path: Path):
    store = BehavioralStateStore(str(tmp_path / "s.db"))
    now = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
    store.record_buy_attempt("SOL-USD", now=now)
    elapsed = store.seconds_since_last_buy(
        "SOL-USD", now=now + timedelta(seconds=301)
    )
    ok, reason = check_buy_dedupe(elapsed, window_seconds=300)
    assert ok is True


# --- 2) Maker limit only ---


class _DummyBroker:
    name = "dummy"

    async def submit_order(self, order):
        raise AssertionError("broker should not be called for rejected market order")

    async def get_position(self, symbol):
        return None


@pytest.mark.asyncio
async def test_executor_rejects_market_orders():
    ex = Executor(_DummyBroker(), _settings(POST_ONLY=True))
    order = OrderRequest(
        symbol="BTC-USD",
        side=OrderSide.BUY,
        qty=0.001,
        order_type=OrderType.MARKET,
        paper=True,
    )
    result = await ex.submit(order)
    assert result.status == OrderStatus.REJECTED
    assert "market_orders_disabled" in (result.message or "")


@pytest.mark.asyncio
async def test_executor_forces_post_only_on_limit():
    class CapturingBroker(_DummyBroker):
        def __init__(self):
            self.last = None

        async def submit_order(self, order):
            self.last = order
            return type("R", (), {
                "client_order_id": order.client_order_id,
                "broker_order_id": "x",
                "status": OrderStatus.FILLED,
                "symbol": order.symbol,
                "side": order.side,
                "qty": order.qty,
                "filled_qty": order.qty,
                "avg_fill_price": order.limit_price,
                "message": "ok",
                "paper": True,
            })()

    # Use a proper OrderResult if model required
    from trading_bot.models import OrderResult

    class CapturingBroker2(_DummyBroker):
        def __init__(self):
            self.last = None

        async def submit_order(self, order):
            self.last = order
            return OrderResult(
                client_order_id=order.client_order_id,
                broker_order_id="x",
                status=OrderStatus.FILLED,
                symbol=order.symbol,
                side=order.side,
                qty=order.qty,
                filled_qty=order.qty,
                avg_fill_price=order.limit_price,
                message="ok",
                paper=True,
            )

    broker = CapturingBroker2()
    ex = Executor(broker, _settings(POST_ONLY=True))
    order = OrderRequest(
        symbol="BTC-USD",
        side=OrderSide.BUY,
        qty=0.001,
        order_type=OrderType.LIMIT,
        limit_price=100000.0,
        paper=True,
        post_only=False,
    )
    result = await ex.submit(order)
    assert result.status == OrderStatus.FILLED
    assert broker.last.post_only is True


# --- 3) Spread filter ---


def test_spread_filter_rejects_wide_spread():
    ok, reason = check_spread(100.0, 100.2, max_spread_pct=0.001)  # 0.2%
    assert ok is False
    assert "spread_filter" in reason


def test_spread_filter_allows_tight_spread():
    ok, reason = check_spread(100.0, 100.05, max_spread_pct=0.001)  # 0.05%
    assert ok is True


def test_spread_filter_missing_quote():
    ok, reason = check_spread(None, None, max_spread_pct=0.001)
    assert ok is False
    assert reason == "no_quote_spread"


# --- 4) Fee-to-target ---


def test_required_min_tp_uses_stricter_of_two():
    # maker 0.5% → RT 1.0%; ×2.5 → 2.5%; min_tp 2.0% → required 2.5%
    assert abs(required_min_tp_pct(0.02, 0.005, 2.5) - 0.025) < 1e-12
    # min_tp 3% stricter
    assert abs(required_min_tp_pct(0.03, 0.005, 2.5) - 0.03) < 1e-12


def test_fee_to_target_rejects_short_tp():
    ok, reason = check_fee_to_target(
        100.0,
        101.2,  # 1.2%
        min_tp_pct=0.025,
        maker_fee_rate=0.005,
        fee_to_target_mult=2.5,
    )
    assert ok is False
    assert "fee_to_target" in reason
    assert "1.20%" in reason
    assert "2.50%" in reason


def test_fee_to_target_in_risk_manager():
    rm = RiskManager(_settings())
    account = AccountState(equity=1600.0, cash=1600.0, buying_power=1600.0)
    decision = Decision(
        action=Action.BUY,
        symbol="XRP-USD",
        confidence=80,
        stop_loss=0.98,
        take_profit=1.012,  # 1.2% from 1.0
        reasoning="short tp",
    )
    verdict = rm.evaluate(decision, account, entry_price=1.0, atr=0.01)
    assert verdict.approved is False
    assert "fee_to_target" in verdict.reason
