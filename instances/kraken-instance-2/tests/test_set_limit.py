"""Unit tests for Telegram /set_limit parse, validation, persist, and apply."""

from __future__ import annotations

import os

import pytest

from trading_bot.config import (
    ENV_KEY_MAX_BOOK,
    ENV_KEY_TRADE_CAP,
    Settings,
    apply_runtime_trade_caps,
    format_env_float,
    upsert_env_keys,
)
from trading_bot.risk_manager import RiskManager
from trading_bot.telegram_commands import (
    SET_LIMIT_USAGE,
    SetLimitError,
    execute_set_limit,
    format_set_limit_reply,
    parse_command,
    parse_set_limit_args,
)


def _settings(**kwargs) -> Settings:
    base = dict(
        PAPER_TRADING_MODE=True,
        ACCOUNT_EQUITY=1600.0,
        MAX_NOTIONAL_PER_TRADE_USD=100.0,
        MAX_TOTAL_EXPOSURE_USD=1000.0,
    )
    base.update(kwargs)
    return Settings(**base)


def test_parse_command_set_limit():
    assert parse_command("/set_limit 100 400") == ("set_limit", ["100", "400"])
    assert parse_command("/set_limit") == ("set_limit", [])


def test_parse_set_limit_args_happy():
    assert parse_set_limit_args(["100", "400"]) == (100.0, 400.0)
    assert parse_set_limit_args(["100.5", "400.25"]) == (100.5, 400.25)
    assert parse_set_limit_args(["50", "50"]) == (50.0, 50.0)


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["100"],
        ["100", "400", "extra"],
        ["abc", "400"],
        ["100", "nope"],
        ["-10", "400"],
        ["0", "400"],
        ["100", "0"],
        ["100", "-1"],
        ["200", "100"],
        ["nan", "400"],
    ],
)
def test_parse_set_limit_args_rejects(args):
    with pytest.raises(SetLimitError) as exc:
        parse_set_limit_args(args)
    msg = str(exc.value)
    assert "Usage: /set_limit" in msg or "must be" in msg or "max_book" in msg
    assert SET_LIMIT_USAGE in msg


def test_parse_set_limit_args_max_book_below_trade_cap_message():
    with pytest.raises(SetLimitError) as exc:
        parse_set_limit_args(["200", "100"])
    assert "must be >= trade_cap" in str(exc.value)


def test_format_set_limit_reply_two_decimals():
    assert (
        format_set_limit_reply(100, 400)
        == "Trade cap updated to $100.00 | Max book updated to $400.00"
    )
    assert (
        format_set_limit_reply(12.5, 40)
        == "Trade cap updated to $12.50 | Max book updated to $40.00"
    )


def test_upsert_env_keys_creates_missing_file(tmp_path):
    env_path = tmp_path / ".env"
    upsert_env_keys(env_path, {ENV_KEY_TRADE_CAP: "100", ENV_KEY_MAX_BOOK: "400"})
    text = env_path.read_text(encoding="utf-8")
    assert f"{ENV_KEY_TRADE_CAP}=100" in text
    assert f"{ENV_KEY_MAX_BOOK}=400" in text


def test_upsert_env_keys_preserves_other_keys_and_comments(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# keep this comment\n"
        "PAPER_TRADING_MODE=true\n"
        f"{ENV_KEY_TRADE_CAP}=50\n"
        f"# {ENV_KEY_TRADE_CAP}=999\n"
        f"export {ENV_KEY_MAX_BOOK}=200\n"
        "OTHER=keep-me\n",
        encoding="utf-8",
    )
    upsert_env_keys(env_path, {ENV_KEY_TRADE_CAP: "100", ENV_KEY_MAX_BOOK: "400"})
    text = env_path.read_text(encoding="utf-8")
    assert "PAPER_TRADING_MODE=true" in text
    assert "OTHER=keep-me" in text
    assert "# keep this comment" in text
    assert f"# {ENV_KEY_TRADE_CAP}=999" in text
    assert f"{ENV_KEY_TRADE_CAP}=100" in text
    assert "MAX_NOTIONAL_PER_TRADE_USD=50" not in text
    assert f"export {ENV_KEY_MAX_BOOK}=400" in text
    assert "export MAX_TOTAL_EXPOSURE_USD=200" not in text


def test_upsert_env_keys_appends_when_keys_missing(tmp_path):
    env_path = tmp_path / "subdir" / ".env"
    env_path.parent.mkdir()
    env_path.write_text("FOO=bar\n", encoding="utf-8")
    upsert_env_keys(env_path, {ENV_KEY_TRADE_CAP: "75"})
    text = env_path.read_text(encoding="utf-8")
    assert "FOO=bar" in text
    assert f"{ENV_KEY_TRADE_CAP}=75" in text


def test_format_env_float_compact():
    assert format_env_float(100.0) == "100"
    assert format_env_float(100.50) == "100.5"
    assert format_env_float(12.25) == "12.25"


def test_apply_runtime_trade_caps_mutates_shared_settings():
    settings = _settings()
    rm = RiskManager(settings)
    paper_before = settings.paper_trading_mode
    apply_runtime_trade_caps(settings, 100.0, 400.0)
    assert settings.max_notional_per_trade_usd == 100.0
    assert settings.max_total_exposure_usd == 400.0
    assert rm.settings is settings
    assert rm.settings.max_notional_per_trade_usd == 100.0
    assert rm.settings.max_total_exposure_usd == 400.0
    assert settings.paper_trading_mode is paper_before


def test_execute_set_limit_happy_path_persists_and_mutates(tmp_path):
    settings = _settings()
    rm = RiskManager(settings)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "PAPER_TRADING_MODE=true\n"
        f"{ENV_KEY_TRADE_CAP}=50\n"
        f"{ENV_KEY_MAX_BOOK}=200\n"
        "SECRET=do-not-wipe\n",
        encoding="utf-8",
    )
    environ: dict[str, str] = {}
    reply = execute_set_limit(settings, ["100", "400"], env_path=env_path, environ=environ)
    assert reply == "Trade cap updated to $100.00 | Max book updated to $400.00"
    assert settings.max_notional_per_trade_usd == 100.0
    assert settings.max_total_exposure_usd == 400.0
    assert rm.settings.max_notional_per_trade_usd == 100.0
    assert settings.paper_trading_mode is True
    text = env_path.read_text(encoding="utf-8")
    assert "SECRET=do-not-wipe" in text
    assert "PAPER_TRADING_MODE=true" in text
    assert f"{ENV_KEY_TRADE_CAP}=100" in text
    assert f"{ENV_KEY_MAX_BOOK}=400" in text
    assert environ[ENV_KEY_TRADE_CAP] == "100"
    assert environ[ENV_KEY_MAX_BOOK] == "400"


def test_execute_set_limit_invalid_does_not_change_state(tmp_path):
    settings = _settings()
    env_path = tmp_path / ".env"
    original = (
        f"{ENV_KEY_TRADE_CAP}=50\n"
        f"{ENV_KEY_MAX_BOOK}=200\n"
        "OTHER=keep\n"
    )
    env_path.write_text(original, encoding="utf-8")
    environ: dict[str, str] = {}
    with pytest.raises(SetLimitError):
        execute_set_limit(settings, ["200", "100"], env_path=env_path, environ=environ)
    assert settings.max_notional_per_trade_usd == 100.0
    assert settings.max_total_exposure_usd == 1000.0
    assert env_path.read_text(encoding="utf-8") == original
    assert environ == {}


def test_execute_set_limit_creates_env_when_missing(tmp_path):
    settings = _settings()
    env_path = tmp_path / "missing.env"
    reply = execute_set_limit(
        settings, ["80", "160"], env_path=env_path, environ={}
    )
    assert "80.00" in reply
    assert env_path.exists()
    text = env_path.read_text(encoding="utf-8")
    assert f"{ENV_KEY_TRADE_CAP}=80" in text
    assert f"{ENV_KEY_MAX_BOOK}=160" in text


@pytest.mark.asyncio
async def test_cmd_set_limit_happy_and_error(tmp_path, monkeypatch):
    os.environ["DRY_RUN"] = "true"
    os.environ["BROKER"] = "mock"
    os.environ["PAPER_TRADING_MODE"] = "true"
    os.environ["TELEGRAM_COMMANDS_ENABLED"] = "false"
    os.environ["SYMBOLS"] = "BTC-USD"
    os.environ["SQLITE_PATH"] = str(tmp_path / "set_limit.db")
    os.environ["LOG_LEVEL"] = "WARNING"
    os.environ[ENV_KEY_TRADE_CAP] = "50"
    os.environ[ENV_KEY_MAX_BOOK] = "200"

    import main as main_mod
    from trading_bot.config import reload_settings

    monkeypatch.setattr(main_mod, "PROJECT_ROOT", tmp_path)
    (tmp_path / ".env").write_text(
        "PAPER_TRADING_MODE=true\nOTHER=keep\n",
        encoding="utf-8",
    )

    settings = reload_settings()
    app = main_mod.TradingApp(settings, once=True)
    paper_before = bool(app.settings.paper_trading_mode)
    cap_before = float(app.settings.max_notional_per_trade_usd)

    bad = await app._cmd_set_limit("set_limit", ["200", "50"])
    assert SET_LIMIT_USAGE in bad
    assert float(app.settings.max_notional_per_trade_usd) == cap_before
    assert float(app.risk.settings.max_notional_per_trade_usd) == cap_before
    assert app.settings.paper_trading_mode is paper_before

    reply = await app._cmd_set_limit("set_limit", ["100", "400"])
    assert reply == "Trade cap updated to $100.00 | Max book updated to $400.00"
    assert float(app.settings.max_notional_per_trade_usd) == 100.0
    assert float(app.settings.max_total_exposure_usd) == 400.0
    assert app.risk.settings is app.settings
    assert float(app.risk.settings.max_notional_per_trade_usd) == 100.0
    assert app.settings.paper_trading_mode is paper_before
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "OTHER=keep" in text
    assert f"{ENV_KEY_TRADE_CAP}=100" in text
    assert f"{ENV_KEY_MAX_BOOK}=400" in text
    assert "PAPER_TRADING_MODE=true" in text


def test_execute_set_limit_persist_failure_leaves_caps_unchanged(tmp_path, monkeypatch):
    settings = _settings()
    env_path = tmp_path / ".env"
    env_path.write_text(f"{ENV_KEY_TRADE_CAP}=50\n", encoding="utf-8")

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr("trading_bot.config.upsert_env_keys", _boom)
    with pytest.raises(OSError, match="disk full"):
        execute_set_limit(settings, ["100", "400"], env_path=env_path, environ={})
    assert settings.max_notional_per_trade_usd == 100.0
    assert settings.max_total_exposure_usd == 1000.0
