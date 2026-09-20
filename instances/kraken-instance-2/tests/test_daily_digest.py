"""Unit tests for once-daily PnL digest helpers."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from trading_bot.daily_digest import (
    DigestSnapshot,
    next_summary_datetime,
    open_exposure_from_positions,
    seconds_until_summary,
)

_CT = ZoneInfo("America/Chicago")


def test_format_message_exact_shape():
    snap = DigestSnapshot(
        date_ct="09/19/2026",
        date_key="2026-09-19",
        starting_equity=1600.0,
        current_equity=1634.5,
        realized_pnl_24h=34.5,
        realized_pnl_pct=2.15625,
        trades_closed=4,
        wins=3,
        losses=1,
        open_exposure=100.0,
    )
    msg = snap.format_message()
    assert msg.startswith("📉📈 SHIT LETS SEE SUMMARY — [09/19/2026]")
    assert "Starting Equity: $1,600.00" in msg
    assert "Current Equity: $1,634.50" in msg
    assert "24h Realized PnL: +$34.50 (+2.16%)" in msg
    assert "Trades Closed: 4 (3 Win / 1 Loss)" in msg
    assert "Active Open Exposure: $100.00" in msg


def test_schedule_is_7am_not_7pm():
    fake = datetime(2026, 9, 19, 18, 0, 0, tzinfo=_CT)
    nxt = next_summary_datetime(7, 0, "America/Chicago", now=fake)
    assert nxt.hour == 7 and nxt.minute == 0 and nxt.day == 20
    delay = seconds_until_summary(7, 0, "America/Chicago", now=fake)
    assert 12 * 3600 < delay < 14 * 3600


def test_open_exposure_sum():
    positions = {
        "BTC-USD": {"qty": 0.001, "avg_entry_price": 100_000, "market_value": 100.0},
        "ETH-USD": {"qty": 0.05, "avg_entry_price": 2000},  # 100 notional
    }
    assert open_exposure_from_positions(positions) == 200.0
