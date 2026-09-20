"""Stabilization: backup helper, maintenance config, crash alert (mocked notifier)."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from trading_bot.backup import (
    backup_sqlite_dbs,
    backup_timestamp,
    prune_backups,
    seconds_until_chicago_midnight,
)
from trading_bot.config import Settings, reload_settings
from main import format_crash_alert, notify_critical_crash


_CT = ZoneInfo("America/Chicago")


def test_format_crash_alert_includes_type_and_message():
    text = format_crash_alert(RuntimeError("disk full"))
    assert text.startswith("CRITICAL: Bot crashed due to RuntimeError")
    assert "disk full" in text


def test_format_crash_alert_redacts_secretish():
    text = format_crash_alert(RuntimeError("token BOT_TOKEN=abc leaked"))
    assert "CRITICAL:" in text
    assert "abc" not in text or "[redacted]" in text


def test_maintenance_interval_config(monkeypatch):
    monkeypatch.setenv("MAINTENANCE_INTERVAL_HOURS", "6")
    monkeypatch.setenv("PAPER_TRADING_MODE", "true")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("BROKER", "mock")
    s = reload_settings()
    assert float(s.maintenance_interval_hours) == 6.0
    assert int(s.sqlite_backup_keep) == 7


def test_seconds_until_chicago_midnight_positive():
    # Fixed: 2026-09-18 15:00 CT → ~9 hours to midnight
    now = datetime(2026, 9, 18, 15, 0, 0, tzinfo=_CT)
    secs = seconds_until_chicago_midnight(now)
    assert 8 * 3600 < secs <= 9 * 3600


def test_backup_sqlite_helper_and_prune(tmp_path):
    src = tmp_path / "trading_bot.db"
    src.write_bytes(b"SQLite fake payload")
    ledger = tmp_path / "paper_ledger.db"
    ledger.write_bytes(b"ledger")
    bdir = tmp_path / "backups"
    when = datetime(2026, 9, 18, 12, 0, 0, tzinfo=_CT)
    written = backup_sqlite_dbs([src, ledger], bdir, keep=7, when=when)
    assert len(written) == 2
    assert all(p.exists() for p in written)
    ts = backup_timestamp(when)
    assert any(ts in p.name for p in written)

    # Create older extras and prune to keep=2
    for i in range(5):
        fake = bdir / f"extra_{i}.db"
        fake.write_text("x")
        # stagger mtimes
        os.utime(fake, (1_700_000_000 + i, 1_700_000_000 + i))
    # touch written so they are newest
    import time

    now = time.time()
    for p in written:
        os.utime(p, (now + 10, now + 10))
    deleted = prune_backups(bdir, keep=2)
    remaining = list(bdir.iterdir())
    assert len(remaining) == 2
    assert len(deleted) >= 5


@pytest.mark.asyncio
async def test_notify_critical_crash_sends_message():
    notifier = MagicMock()
    notifier.send = AsyncMock()
    await notify_critical_crash(notifier, ValueError("boom-test"))
    notifier.send.assert_awaited_once()
    args, kwargs = notifier.send.await_args
    assert "CRITICAL: Bot crashed due to ValueError" in args[0]
    assert "boom-test" in args[0]
    assert kwargs.get("high_priority") is True


@pytest.mark.asyncio
async def test_notify_critical_crash_noop_without_notifier():
    await notify_critical_crash(None, RuntimeError("x"))


@pytest.mark.asyncio
async def test_maintenance_once_logs_and_clears(tmp_path, monkeypatch, caplog):
    import logging

    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("BROKER", "mock")
    monkeypatch.setenv("PAPER_TRADING_MODE", "true")
    monkeypatch.setenv("TELEGRAM_COMMANDS_ENABLED", "false")
    monkeypatch.setenv("PERP_LEADLAG_ENABLED", "false")
    monkeypatch.setenv("FUNDING_OI_ENABLED", "false")
    monkeypatch.setenv("ONCHAIN_GUARDS_ENABLED", "false")
    monkeypatch.setenv("OPTIMIZER_ENABLED", "false")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("PAPER_BOOK_PATH", str(tmp_path / "paper_book.json"))
    reload_settings()
    from main import TradingApp
    from trading_bot.config import get_settings

    app = TradingApp(get_settings(), once=True)
    # seed caches
    app.feed._frames["BTC-USD"] = MagicMock()
    app.feed._l2_cache["BTC-USD"] = (1.0, 1.2)
    with caplog.at_level(logging.INFO):
        await app._run_maintenance_once()
    assert "MAINTENANCE: gc + stream buffer clear" in caplog.text
    assert app.feed._frames == {}
    assert app.feed._l2_cache == {}
