"""Focused tests: normalize, volume/spread, mode toggle, friendly labels."""
from trading_bot.config import Settings, set_runtime_active_symbols, DEFAULT_SYMBOL_ALLOWLIST
from trading_bot.symbol_universe import (
    normalize_to_base_usd,
    filter_by_volume_spread,
    pair_is_active_spot_usd,
    SymbolUniverse,
)
from trading_bot.telegram_commands import (
    format_universe_line,
    format_universe_switched,
    parse_universe_args,
    format_status_reply,
)


def test_normalize():
    assert normalize_to_base_usd("XXBTZUSD") == "BTC-USD"
    assert normalize_to_base_usd("ETH/USD") == "ETH-USD"
    assert normalize_to_base_usd("ETHUSD") == "ETH-USD"
    assert normalize_to_base_usd("XETHZEUR") is None


def test_volume_spread():
    ok, vol, sp = filter_by_volume_spread(
        {"v": ["0", "100"], "p": ["0", "6000"], "a": ["100.5"], "b": ["100.0"]}
    )
    assert ok and vol == 600000
    ok2, _, _ = filter_by_volume_spread(
        {"v": ["0", "1"], "p": ["0", "100"], "a": ["101"], "b": ["100"]}
    )
    assert not ok2


def test_pair_filter():
    assert pair_is_active_spot_usd(
        {"quote": "ZUSD", "status": "online", "base": "SOL", "altname": "SOLUSD"}
    )
    assert not pair_is_active_spot_usd(
        {"quote": "ZUSD", "status": "halted", "base": "SOL", "altname": "SOLUSD"}
    )


def test_mode_toggle(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("SYMBOL_MODE=ALLOWLIST\n")
    monkeypatch.setattr("trading_bot.symbol_universe.PROJECT_ROOT", tmp_path)
    s = Settings()
    object.__setattr__(s, "symbol_mode", "ALLOWLIST")
    uni = SymbolUniverse(s)
    uni.set_mode("DYNAMIC_ALL", persist=True)
    assert uni.mode == "DYNAMIC_ALL" and "DYNAMIC_ALL" in env.read_text()
    set_runtime_active_symbols(["BTC-USD", "AAVE-USD"])
    assert s.is_allowlisted("AAVE-USD")
    uni.set_mode("ALLOWLIST", persist=True)
    assert not s.is_allowlisted("AAVE-USD")


def test_friendly_labels():
    assert format_universe_line("ALLOWLIST", 14) == "Universe: Allow list (14)"
    assert format_universe_line("DYNAMIC_ALL", 87) == "Universe: Kraken discovery (87)"
    assert "Allow list" in format_universe_switched(mode="ALLOWLIST", count=14)
    assert "Kraken discovery" in format_universe_switched(
        mode="DYNAMIC_ALL", count=50, refreshing=True
    )
    assert parse_universe_args(["all"]) == "DYNAMIC_ALL"
    assert parse_universe_args(["allowlist"]) == "ALLOWLIST"
    txt = format_status_reply(
        paper_cash=1,
        paper_equity=1,
        positions=[],
        paused=False,
        strategy_mode="vss",
        last_tick_age_seconds=1,
        symbols=list(DEFAULT_SYMBOL_ALLOWLIST),
        symbol_mode="ALLOWLIST",
    )
    assert "Universe: Allow list (14)" in txt
