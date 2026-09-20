"""Unit tests for Telegram /set_threshold parse, validation, persist, and apply."""

from __future__ import annotations

import os

import pytest

from trading_bot.config import (
    ENV_KEY_ENTRY_THRESHOLD,
    Settings,
    apply_runtime_entry_threshold,
    upsert_env_keys,
)
from trading_bot.telegram_commands import (
    SET_THRESHOLD_USAGE,
    SetThresholdError,
    execute_set_threshold,
    format_set_threshold_reply,
    format_status_reply,
    parse_command,
    parse_set_threshold_args,
)
from trading_bot.utils import entry_proximity as ep
from trading_bot.utils.entry_proximity import (
    DEFAULT_ENTRY_THRESHOLD,
    get_entry_proximity,
    get_entry_threshold,
    set_entry_threshold,
)


def _settings(**kwargs) -> Settings:
    base = dict(
        PAPER_TRADING_MODE=True,
        ACCOUNT_EQUITY=1600.0,
        MAX_NOTIONAL_PER_TRADE_USD=100.0,
        MAX_TOTAL_EXPOSURE_USD=1000.0,
        ENTRY_THRESHOLD=60.0,
    )
    base.update(kwargs)
    return Settings(**base)


@pytest.fixture(autouse=True)
def _reset_threshold():
    """Keep module runtime threshold + env isolated across tests."""
    prev = os.environ.get("ENTRY_THRESHOLD")
    os.environ["ENTRY_THRESHOLD"] = "60"
    set_entry_threshold(DEFAULT_ENTRY_THRESHOLD)
    yield
    set_entry_threshold(DEFAULT_ENTRY_THRESHOLD)
    os.environ["ENTRY_THRESHOLD"] = "60"
    if prev is None:
        os.environ.pop("ENTRY_THRESHOLD", None)
    else:
        os.environ["ENTRY_THRESHOLD"] = prev


def test_parse_command_set_threshold():
    assert parse_command("/set_threshold 70") == ("set_threshold", ["70"])
    assert parse_command("/set_threshold@CruzBot 70%") == ("set_threshold", ["70%"])
    assert parse_command("/set_threshold") == ("set_threshold", [])


def test_parse_set_threshold_args_happy():
    assert parse_set_threshold_args(["70"]) == 70
    assert parse_set_threshold_args(["70%"]) == 70
    assert parse_set_threshold_args(["50"]) == 50
    assert parse_set_threshold_args(["35"]) == 35
    assert parse_set_threshold_args(["15"]) == 15
    assert parse_set_threshold_args(["custom", "42"]) == 42
    assert parse_set_threshold_args(["custom", "15%"]) == 15
    assert parse_set_threshold_args(["45"]) == 45
    assert parse_set_threshold_args(["95"]) == 95
    assert parse_set_threshold_args(["60.0"]) == 60


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["70", "extra"],
        ["abc"],
        ["14"],
        ["96"],
        ["-1"],
        ["70.5"],
        ["nan"],
        ["%"],
    ],
)
def test_parse_set_threshold_args_rejects(args):
    with pytest.raises(SetThresholdError) as exc:
        parse_set_threshold_args(args)
    msg = str(exc.value)
    assert "Usage: /set_threshold" in msg or "must be" in msg or "15" in msg or "custom" in msg or "35" in msg or "45" in msg or "50" in msg
    assert SET_THRESHOLD_USAGE in msg


def test_format_set_threshold_reply():
    assert (
        format_set_threshold_reply(70)
        == "✅ Entry threshold updated to 70%. "
        "Signals reaching or exceeding 70% proximity will now trigger paper trades."
    )


def test_execute_set_threshold_happy_path_persists_and_mutates(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("PAPER_TRADING_MODE=true\n", encoding="utf-8")
    settings = _settings()
    environ = {}
    reply = execute_set_threshold(
        settings, ["70"], env_path=env_path, environ=environ
    )
    assert "70%" in reply
    assert settings.entry_threshold == 70.0
    assert get_entry_threshold() == 70.0
    assert ep.LONG_THRESHOLD == 70.0
    text = env_path.read_text(encoding="utf-8")
    assert f"{ENV_KEY_ENTRY_THRESHOLD}=70" in text
    assert environ[ENV_KEY_ENTRY_THRESHOLD] == "70"


def test_execute_set_threshold_invalid_does_not_change_state(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("ENTRY_THRESHOLD=60\n", encoding="utf-8")
    settings = _settings(ENTRY_THRESHOLD=60.0)
    set_entry_threshold(60)
    environ = {"ENTRY_THRESHOLD": "60"}
    with pytest.raises(SetThresholdError):
        execute_set_threshold(settings, ["10"], env_path=env_path, environ=environ)
    assert settings.entry_threshold == 60.0
    assert get_entry_threshold() == 60.0
    assert "ENTRY_THRESHOLD=60" in env_path.read_text(encoding="utf-8")


def test_runtime_threshold_affects_long_wait_and_status():
    strong = dict(
        close=100.0,
        vwap=100.0,
        extras={"retest_ok": True, "breakout_volume": 1000.0, "pullback_volume": 100.0},
    )
    set_entry_threshold(60)
    prox60 = get_entry_proximity(**strong)
    assert prox60["score"] >= 60
    assert prox60["direction"] == "LONG"

    set_entry_threshold(90)
    prox90 = get_entry_proximity(**strong)
    assert prox90["score"] == prox60["score"]
    assert prox90["direction"] == "WAIT"

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
        entry_proximity=prox90,
        entry_threshold=90,
    )
    assert "Threshold: 90%" in text
    assert "Target Setup: WAIT" in text


def test_status_includes_threshold_line_default():
    text = format_status_reply(
        paper_cash=1600.0,
        paper_equity=1600.0,
        positions=[],
        paused=False,
        strategy_mode="volume_sweet_spot",
        last_tick_age_seconds=None,
        paper=True,
        symbols=["BTC-USD"],
        max_notional_per_trade=100.0,
        max_total_exposure=1000.0,
        target_setup="WAIT",
    )
    assert "Threshold: 60%" in text
    lines = text.splitlines()
    caps_i = next(i for i, ln in enumerate(lines) if ln.startswith("caps="))
    thr_i = next(i for i, ln in enumerate(lines) if ln.startswith("Threshold:"))
    setup_i = next(i for i, ln in enumerate(lines) if ln.startswith("Target Setup:"))
    assert caps_i < thr_i < setup_i


def test_apply_runtime_entry_threshold_syncs_settings_and_module():
    settings = _settings()
    apply_runtime_entry_threshold(settings, 75)
    assert settings.entry_threshold == 75.0
    assert get_entry_threshold() == 75.0


@pytest.mark.asyncio
async def test_cmd_set_threshold_happy_and_error(tmp_path, monkeypatch):
    os.environ["DRY_RUN"] = "true"
    os.environ["BROKER"] = "mock"
    os.environ["PAPER_TRADING_MODE"] = "true"
    os.environ["TELEGRAM_COMMANDS_ENABLED"] = "false"
    os.environ["SQLITE_PATH"] = str(tmp_path / "set_threshold.db")
    os.environ["LOG_LEVEL"] = "WARNING"
    os.environ["ENTRY_THRESHOLD"] = "60"

    from trading_bot.config import reload_settings
    from main import TradingApp

    monkeypatch.chdir(tmp_path)
    # Point PROJECT_ROOT .env via writing in cwd — TradingApp uses PROJECT_ROOT/.env
    # so use real project env_path; execute path is tested above. Here exercise handler.
    settings = reload_settings()
    app = TradingApp(settings, once=True)
    # Redirect env persist to tmp
    from trading_bot import config as cfg

    monkeypatch.setattr(cfg, "PROJECT_ROOT", tmp_path)
    # Also patch main.PROJECT_ROOT used by handler
    import main as main_mod

    monkeypatch.setattr(main_mod, "PROJECT_ROOT", tmp_path)
    (tmp_path / ".env").write_text("ENTRY_THRESHOLD=60\n", encoding="utf-8")

    bad = await app._cmd_set_threshold("set_threshold", ["10"])
    assert "35" in bad or "Usage" in bad or "must be" in bad
    assert get_entry_threshold() == 60.0

    reply = await app._cmd_set_threshold("set_threshold", ["70"])
    assert "70%" in reply
    assert get_entry_threshold() == 70.0
    assert app.settings.entry_threshold == 70.0
    assert "ENTRY_THRESHOLD=70" in (tmp_path / ".env").read_text(encoding="utf-8")


def test_sweet_spot_entry_gate_respects_threshold():
    from trading_bot.models import Action, AgentObservation, IndicatorSnapshot
    from trading_bot.strategy_volume_sweet_spot import VolumeSweetSpotEngine

    ind = IndicatorSnapshot(
        symbol="ETH-USD",
        close=2500.0,
        vwap=2500.0,
        volume=100.0,
        rsi=55.0,
        macd=1.0,
        macd_signal=0.5,
        macd_hist=0.4,
        ema_fast=2498.0,
        ema_slow=2490.0,
        atr=10.0,
        extras={
            "retest_ok": True,
            "breakout_volume": 1000.0,
            "pullback_volume": 100.0,
            "swing_low": 2400.0,
            "resistance": 2700.0,
            "volume_ratio": 2.5,
            "breakout_rvol": 2.5,
        },
    )
    obs = AgentObservation(symbol="ETH-USD", indicators=ind)
    engine = VolumeSweetSpotEngine(swing_sl_buffer_pct=0.002, min_tp_pct=0.025, tp2_rr=2.5)

    set_entry_threshold(60)
    d_ok = engine.reason(obs)
    assert d_ok.action == Action.BUY

    set_entry_threshold(90)
    d_hold = engine.reason(obs)
    assert d_hold.action == Action.HOLD
    assert "proximity" in d_hold.reasoning.lower()
    assert "90" in d_hold.reasoning
