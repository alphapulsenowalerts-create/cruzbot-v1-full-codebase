"""Focused tests for C2 Telegram commands + /set_spread."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from trading_bot.telegram_commands import (
    KNOWN_COMMANDS,
    SET_SPREAD_USAGE,
    SetSpreadError,
    execute_set_spread,
    format_balance_reply,
    format_help_reply,
    format_history_reply,
    format_ping_reply,
    format_positions_reply,
    format_regime_reply,
    format_set_spread_reply,
    format_status_reply,
    parse_command,
    parse_set_max_spread_alias,
    parse_set_spread_args,
    recent_trades_from_ledger,
    redact_secrets,
)


REQUIRED_NEW = {
    "ping",
    "positions",
    "balance",
    "history",
    "grok",
    "regime",
    "logs",
    "help",
    "set_spread",
    "set",
}


def test_known_commands_include_c2_and_spread():
    assert REQUIRED_NEW <= set(KNOWN_COMMANDS)


@pytest.mark.parametrize(
    "text,cmd",
    [
        ("/ping", "ping"),
        ("/help", "help"),
        ("/positions", "positions"),
        ("/balance", "balance"),
        ("/history", "history"),
        ("/grok", "grok"),
        ("/regime", "regime"),
        ("/logs", "logs"),
        ("/set_spread 0.5", "set_spread"),
        ("/set MAX_SPREAD_PCT 0.5", "set"),
        ("/set_spread@CruzBot 0.5%", "set_spread"),
    ],
)
def test_parse_command_new(text, cmd):
    parsed = parse_command(text)
    assert parsed is not None
    assert parsed[0] == cmd


def test_help_lists_new_and_existing():
    text = format_help_reply()
    assert "/ping" in text
    assert "/set_spread" in text
    assert "/status" in text
    assert "/test_trade" in text
    assert "Spread" in text or "set_spread" in text


def test_format_ping_and_positions_balance():
    assert format_ping_reply(12.3) == "pong 12ms"
    assert "none" in format_positions_reply([], paper=True).lower()
    pos = format_positions_reply(
        [
            {
                "symbol": "BTC-USD",
                "qty": 0.01,
                "avg_entry_price": 100.0,
                "unrealized_pl": 1.0,
                "pnl_pct": 1.0,
                "stop_loss": 95.0,
                "take_profit": None,
            }
        ],
        paper=True,
    )
    assert "BTC-USD" in pos
    assert "sl=$95" in pos
    assert "tp=n/a" in pos
    bal = format_balance_reply(paper=True, cash=1600, equity=1600)
    assert "PAPER" in bal
    assert "1600.00" in bal


def test_format_status_includes_spread_cap():
    text = format_status_reply(
        paper_cash=1600.0,
        paper_equity=1600.0,
        positions=[],
        paused=False,
        strategy_mode="volume_sweet_spot",
        last_tick_age_seconds=1.0,
        paper=True,
        symbols=["BTC-USD"],
        max_notional_per_trade=100.0,
        max_total_exposure=1000.0,
        entry_threshold=60,
        max_spread_pct=0.001,
    )
    assert "Spread cap: 0.1%" in text
    assert "Threshold: 60%" in text


def test_parse_set_spread_happy_and_alias():
    assert parse_set_spread_args(["0.5"]) == 0.5
    assert parse_set_spread_args(["0.5%"]) == 0.5
    assert parse_set_spread_args(["0.01"]) == 0.01
    assert parse_set_spread_args(["5"]) == 5.0
    assert parse_set_max_spread_alias(["MAX_SPREAD_PCT", "0.5"]) == 0.5
    assert parse_set_max_spread_alias(["MAX_SPREAD_PCT=0.5%"]) == 0.5


@pytest.mark.parametrize("args", [[], ["0"], ["5.1"], ["abc"], ["0.5", "1"]])
def test_parse_set_spread_rejects(args):
    with pytest.raises(SetSpreadError) as ei:
        parse_set_spread_args(args)
    assert "Usage: /set_spread" in str(ei.value) or "must be" in str(ei.value)


def test_execute_set_spread_persists(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("PAPER_TRADING_MODE=true\n")
    settings = SimpleNamespace(max_spread_pct=0.001)
    environ: dict = {}
    reply = execute_set_spread(
        settings, ["0.5"], env_path=env_path, environ=environ
    )
    assert "0.5%" in reply
    assert abs(settings.max_spread_pct - 0.005) < 1e-12
    assert "MAX_SPREAD_PCT=0.005" in env_path.read_text()
    assert environ.get("MAX_SPREAD_PCT") == "0.005"


def test_redact_secrets_and_regime_honest():
    scrubbed = redact_secrets("TELEGRAM_BOT_TOKEN=123:ABC api_key=zzz")
    assert "123:ABC" not in scrubbed
    assert "[redacted]" in scrubbed.lower() or "redacted" in scrubbed.lower()
    regime = format_regime_reply(market_regime=None, note="partial")
    assert "n/a" in regime.lower() or "not in active_params" in regime.lower()


def test_history_formatter_and_optional_ledger():
    text = format_history_reply(
        [
            {
                "when": "09/19 16:27 CT",
                "side": "SELL",
                "symbol": "BTC-USD",
                "qty": 0.001,
                "price": 80000,
                "pnl": -1.16,
            }
        ]
    )
    assert "BTC-USD" in text
    assert "-$1.16" in text
    # real ledger if present
    ledger = Path(__file__).resolve().parents[1] / "data" / "paper_ledger.db"
    if ledger.exists():
        rows = recent_trades_from_ledger(str(ledger), limit=5)
        assert isinstance(rows, list)
        assert len(rows) <= 5


def test_format_set_spread_reply():
    r = format_set_spread_reply(0.5)
    assert "0.5%" in r
    assert "MAX_SPREAD_PCT=" in r
