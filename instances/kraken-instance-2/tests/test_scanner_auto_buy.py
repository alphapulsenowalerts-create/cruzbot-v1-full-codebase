"""Tests for scanner proximity → auto paper-buy bridge."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from trading_bot.models import Decision, OrderResult, OrderSide, OrderStatus
from trading_bot.scanner_auto_buy import (
    AUTO_PAPER_MAX_EXPOSURE_USD,
    AUTO_PAPER_MAX_NOTIONAL_USD,
    clamp_auto_notional,
    evaluate_scanner_auto_buy,
    format_scanner_loop_log,
)


def test_clamp_auto_notional_hard_ceiling():
    assert clamp_auto_notional(5000) == AUTO_PAPER_MAX_NOTIONAL_USD
    assert clamp_auto_notional(250) == 250.0
    assert clamp_auto_notional(0) == AUTO_PAPER_MAX_NOTIONAL_USD


def test_format_scanner_loop_log_exact_style():
    line = format_scanner_loop_log("AVAX-USD", 33.4, 35.0, "SKIPPED: below_threshold")
    assert line == (
        "[SCANNER LOOP] Symbol: AVAX-USD | Proximity: 33% | "
        "Target: 35% | Status: SKIPPED: below_threshold"
    )


def test_aggressive_fires_despite_tod_and_rvol_one():
    ok, status = evaluate_scanner_auto_buy(
        proximity=40.0,
        threshold=35.0,
        profile="aggressive",
        paper=True,
        paused=False,
        already_open=False,
        open_position_count=0,
        max_concurrent=3,
        open_exposure_usd=0.0,
        max_exposure_usd=1500.0,
        trade_notional=1000.0,
        tod_blocked=True,  # would normally block
        disable_tod_gate=True,
        rvol=1.0,
        rvol_min=1.0,
        bypass_volume_spike=True,
    )
    assert ok is True
    assert status == "AUTO-FIRED"


def test_below_threshold_skipped():
    ok, status = evaluate_scanner_auto_buy(
        proximity=33.0,
        threshold=35.0,
        profile="aggressive",
        paper=True,
        paused=False,
        already_open=False,
        open_position_count=0,
        tod_blocked=False,
        rvol=1.0,
        bypass_volume_spike=True,
    )
    assert ok is False
    assert status == "SKIPPED: below_threshold"


def test_max_positions_blocks():
    ok, status = evaluate_scanner_auto_buy(
        proximity=80.0,
        threshold=35.0,
        profile="aggressive",
        paper=True,
        paused=False,
        already_open=False,
        open_position_count=3,
        max_concurrent=3,
        rvol=1.0,
        bypass_volume_spike=True,
    )
    assert ok is False
    assert status == "SKIPPED: max_positions"


def test_exposure_cap_blocks():
    ok, status = evaluate_scanner_auto_buy(
        proximity=80.0,
        threshold=35.0,
        profile="aggressive",
        paper=True,
        paused=False,
        already_open=False,
        open_position_count=1,
        open_exposure_usd=800.0,
        max_exposure_usd=AUTO_PAPER_MAX_EXPOSURE_USD,
        trade_notional=1000.0,  # 800+1000 > 1500
        rvol=1.0,
        bypass_volume_spike=True,
    )
    assert ok is False
    assert status == "SKIPPED: exposure_cap"


def test_medium_honors_tod_gate():
    ok, status = evaluate_scanner_auto_buy(
        proximity=70.0,
        threshold=65.0,
        profile="medium",
        paper=True,
        paused=False,
        already_open=False,
        open_position_count=0,
        tod_blocked=True,
        disable_tod_gate=False,
        rvol=2.5,
        rvol_min=2.0,
    )
    assert ok is False
    assert status == "SKIPPED: tod_gate"


def test_medium_honors_rvol():
    ok, status = evaluate_scanner_auto_buy(
        proximity=70.0,
        threshold=65.0,
        profile="medium",
        paper=True,
        paused=False,
        already_open=False,
        open_position_count=0,
        tod_blocked=False,
        rvol=1.0,
        rvol_min=2.0,
    )
    assert ok is False
    assert status == "SKIPPED: rvol"


def test_live_and_paused_and_already_open():
    assert evaluate_scanner_auto_buy(
        proximity=90, threshold=35, profile="aggressive", paper=False,
        paused=False, already_open=False, open_position_count=0,
        bypass_volume_spike=True,
    )[1] == "SKIPPED: live_blocked"
    assert evaluate_scanner_auto_buy(
        proximity=90, threshold=35, profile="aggressive", paper=True,
        paused=True, already_open=False, open_position_count=0,
        bypass_volume_spike=True,
    )[1] == "SKIPPED: paused"
    assert evaluate_scanner_auto_buy(
        proximity=90, threshold=35, profile="aggressive", paper=True,
        paused=False, already_open=True, open_position_count=1,
        bypass_volume_spike=True,
    )[1] == "SKIPPED: already_open"


@pytest.mark.asyncio
async def test_auto_paper_buy_submits_clamped_order():
    """TradingApp._auto_paper_buy uses executor.submit with $1000 clamp."""
    import main as main_mod

    app = MagicMock()
    app.settings = MagicMock(
        paper_trading_mode=True,
        qty_precision=8,
        max_notional_per_trade_usd=5000.0,  # higher than ceiling → clamp
    )
    app.ops = MagicMock(paused=False)
    app.state = MagicMock()
    app.trade_logger = MagicMock()
    filled = OrderResult(
        client_order_id="t1",
        broker_order_id="b1",
        status=OrderStatus.FILLED,
        symbol="SOL-USD",
        side=OrderSide.BUY,
        qty=10.0,
        filled_qty=10.0,
        avg_fill_price=100.0,
        message="paper fill",
        paper=True,
    )
    app.executor = MagicMock()
    app.executor.submit = AsyncMock(return_value=filled)

    # Bind unbound method
    dec = await main_mod.TradingApp._auto_paper_buy(
        app,
        "SOL-USD",
        price=100.0,
        notional=5000.0,
        proximity=40.0,
        threshold=35.0,
    )
    assert isinstance(dec, Decision)
    assert dec.action.value == "BUY"
    app.executor.submit.assert_awaited_once()
    order = app.executor.submit.await_args.args[0]
    # qty * price ~= 1000 (clamped)
    assert float(order.qty) * float(order.limit_price) <= AUTO_PAPER_MAX_NOTIONAL_USD + 1.0
