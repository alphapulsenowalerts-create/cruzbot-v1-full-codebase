"""Exit sizing, time-stop, no-pyramid, paper persist, ledger skips rejects."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trading_bot.brokers.coinbase import CoinbaseBroker
from trading_bot.config import Settings, reload_settings
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
    utcnow,
)
from trading_bot.risk_manager import RiskManager


def _settings(**kwargs) -> Settings:
    base = dict(
        PAPER_TRADING_MODE=True,
        ACCOUNT_EQUITY=200.0,
        MAX_RISK_PER_TRADE_PCT=0.015,
        MAX_RISK_PER_TRADE_PCT_CEILING=0.02,
        DAILY_DRAWDOWN_LIMIT_PCT=0.03,
        MAX_POSITION_PCT=1.0,
        MAX_NOTIONAL_PER_TRADE_USD=50.0,
        MAX_TOTAL_EXPOSURE_USD=200.0,
        QTY_PRECISION=8,
        MAX_HOLD_MINUTES=45,
        ALLOW_PYRAMIDING=False,
        # Legacy ATR-bracket tests need vwap_scalp (default app mode is sweet-spot)
        STRATEGY_MODE="vwap_scalp",
        POST_ONLY=False,
        COINBASE_API_KEY="",
        COINBASE_API_SECRET="",
        BROKER="coinbase",
    )
    base.update(kwargs)
    return Settings(**base)


def test_sell_uses_full_open_position_qty():
    rm = RiskManager(_settings())
    account = AccountState(equity=200.0, cash=150.0, buying_power=150.0)
    pos = Position(
        symbol="BTC-USD",
        qty=0.00062576,
        avg_entry_price=78110.0,
        market_value=48.87,
        stop_loss=77000.0,
        take_profit=79500.0,
    )
    decision = Decision(
        action=Action.SELL,
        symbol="BTC-USD",
        confidence=90,
        reasoning="VWAP exit",
    )
    verdict = rm.evaluate(
        decision,
        account,
        entry_price=78000.0,
        atr=700.0,
        open_position=pos,
    )
    assert verdict.approved is True
    assert abs(verdict.sized_qty - 0.00062576) < 1e-12


def test_sell_while_flat_suppressed():
    rm = RiskManager(_settings())
    account = AccountState(equity=200.0, cash=200.0, buying_power=200.0)
    decision = Decision(
        action=Action.SELL,
        symbol="ETH-USD",
        confidence=80,
        reasoning="agent wants short",
    )
    verdict = rm.evaluate(decision, account, entry_price=2500.0, atr=20.0, open_position=None)
    assert verdict.approved is False
    assert "flat" in verdict.reason.lower() or "no open" in verdict.reason.lower()


def test_no_pyramid_rejects_second_buy():
    rm = RiskManager(_settings(ALLOW_PYRAMIDING=False))
    account = AccountState(equity=200.0, cash=150.0, buying_power=150.0)
    pos = Position(symbol="ETH-USD", qty=0.02, avg_entry_price=2500.0, market_value=50.0)
    decision = Decision(
        action=Action.BUY,
        symbol="ETH-USD",
        confidence=90,
        stop_loss=2470.0,
        take_profit=2550.0,
        reasoning="stack",
    )
    verdict = rm.evaluate(
        decision, account, entry_price=2500.0, atr=20.0, open_position=pos
    )
    assert verdict.approved is False
    assert "pyramid" in verdict.reason.lower() or "already long" in verdict.reason.lower()


def test_size_position_min_notional_returns_tuple():
    """Regression: size_position must never return RiskVerdict."""
    rm = RiskManager(_settings(MIN_NOTIONAL_USD=10.0, MAX_NOTIONAL_PER_TRADE_USD=50))
    # Tiny equity → dust qty under min notional
    qty, risk_amount, risk_pct = rm.size_position(5.0, 50000.0, 49900.0)
    assert qty == 0.0
    assert isinstance(risk_amount, float)
    assert isinstance(risk_pct, float)


@pytest.mark.asyncio
async def test_paper_book_persist_reload(tmp_path):
    book = tmp_path / "paper_book.json"
    settings = _settings(PAPER_BOOK_PATH=str(book), ACCOUNT_EQUITY=200.0)
    broker = CoinbaseBroker(settings)
    await broker.connect()
    broker._paper_prices["BTC-USD"] = 80000.0
    order = OrderRequest(
        symbol="BTC-USD",
        side=OrderSide.BUY,
        qty=0.0005,
        order_type=OrderType.LIMIT,
        limit_price=80000.0,
        stop_loss=79200.0,
        take_profit=81200.0,
        paper=True,
    )
    result = await broker.submit_order(order)
    assert result.status == OrderStatus.FILLED
    assert book.exists()
    cash_after = broker._paper_cash
    await broker.disconnect()

    # Reload fresh broker — positions must survive
    broker2 = CoinbaseBroker(settings)
    await broker2.connect()
    try:
        pos = await broker2.get_position("BTC-USD")
        assert pos is not None
        assert abs(pos.qty - 0.0005) < 1e-12
        assert pos.stop_loss == pytest.approx(79200.0)
        assert abs(broker2._paper_cash - cash_after) < 1e-6

        # SELL after reload must work
        sell = OrderRequest(
            symbol="BTC-USD",
            side=OrderSide.SELL,
            qty=0.0005,
            order_type=OrderType.LIMIT,
            limit_price=80100.0,
            paper=True,
        )
        sold = await broker2.submit_order(sell)
        assert sold.status == OrderStatus.FILLED
        assert await broker2.get_position("BTC-USD") is None
    finally:
        await broker2.disconnect()


def test_hard_bracket_time_stop():
    from main import TradingApp

    settings = _settings(MAX_HOLD_MINUTES=45)
    app = TradingApp(settings, once=True)
    opened = utcnow() - timedelta(minutes=50)
    pos = Position(
        symbol="SOL-USD",
        qty=0.3,
        avg_entry_price=150.0,
        market_value=45.0,
        stop_loss=140.0,
        take_profit=160.0,
        trail_distance=3.0,
        trail_high_water=152.0,
        opened_at=opened,
    )
    # Mark in the middle — only time stop should fire
    decision = app._check_hard_brackets("SOL-USD", pos, mark=151.0)
    assert decision is not None
    assert decision.action == Action.SELL
    assert decision.reasoning in ("time stop", "time-stop")


def test_hard_bracket_atr_stop_and_tp():
    from main import TradingApp

    settings = _settings()
    app = TradingApp(settings, once=True)
    pos = Position(
        symbol="ETH-USD",
        qty=0.02,
        avg_entry_price=2500.0,
        stop_loss=2470.0,
        take_profit=2550.0,
        trail_distance=30.0,
        trail_high_water=2520.0,
        opened_at=utcnow(),
    )
    d_sl = app._check_hard_brackets("ETH-USD", pos, mark=2465.0)
    assert d_sl is not None and d_sl.reasoning == "ATR stop"
    d_tp = app._check_hard_brackets("ETH-USD", pos, mark=2555.0)
    assert d_tp is not None and d_tp.reasoning == "ATR take-profit"
    d_trail = app._check_hard_brackets("ETH-USD", pos, mark=2485.0)  # 2520-30=2490
    assert d_trail is not None and d_trail.reasoning == "trail"


@pytest.mark.asyncio
async def test_ledger_skips_rejects(tmp_path, monkeypatch):
    # Point ledger DB at tmp
    import scripts.paper_ledger as pl

    monkeypatch.setattr(pl, "DB", tmp_path / "ledger.db")

    settings = _settings(ACCOUNT_EQUITY=200.0, PAPER_BOOK_PATH=str(tmp_path / "book.json"))
    broker = CoinbaseBroker(settings)
    await broker.connect()
    try:
        ex = Executor(broker, settings)
        # Rejected sell (flat)
        reject = await ex.submit(
            OrderRequest(
                symbol="BTC-USD",
                side=OrderSide.SELL,
                qty=0.001,
                order_type=OrderType.LIMIT,
                limit_price=80000.0,
                paper=True,
            )
        )
        assert reject.status == OrderStatus.REJECTED
        # Buy then check ledger has only fills
        broker._paper_prices["ETH-USD"] = 2500.0
        filled = await ex.submit(
            OrderRequest(
                symbol="ETH-USD",
                side=OrderSide.BUY,
                qty=0.02,
                order_type=OrderType.LIMIT,
                limit_price=2500.0,
                paper=True,
            )
        )
        assert filled.status == OrderStatus.FILLED
        rows = list(
            __import__("sqlite3")
            .connect(tmp_path / "ledger.db")
            .execute("SELECT kind, qty, notional FROM events")
        )
        assert all(r[1] > 0 and r[2] >= 1.0 for r in rows)
        assert not any(r[0] == "SELL" for r in rows)
        assert any(r[0] == "BUY" for r in rows)
    finally:
        await broker.disconnect()
