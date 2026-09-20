"""Focused tests for Telegram /wipe_paper (full paper scratch) + /factory_reset alias."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

from trading_bot.telegram_commands import (
    KNOWN_COMMANDS,
    REPLY_WIPE_PAPER_CONFIRM_EXPIRED,
    REPLY_WIPE_PAPER_LIVE_REFUSED,
    WIPE_PAPER_CONFIRM_TTL_SECONDS,
    OpsControlState,
    WipePaperError,
    assert_paper_mode_for_wipe_paper,
    format_help_reply,
    format_wipe_paper_done_reply,
    format_wipe_paper_pending_reply,
    parse_command,
    parse_wipe_paper_args,
    wipe_paper_artifacts,
)


def test_known_commands_include_wipe_and_alias():
    assert "wipe_paper" in KNOWN_COMMANDS
    assert "factory_reset" in KNOWN_COMMANDS
    assert "reset_paper" in KNOWN_COMMANDS  # lighter reset left intact


def test_help_lists_wipe_paper():
    text = format_help_reply()
    assert "/wipe_paper" in text
    assert "factory_reset" in text.lower() or "/factory_reset" in text
    assert "/reset_paper" in text


@pytest.mark.parametrize(
    "text,cmd,args",
    [
        ("/wipe_paper", "wipe_paper", []),
        ("/wipe_paper 1600", "wipe_paper", ["1600"]),
        ("/wipe_paper confirm", "wipe_paper", ["confirm"]),
        ("/wipe_paper confirm 2500", "wipe_paper", ["confirm", "2500"]),
        ("/factory_reset", "factory_reset", []),
        ("/factory_reset 1600", "factory_reset", ["1600"]),
        ("/factory_reset confirm", "factory_reset", ["confirm"]),
        ("/wipe_paper@CruzBot 1800", "wipe_paper", ["1800"]),
    ],
)
def test_parse_command_wipe_paper(text, cmd, args):
    parsed = parse_command(text)
    assert parsed is not None
    assert parsed[0] == cmd
    assert parsed[1] == args


@pytest.mark.parametrize(
    "args,expect",
    [
        ([], (False, None)),
        (["1600"], (False, 1600.0)),
        (["confirm"], (True, None)),
        (["confirm", "2500"], (True, 2500.0)),
    ],
)
def test_parse_wipe_paper_args_ok(args, expect):
    assert parse_wipe_paper_args(args) == expect


@pytest.mark.parametrize("args", [["nope"], ["0"], ["-1"], ["confirm", "x"]])
def test_parse_wipe_paper_args_bad(args):
    with pytest.raises(WipePaperError):
        parse_wipe_paper_args(args)


def test_paper_only_guard():
    assert_paper_mode_for_wipe_paper(paper=True)
    with pytest.raises(WipePaperError) as ei:
        assert_paper_mode_for_wipe_paper(paper=False)
    assert ei.value.args[0] == REPLY_WIPE_PAPER_LIVE_REFUSED


def test_ops_pending_wipe_paper_ttl():
    ops = OpsControlState()
    assert ops.has_pending_wipe_paper_confirm() is False
    ops.arm_wipe_paper_confirm(1600.0, explicit=True, ttl=WIPE_PAPER_CONFIRM_TTL_SECONDS)
    assert ops.has_pending_wipe_paper_confirm() is True
    assert ops.pending_wipe_paper_cash() == 1600.0
    assert ops.pending_wipe_paper_explicit() is True
    ops._pending_wipe_paper_until = time.monotonic() - 1.0
    assert ops.has_pending_wipe_paper_confirm() is False


def test_format_wipe_replies():
    pending = format_wipe_paper_pending_reply(1600.0, ttl_seconds=60)
    assert "1600.00" in pending
    assert "/wipe_paper confirm" in pending
    done = format_wipe_paper_done_reply(
        1600.0,
        positions=0,
        history_cleared=True,
        logs_cleared=True,
        ledger_rows_deleted=3,
        trading_tables_cleared={"trade_memory": 1, "events": 2},
    )
    assert "cash=$1600.00" in done
    assert "history_cleared=yes" in done
    assert "logs_cleared=yes" in done


def test_wipe_paper_artifacts_clears_dbs_and_log(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir()
    ledger = data / "paper_ledger.db"
    trading = data / "trading_bot_2.db"
    log = data / "paper_loop.log"
    log.write_text("old log line\n", encoding="utf-8")

    conn = sqlite3.connect(str(ledger))
    conn.execute(
        """CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, kind TEXT, symbol TEXT, side TEXT,
            qty REAL, price REAL, notional REAL, fee REAL, pnl REAL,
            note TEXT, raw TEXT
        )"""
    )
    conn.execute(
        "INSERT INTO events (ts,kind,symbol,side,qty,price,notional,fee,pnl,note,raw) "
        "VALUES (1,'BUY','BTC-USD','BUY',1,100,100,0,0,'','{}')"
    )
    conn.commit()
    conn.close()

    conn = sqlite3.connect(str(trading))
    conn.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY, ts TEXT, event_type TEXT, symbol TEXT, payload TEXT)"
    )
    conn.execute(
        "CREATE TABLE trade_memory (id INTEGER PRIMARY KEY, ts TEXT, symbol TEXT, action TEXT, outcome TEXT, reason TEXT, pnl REAL)"
    )
    conn.execute(
        "INSERT INTO events VALUES (1,'t','fill','BTC-USD','{}')"
    )
    conn.execute(
        "INSERT INTO trade_memory VALUES (1,'t','BTC-USD','BUY','ok','',1.0)"
    )
    conn.commit()
    conn.close()

    # sentinel secrets file must remain
    env = tmp_path / ".env"
    env.write_text("SECRET=keep\n", encoding="utf-8")

    summary = wipe_paper_artifacts(
        project_root=tmp_path,
        sqlite_path=trading,
        ledger_path=ledger,
        log_path=log,
    )
    assert summary["ledger_rows_deleted"] == 1
    assert summary["trading_tables_cleared"].get("events") == 1
    assert summary["trading_tables_cleared"].get("trade_memory") == 1
    assert summary["log_truncated"] is True
    assert log.read_text(encoding="utf-8") == ""
    assert ledger.exists() and trading.exists()
    assert "SECRET=keep" in env.read_text(encoding="utf-8")

    conn = sqlite3.connect(str(ledger))
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    conn.close()
    conn = sqlite3.connect(str(trading))
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM trade_memory").fetchone()[0] == 0
    conn.close()


@pytest.mark.asyncio
async def test_cmd_wipe_paper_confirm_required_and_clears(tmp_path):
    os.environ["DRY_RUN"] = "true"
    os.environ["BROKER"] = "mock"
    os.environ["PAPER_TRADING_MODE"] = "true"
    os.environ["TELEGRAM_COMMANDS_ENABLED"] = "false"
    os.environ["SYMBOLS"] = "BTC-USD"
    os.environ["SQLITE_PATH"] = str(tmp_path / "wipe.db")
    os.environ["LOG_LEVEL"] = "WARNING"
    os.environ["ACCOUNT_EQUITY"] = "1600"

    from trading_bot.config import reload_settings
    from main import TradingApp
    import main as main_mod

    # Seed ledger + log under tmp project root
    data = tmp_path / "data"
    data.mkdir()
    ledger = data / "paper_ledger.db"
    log = data / "paper_loop.log"
    log.write_text("noise\n", encoding="utf-8")
    conn = sqlite3.connect(str(ledger))
    conn.execute(
        """CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, kind TEXT, symbol TEXT, side TEXT,
            qty REAL, price REAL, notional REAL, fee REAL, pnl REAL,
            note TEXT, raw TEXT
        )"""
    )
    conn.execute(
        "INSERT INTO events (ts,kind,symbol,side,qty,price,notional,fee,pnl,note,raw) "
        "VALUES (1,'SELL','ETH-USD','SELL',1,10,10,0,1.5,'','{}')"
    )
    conn.commit()
    conn.close()

    # Seed trading sqlite used by TradeLogger / wipe
    trading = tmp_path / "wipe.db"
    conn = sqlite3.connect(str(trading))
    conn.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY, ts TEXT, event_type TEXT, symbol TEXT, payload TEXT)"
    )
    conn.execute(
        "CREATE TABLE trade_memory (id INTEGER PRIMARY KEY, ts TEXT, symbol TEXT, action TEXT, outcome TEXT, reason TEXT, pnl REAL)"
    )
    conn.execute("INSERT INTO trade_memory VALUES (1,'t','ETH-USD','SELL','ok','',1.5)")
    conn.commit()
    conn.close()

    settings = reload_settings()
    app = TradingApp(settings, once=True)
    # Put a fake position so reset clears it
    app.broker._cash = 1000.0
    app.broker._positions = {"BTC-USD": {"qty": 0.01, "avg_price": 50000}}
    app._last_obs = {"BTC-USD": object()}

    env_file = tmp_path / ".env"
    env_file.write_text("ACCOUNT_EQUITY=1600\n", encoding="utf-8")
    real_root = main_mod.PROJECT_ROOT
    main_mod.PROJECT_ROOT = tmp_path
    try:
        reply = await app._cmd_wipe_paper("wipe_paper", ["2500"])
        assert "confirm" in reply.lower()
        assert app.ops.has_pending_wipe_paper_confirm() is True
        assert float(app.broker._cash) == 1000.0  # unchanged until confirm
        assert len(app.broker._positions) == 1

        reply = await app._cmd_wipe_paper("wipe_paper", ["confirm"])
        assert "factory wipe" in reply.lower() or "Paper factory wipe" in reply
        assert "cash=$2500.00" in reply
        assert "history_cleared=yes" in reply
        assert "logs_cleared=yes" in reply
        assert float(app.broker._cash) == 2500.0
        assert app.broker._positions == {}
        assert app._last_obs == {}
        assert log.read_text(encoding="utf-8") == ""
        conn = sqlite3.connect(str(ledger))
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        conn.close()
        conn = sqlite3.connect(str(trading))
        assert conn.execute("SELECT COUNT(*) FROM trade_memory").fetchone()[0] == 0
        conn.close()
        assert "ACCOUNT_EQUITY=2500" in env_file.read_text(encoding="utf-8")
    finally:
        main_mod.PROJECT_ROOT = real_root


@pytest.mark.asyncio
async def test_cmd_wipe_paper_live_refused(tmp_path):
    os.environ["DRY_RUN"] = "true"
    os.environ["BROKER"] = "mock"
    os.environ["PAPER_TRADING_MODE"] = "false"
    os.environ["TELEGRAM_COMMANDS_ENABLED"] = "false"
    os.environ["SYMBOLS"] = "BTC-USD"
    os.environ["SQLITE_PATH"] = str(tmp_path / "wipe_live.db")
    os.environ["LOG_LEVEL"] = "WARNING"

    from trading_bot.config import reload_settings
    from main import TradingApp

    settings = reload_settings()
    app = TradingApp(settings, once=True)
    object.__setattr__(app.settings, "paper_trading_mode", False)
    reply = await app._cmd_wipe_paper("wipe_paper", ["1600"])
    assert reply == REPLY_WIPE_PAPER_LIVE_REFUSED
    assert app.ops.has_pending_wipe_paper_confirm() is False


@pytest.mark.asyncio
async def test_cmd_wipe_paper_confirm_without_pending(tmp_path):
    os.environ["DRY_RUN"] = "true"
    os.environ["BROKER"] = "mock"
    os.environ["PAPER_TRADING_MODE"] = "true"
    os.environ["TELEGRAM_COMMANDS_ENABLED"] = "false"
    os.environ["SYMBOLS"] = "BTC-USD"
    os.environ["SQLITE_PATH"] = str(tmp_path / "wipe_exp.db")
    os.environ["LOG_LEVEL"] = "WARNING"
    os.environ["ACCOUNT_EQUITY"] = "1600"

    from trading_bot.config import reload_settings
    from main import TradingApp

    settings = reload_settings()
    app = TradingApp(settings, once=True)
    reply = await app._cmd_wipe_paper("wipe_paper", ["confirm"])
    assert reply == REPLY_WIPE_PAPER_CONFIRM_EXPIRED


@pytest.mark.asyncio
async def test_factory_reset_alias_handler(tmp_path):
    os.environ["DRY_RUN"] = "true"
    os.environ["BROKER"] = "mock"
    os.environ["PAPER_TRADING_MODE"] = "true"
    os.environ["TELEGRAM_COMMANDS_ENABLED"] = "false"
    os.environ["SYMBOLS"] = "BTC-USD"
    os.environ["SQLITE_PATH"] = str(tmp_path / "wipe_alias.db")
    os.environ["LOG_LEVEL"] = "WARNING"
    os.environ["ACCOUNT_EQUITY"] = "1600"

    from trading_bot.config import reload_settings
    from main import TradingApp
    import main as main_mod

    (tmp_path / "data").mkdir(exist_ok=True)
    settings = reload_settings()
    app = TradingApp(settings, once=True)
    real_root = main_mod.PROJECT_ROOT
    main_mod.PROJECT_ROOT = tmp_path
    try:
        reply = await app._cmd_wipe_paper("factory_reset", [])
        assert "confirm" in reply.lower()
        assert app.ops.has_pending_wipe_paper_confirm() is True
    finally:
        main_mod.PROJECT_ROOT = real_root
