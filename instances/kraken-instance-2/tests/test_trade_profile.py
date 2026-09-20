"""Tests for the exact Telegram trade-profile presets."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from trading_bot.telegram_commands import (
    KNOWN_COMMANDS,
    TRADE_PROFILE_PRESETS,
    TradeProfileError,
    execute_set_profile,
    format_help_reply,
    format_set_profile_reply,
    format_status_reply,
    normalize_trade_profile,
    parse_command,
    parse_profile_args,
)


EXPECTED = {
    "aggressive": (35, 0.005, True),
    "medium": (65, 0.0025, False),
    "low": (80, 0.0015, False),
}
REPLIES = {
    "aggressive": "⚡ PROFILE: AGGRESSIVE ARMED — Threshold 35% | Spread 0.5% | TOD Gate Disabled | RVOL 1.0x | Auto paper-buy on proximity. Caps $1000/$1500 | 3 concurrent.",
    "medium": "⚖️ PROFILE: MEDIUM ARMED — Threshold 65% | Spread 0.25% | TOD Gate Enabled. Balanced intraday volume strategy.",
    "low": "🛡️ PROFILE: LOW ARMED — Threshold 80% | Spread 0.15% | TOD Gate Enabled. Selective mode preserved.",
}


def test_known_commands_include_profiles():
    assert {"aggressive", "medium", "low", "profile"} <= set(KNOWN_COMMANDS)


@pytest.mark.parametrize("text,cmd", [
    ("/aggressive", "aggressive"),
    ("/medium", "medium"),
    ("/low", "low"),
    ("/profile aggressive", "profile"),
    ("/profile@CruzBot medium", "profile"),
])
def test_parse_profile_commands(text, cmd):
    parsed = parse_command(text)
    assert parsed is not None and parsed[0] == cmd


def test_help_lists_exact_profile_numbers():
    text = format_help_reply()
    assert "/aggressive" in text and "35%" in text and "0.5%" in text
    assert "/medium" in text and "65%" in text and "0.25%" in text
    assert "/low" in text and "80%" in text and "0.15%" in text


@pytest.mark.parametrize("raw,expected", [
    ("aggressive", "aggressive"), ("AGG", "aggressive"),
    ("medium", "medium"), ("med", "medium"),
    ("low", "low"), ("preserve", "low"), ("conservative", "low"),
])
def test_normalize_trade_profile(raw, expected):
    assert normalize_trade_profile(raw) == expected


def test_parse_profile_args_rejects_invalid():
    assert parse_profile_args(["aggressive"]) == "aggressive"
    with pytest.raises(TradeProfileError):
        parse_profile_args([])
    with pytest.raises(TradeProfileError):
        parse_profile_args(["turbo"])


@pytest.mark.parametrize("name", ["aggressive", "medium", "low"])
def test_preset_values_and_exact_reply(name):
    threshold, spread, disable_tod = EXPECTED[name]
    knobs = TRADE_PROFILE_PRESETS[name]
    assert knobs["entry_threshold"] == threshold
    assert knobs["max_spread_pct"] == pytest.approx(spread)
    assert knobs["disable_tod_gate"] is disable_tod
    assert format_set_profile_reply(name, knobs) == REPLIES[name]


def test_format_status_shows_uppercase_profile():
    text = format_status_reply(
        paper_cash=1600.0, paper_equity=1600.0, positions=[], paused=False,
        strategy_mode="volume_sweet_spot", last_tick_age_seconds=1.0,
        paper=True, entry_threshold=65.0, max_spread_pct=0.0025,
        trade_profile="medium",
    )
    assert "Profile: MEDIUM" in text
    assert "Threshold: 65%" in text
    assert "Spread cap: 0.25%" in text


def _settings():
    return SimpleNamespace(
        entry_threshold=65.0, max_spread_pct=0.0025,
        tod_gate_enabled=True, disable_tod_gate=False,
        trade_profile="medium", phase1_fail_closed=True, post_only=True,
        rvol_breakout_mult=2.0, prefilter_volume_spike_mult=2.75,
        max_concurrent_positions=2, agent_poll_seconds=30.0,
        bar_timeframe="1Min",
        max_notional_per_trade_usd=1000.0, max_total_exposure_usd=1500.0,
    )


@pytest.mark.parametrize("name", ["aggressive", "medium", "low"])
def test_execute_set_profile_persists_exact_keys(name, tmp_path):
    env_path = tmp_path / ".env"
    settings = _settings()
    engine = SimpleNamespace(tod_gate_enabled=True, disable_tod_gate=False)
    environ: dict[str, str] = {}
    execute_set_profile(settings, name, env_path=env_path, environ=environ, signal_engine=engine)
    threshold, spread, disable_tod = EXPECTED[name]
    assert settings.trade_profile == name
    assert settings.entry_threshold == threshold
    assert settings.max_spread_pct == pytest.approx(spread)
    assert settings.disable_tod_gate is disable_tod
    assert settings.tod_gate_enabled is (not disable_tod)
    assert engine.disable_tod_gate is disable_tod
    assert engine.tod_gate_enabled is (not disable_tod)
    text = env_path.read_text(encoding="utf-8")
    assert f"TRADE_PROFILE={name}" in text
    assert f"ENTRY_THRESHOLD={threshold}" in text
    assert f"MAX_SPREAD_PCT={spread}" in text
    assert f"DISABLE_TOD_GATE={'true' if disable_tod else 'false'}" in text
    assert environ["DISABLE_TOD_GATE"] == ("true" if disable_tod else "false")
    if name == "aggressive":
        assert settings.rvol_breakout_mult == pytest.approx(1.0)
        assert settings.prefilter_volume_spike_mult == pytest.approx(1.0)
        assert settings.max_concurrent_positions == 3
        assert settings.agent_poll_seconds == pytest.approx(12.0)
        assert settings.max_notional_per_trade_usd == pytest.approx(1000.0)
        assert settings.max_total_exposure_usd == pytest.approx(1500.0)
        assert "RVOL_BREAKOUT_MULT=1" in text
        assert "MAX_NOTIONAL_PER_TRADE_USD=1000" in text
        assert "MAX_TOTAL_EXPOSURE_USD=1500" in text
        assert "MAX_CONCURRENT_POSITIONS=3" in text
