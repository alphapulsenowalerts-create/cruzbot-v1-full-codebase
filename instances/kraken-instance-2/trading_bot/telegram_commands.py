"""Interactive Telegram command long-poll (authorized chat only).

User-initiated replies only — never unsolicited status spam.
Commands: /status /pause /resume /pnl /kill /mode /confirm_live /set_limit /set_threshold /set_spread /tod_custom /stop_loss /set /aggressive /medium /low /profile /test_trade /reset_paper /wipe_paper /factory_reset /ping /positions /balance /history /grok /regime /logs /universe /symbols /help
"""

from __future__ import annotations

import fcntl
import logging
import os
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, MutableMapping, Optional, Sequence, Union
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
from trading_bot.notifier import _fmt_price as _tg_fmt_price, _fmt_qty as _tg_fmt_qty

_CT = ZoneInfo("America/Chicago")

STATUS_SYMBOLS_CALLBACK = "status:symbols"
STATUS_SYMBOLS_COLLAPSE = "status:symbols_collapse"


@dataclass
class TelegramReply:
    """Optional rich Telegram reply (inline keyboard / parse_mode)."""

    text: str
    reply_markup: Optional[Dict[str, Any]] = None
    parse_mode: Optional[str] = None


CommandHandler = Callable[[str, List[str]], Awaitable[Union[str, TelegramReply, None]]]
CallbackHandler = Callable[[str], Awaitable[Union[str, TelegramReply, None]]]

KNOWN_COMMANDS = frozenset(
    {
        "status",
        "pause",
        "resume",
        "pnl",
        "kill",
        "mode",
        "confirm_live",
        "set_limit",
        "set_threshold",
        "set_threshold_custom",
        "set_spread",
        "tod_custom",
        "stop_loss",
        "winning_formula",
        "weekly_digest_101",
        "circuity_breaker_manually",
        "set",
        "aggressive",
        "medium",
        "low",
        "profile",
        "test_trade",
        "reset_paper",
        "wipe_paper",
        "factory_reset",
        "ping",
        "positions",
        "balance",
        "history",
        "grok",
        "regime",
        "logs",
        "universe",
        "universe_all",
        "universe_stocks",
        "symbols",
        "close",
        "clear_positions",
        "help",
    }
)


def normalize_symbol_mode(raw: Any) -> str:
    """Return ALLOWLIST, DYNAMIC_ALL, or OFF (stocks-only)."""
    m = str(raw or "ALLOWLIST").strip().upper().replace("-", "_")
    if m in ("OFF", "NONE", "CRYPTO_OFF", "STOCKS_ONLY"):
        return "OFF"
    if m in ("DYNAMIC", "DYNAMIC_ALL", "ALL", "KRAKEN", "DISCOVERY"):
        return "DYNAMIC_ALL"
    return "ALLOWLIST"


def universe_friendly_label(mode: Any) -> str:
    """User-facing universe label (not internal enum jargon)."""
    normalized = normalize_symbol_mode(mode)
    if normalized == "DYNAMIC_ALL":
        return "Kraken discovery"
    if normalized == "OFF":
        return "Stocks only"
    return "Allow list"


def format_universe_line(mode: Any, count: int, *, stocks_enabled: Optional[bool] = None, stocks_count: Optional[int] = None) -> str:
    """Format the universe line, optionally including xStocks state."""
    base = f"Universe: {universe_friendly_label(mode)} ({int(count)})"
    if stocks_enabled is None:
        return base
    if stocks_enabled:
        return f"{base} | Stocks: ON ({int(stocks_count or 0)})"
    return f"{base} | Stocks: OFF"


UNIVERSE_USAGE = "Usage: /universe [all|allowlist|off]"


def parse_universe_args(args: Sequence[str]) -> Optional[str]:
    """None = show status; DYNAMIC_ALL / ALLOWLIST to switch. Raises ValueError."""
    if not args:
        return None
    if len(args) != 1:
        raise ValueError(UNIVERSE_USAGE)
    raw = str(args[0]).strip().lower()
    if raw in ("all", "dynamic", "dynamic_all", "discovery", "kraken"):
        return "DYNAMIC_ALL"
    if raw in ("allowlist", "allow", "hard", "default", "list"):
        return "ALLOWLIST"
    if raw in ("off", "none", "crypto_off", "stocks_only", "stocks-only"):
        return "OFF"
    raise ValueError(UNIVERSE_USAGE)


UNIVERSE_STOCKS_USAGE = "Usage: /universe_stocks [on|off|toggle]"


def parse_universe_stocks_args(args: Sequence[str]) -> Optional[bool]:
    """Return desired state, None for status, or raise ValueError."""
    if not args:
        return None
    if len(args) != 1:
        raise ValueError(UNIVERSE_STOCKS_USAGE)
    raw = str(args[0]).strip().lower()
    if raw in ("on", "enable", "enabled", "true", "1"):
        return True
    if raw in ("off", "disable", "disabled", "false", "0"):
        return False
    if raw in ("toggle", "flip"):
        return None
    raise ValueError(UNIVERSE_STOCKS_USAGE)



TOD_USAGE = "Usage: /tod_custom [on|off|toggle] — TOD gate (locks vs /aggressive|/medium|/low)"


def parse_tod_args(args: Sequence[str]) -> Optional[bool]:
    """Return desired TOD-enabled state, None for status-only, raise on bad args.

    on  → gate enabled (block entries in TOD blackout)
    off → gate disabled (entries allowed any time)
    """
    if not args:
        return None
    if len(args) != 1:
        raise ValueError(TOD_USAGE)
    raw = str(args[0]).strip().lower()
    if raw in ("on", "enable", "enabled", "true", "1"):
        return True
    if raw in ("off", "disable", "disabled", "false", "0"):
        return False
    if raw in ("toggle", "flip"):
        return None  # caller flips
    raise ValueError(TOD_USAGE)


def format_tod_status(*, enabled: bool, locked: bool = True) -> str:
    base = (
        "TOD gate: ON — entries blocked during time-of-day blackout."
        if enabled
        else "TOD gate: OFF — entries allowed any time (blackout bypassed)."
    )
    if locked:
        base += " 🔒 Locked — /aggressive /medium /low will not change TOD."
    return base


def execute_set_tod(
    settings: Any,
    *,
    enabled: bool,
    env_path: Path,
    environ: Optional[MutableMapping[str, str]] = None,
    signal_engine: Any = None,
    custom_lock: bool = True,
) -> str:
    """Persist + hot-apply TOD gate. /tod_custom always locks vs profiles."""
    from pathlib import Path as _Path
    from trading_bot.config import (
        ENV_KEY_DISABLE_TOD_GATE,
        ENV_KEY_TOD_GATE_ENABLED,
        ENV_KEY_TOD_CUSTOM_LOCK,
        upsert_env_keys,
    )

    disable = not bool(enabled)
    updates = {
        ENV_KEY_DISABLE_TOD_GATE: "true" if disable else "false",
        ENV_KEY_TOD_GATE_ENABLED: "true" if enabled else "false",
        ENV_KEY_TOD_CUSTOM_LOCK: "true" if custom_lock else "false",
    }
    upsert_env_keys(_Path(env_path), updates)
    env_map = os.environ if environ is None else environ
    for k, v in updates.items():
        env_map[k] = v
    object.__setattr__(settings, "disable_tod_gate", disable)
    object.__setattr__(settings, "tod_gate_enabled", bool(enabled))
    try:
        object.__setattr__(settings, "tod_custom_lock", bool(custom_lock))
    except Exception:
        pass
    if signal_engine is not None:
        if hasattr(signal_engine, "disable_tod_gate"):
            signal_engine.disable_tod_gate = disable
        if hasattr(signal_engine, "tod_gate_enabled"):
            signal_engine.tod_gate_enabled = bool(enabled)
    return format_tod_status(enabled=bool(enabled), locked=bool(custom_lock))



STOP_LOSS_USAGE = "Usage: /stop_loss <tight | medium | free>"

STOP_LOSS_PRESETS: Dict[str, Dict[str, Any]] = {
    "tight": {
        "sl_pct": 0.0075,
        "sl_min_pct": 0.0075,
        "sl_max_pct": 0.0075,
        "tp_pct": 0.0115,
        "atr_mult": 1.0,
        "emoji": "🔴",
        "note": "Capital Preservation Active",
        "label": "TIGHT",
    },
    "medium": {
        "sl_pct": 0.015,
        "sl_min_pct": 0.015,
        "sl_max_pct": 0.015,
        "tp_pct": 0.0225,
        "atr_mult": 1.5,
        "emoji": "🟡",
        "note": "Standard Room",
        "label": "MEDIUM",
    },
    "free": {
        "sl_pct": 0.025,
        "sl_min_pct": 0.025,
        "sl_max_pct": 0.030,
        "tp_pct": 0.0375,
        "atr_mult": 2.5,
        "emoji": "🟢",
        "note": "Wide Swing Room",
        "label": "FREE",
    },
}


def normalize_stop_loss_profile(raw: Any) -> str:
    n = str(raw or "").strip().lower()
    if n in ("loose", "wide", "free"):
        return "free"
    if n in ("tight", "t", "red"):
        return "tight"
    if n in ("medium", "med", "m", "yellow", "bal", "balanced"):
        return "medium"
    raise ValueError(STOP_LOSS_USAGE)


def parse_stop_loss_args(args: Sequence[str]) -> Optional[str]:
    """Return profile name, or None for status-only."""
    if not args:
        return None
    if len(args) != 1:
        raise ValueError(STOP_LOSS_USAGE)
    return normalize_stop_loss_profile(args[0])


def format_stop_loss_status(profile: str, *, strategy: str = "") -> str:
    try:
        name = normalize_stop_loss_profile(profile)
    except ValueError:
        name = "medium"
    p = STOP_LOSS_PRESETS[name]
    strat = (strategy or "").strip() or "volume_sweet_spot"
    return (
        f"{p['emoji']} Stop Loss: {p['label']} (−{p['sl_pct']*100:.2f}% / TP +{p['tp_pct']*100:.2f}% / {p['atr_mult']:.1f}x ATR)\n"
        f"• {p['note']}\n"
        f"• Strategy: {strat}\n"
        f"{STOP_LOSS_USAGE}"
    )


def format_stop_loss_status_line(
    profile: str,
    *,
    effective_sl_pct: float | None = None,
    clamped: bool = False,
) -> str:
    """Compact /status line under Profile (optional WF BEAR effective %)."""
    try:
        name = normalize_stop_loss_profile(profile)
    except ValueError:
        name = "medium"
    p = STOP_LOSS_PRESETS[name]
    sl = float(effective_sl_pct) if effective_sl_pct is not None else float(p["sl_pct"])
    note = " (WF BEAR clamp)" if clamped else ""
    return f"stop_loss: {p['emoji']} {p['label']} −{sl * 100:.2f}%{note}"


def format_stop_loss_updated(profile: str, *, strategy: str = "") -> str:
    name = normalize_stop_loss_profile(profile)
    p = STOP_LOSS_PRESETS[name]
    strat = (strategy or "").strip() or "volume_sweet_spot"
    return (
        "✅ Stop Loss profile updated to: {label} {emoji}\n"
        "• Distance: SL −{sl:.2f}% / TP +{tp:.2f}% / {atr:.1f}x ATR\n"
        "• Active Strategy: {strat}\n"
        "• Mode Note: {note}"
    ).format(
        label=p["label"],
        emoji=p["emoji"],
        sl=p["sl_pct"] * 100,
        tp=p["tp_pct"] * 100,
        atr=p["atr_mult"],
        strat=strat,
        note=p["note"],
    )



def stop_loss_bracket_prices(entry: float, profile: str, *, short: bool = False) -> tuple[float, float]:
    """Return (stop_loss, take_profit) from profile pcts."""
    name = normalize_stop_loss_profile(profile)
    p = STOP_LOSS_PRESETS[name]
    e = float(entry)
    sl_pct = float(p["sl_pct"])
    tp_pct = float(p["tp_pct"])
    if short:
        return e * (1.0 + sl_pct), e * (1.0 - tp_pct)
    return e * (1.0 - sl_pct), e * (1.0 + tp_pct)


def execute_set_stop_loss(
    settings: Any,
    profile: str,
    *,
    env_path: Path,
    environ: Optional[MutableMapping[str, str]] = None,
    signal_engine: Any = None,
) -> str:
    """Persist STOP_LOSS_PROFILE + SL_MIN/MAX + ATR mult; hot-apply."""
    from pathlib import Path as _Path
    from trading_bot.config import (
        ENV_KEY_STOP_LOSS_PROFILE,
        ENV_KEY_SL_MIN_PCT,
        ENV_KEY_SL_MAX_PCT,
        ENV_KEY_ELITE_ATR_SL_MULT,
        format_env_float,
        upsert_env_keys,
    )

    name = normalize_stop_loss_profile(profile)
    p = STOP_LOSS_PRESETS[name]
    updates = {
        ENV_KEY_STOP_LOSS_PROFILE: name,
        ENV_KEY_SL_MIN_PCT: format_env_float(float(p["sl_min_pct"])),
        ENV_KEY_SL_MAX_PCT: format_env_float(float(p["sl_max_pct"])),
        ENV_KEY_ELITE_ATR_SL_MULT: format_env_float(float(p["atr_mult"])),
        "MIN_TP_PCT": format_env_float(float(p["tp_pct"])),
        "ATR_BRACKET_TP_MIN_PCT": format_env_float(float(p["tp_pct"])),
    }
    upsert_env_keys(_Path(env_path), updates)
    env_map = os.environ if environ is None else environ
    for k, v in updates.items():
        env_map[k] = v
    object.__setattr__(settings, "stop_loss_profile", name)
    object.__setattr__(settings, "atr_bracket_sl_min_pct", float(p["sl_min_pct"]))
    object.__setattr__(settings, "atr_bracket_sl_max_pct", float(p["sl_max_pct"]))
    object.__setattr__(settings, "elite_atr_sl_mult", float(p["atr_mult"]))
    object.__setattr__(settings, "min_tp_pct", float(p["tp_pct"]))
    try:
        object.__setattr__(settings, "atr_bracket_tp_min_pct", float(p["tp_pct"]))
    except Exception:
        pass
    if signal_engine is not None:
        if hasattr(signal_engine, "atr_bracket_sl_min_pct"):
            signal_engine.atr_bracket_sl_min_pct = float(p["sl_min_pct"])
        if hasattr(signal_engine, "atr_bracket_sl_max_pct"):
            signal_engine.atr_bracket_sl_max_pct = float(p["sl_max_pct"])
        if hasattr(signal_engine, "atr_sl_mult"):
            signal_engine.atr_sl_mult = float(p["atr_mult"])
        if hasattr(signal_engine, "min_tp_pct"):
            signal_engine.min_tp_pct = float(p["tp_pct"])
    strat = str(getattr(settings, "strategy_mode", "") or "volume_sweet_spot")
    return format_stop_loss_updated(name, strategy=strat)




WINNING_FORMULA_USAGE = "Usage: /winning_formula [on|off|status]"

# Snapshot of knobs restored when formula turns off
_WF_SAVED_KEYS = (
    "entry_threshold",
    "max_concurrent_positions",
    "min_tp_pct",
    "atr_bracket_tp_min_pct",
    "trail_fee_buffer_pct",
    "elite_fee_lock_arm_pct",
    "tp1_fraction",
    "stop_loss_profile",
    "taker_fee_rate",
    "maker_fee_rate",
    "circuit_breaker_enabled",
)


def parse_winning_formula_args(args: Sequence[str]) -> Optional[str]:
    """Return on|off|status; None means status."""
    if not args:
        return "status"
    if len(args) != 1:
        raise ValueError(WINNING_FORMULA_USAGE)
    raw = str(args[0]).strip().lower()
    if raw in ("on", "enable", "enabled", "true", "1"):
        return "on"
    if raw in ("off", "disable", "disabled", "false", "0"):
        return "off"
    if raw in ("status", "stat", "s", "?"):
        return "status"
    raise ValueError(WINNING_FORMULA_USAGE)


def format_winning_formula_status_line(*, enabled: bool) -> str:
    return "winning_formula: ON 🚀" if enabled else "winning_formula: OFF"


def format_winning_formula_status(*, enabled: bool, strategy: str = "") -> str:
    strat = (strategy or "").strip() or "volume_sweet_spot"
    if enabled:
        return (
            "🚀 WINNING FORMULA MODE: ON\n"
            "• SL: 🟡 MEDIUM (−1.50%); BEAR clamp −1.25% / ≤$6@$500\n"
            "• Threshold: 65% BEAR / 60% BULL · max 1 pos in BEAR\n"
            "• Fees Tier-1: 0.80% taker / 0.40% maker (RT 1.20%)\n"
            "• HWM: peak ≥+1.20% → SL floor +1.25% (never trail below)\n"
            "• Time exit: maker only if gross ≥+1.25% @ entry×1.0125\n"
            "• Exits: full size only (TP1=0) · post-only maker\n"
            "• Circuit: 3 consec losses · 45m gated auto-resume\n"
            f"• Strategy: {strat}\n"
            f"{WINNING_FORMULA_USAGE}"
        )
    return (
        "winning_formula: OFF — manual profile settings active.\n"
        f"• Strategy: {strat}\n"
        f"{WINNING_FORMULA_USAGE}"
    )


def format_winning_formula_activated() -> str:
    return (
        "🚀 WINNING FORMULA ACTIVATED\n"
        "• SL → 🟡 MEDIUM −1.50% / TP ~+2.25% (BEAR clamp −1.25% / ≤$6@$500)\n"
        "• Threshold 65% BEAR / 50% BULL · max 1 pos in BEAR\n"
        "• Tier-1 fees 0.80%/0.40% · HWM peak +1.20% → SL +1.25%\n"
        "• Time-exit maker ≥+1.25% · full exits (no partials)\n"
        "• Circuit: 3 consec losses · 45m gated auto-resume"
    )



CIRCUITY_BREAKER_USAGE = "Usage: /circuity_breaker_manually [on|off|status]"


def parse_circuity_breaker_args(args: Sequence[str]) -> Optional[str]:
    """Return on|off|status|None (bare → status)."""
    if not args:
        return "status"
    a = str(args[0]).strip().lower()
    if a in ("on", "off", "status"):
        return a
    if a in ("1", "true", "enable", "enabled"):
        return "on"
    if a in ("0", "false", "disable", "disabled"):
        return "off"
    return None


def format_circuity_breaker_status(*, enabled: bool, consec: int = 0, tripped: bool = False) -> str:
    state = "ON" if enabled else "OFF"
    bits = [
        f"circuity_breaker_manually: {state}",
        f"{int(consec)} consecutive loss{'es' if int(consec) != 1 else ''}",
    ]
    if enabled:
        bits.append("auto pause + 45m cooldown when limit hit")
    else:
        bits.append("losses still counted — no auto pause; /resume if paused")
    if tripped:
        bits.append("TRIPPED (paused) — /resume to trade")
    return " · ".join(bits)


def execute_set_winning_formula(
    settings: Any,
    *,
    enabled: bool,
    env_path: Path,
    environ: Optional[MutableMapping[str, str]] = None,
    signal_engine: Any = None,
) -> str:
    """Persist WINNING_FORMULA and apply/revert institutional knobs."""
    from pathlib import Path as _Path
    from trading_bot.config import ENV_KEY_WINNING_FORMULA, upsert_env_keys, format_env_float

    env_map = os.environ if environ is None else environ
    updates: Dict[str, str] = {
        ENV_KEY_WINNING_FORMULA: "true" if enabled else "false",
    }

    if enabled:
        # Save current knobs once (for off restore)
        saved = {}
        for k in _WF_SAVED_KEYS:
            if hasattr(settings, k):
                saved[k] = getattr(settings, k)
        try:
            object.__setattr__(settings, "_winning_formula_saved", saved)
        except Exception:
            pass
        # Apply formula defaults (BEAR floor applied at runtime; base thresh 60)
        object.__setattr__(settings, "entry_threshold", 60.0)
        try:
            from trading_bot.config import apply_runtime_entry_threshold
            apply_runtime_entry_threshold(settings, 60.0)
        except Exception:
            pass
        os.environ["ENTRY_THRESHOLD"] = "60"  # beat shell leftovers that override .env
        object.__setattr__(settings, "max_concurrent_positions", 3)  # BEAR cap enforced live
        object.__setattr__(settings, "min_tp_pct", 0.0305)  # medium SL*1.5+0.8% example floor
        try:
            object.__setattr__(settings, "atr_bracket_tp_min_pct", 0.0305)
        except Exception:
            pass
        # Tier-1 best stack (Sep 2026)
        object.__setattr__(settings, "trail_fee_buffer_pct", 0.0125)  # +1.25% floor
        object.__setattr__(settings, "elite_fee_lock_arm_pct", 0.012)  # arm at RT hurdle +1.20%
        object.__setattr__(settings, "tp1_fraction", 0.0)  # no partials
        object.__setattr__(settings, "taker_fee_rate", 0.008)
        object.__setattr__(settings, "maker_fee_rate", 0.004)
        try:
            object.__setattr__(settings, "circuit_breaker_enabled", True)
        except Exception:
            pass
        updates["ENTRY_THRESHOLD"] = "60"
        updates["MAX_CONCURRENT_POSITIONS"] = "3"
        updates["MIN_TP_PCT"] = format_env_float(0.0305)
        updates["TRAIL_FEE_BUFFER_PCT"] = format_env_float(0.0125)
        updates["ELITE_FEE_LOCK_ARM_PCT"] = format_env_float(0.012)
        updates["TP1_FRACTION"] = "0"
        updates["TAKER_FEE_RATE"] = format_env_float(0.008)
        updates["MAKER_FEE_RATE"] = format_env_float(0.004)
        updates["CIRCUIT_BREAKER_ENABLED"] = "true"
        if signal_engine is not None:
            if hasattr(signal_engine, "min_tp_pct"):
                signal_engine.min_tp_pct = 0.0305
            if hasattr(signal_engine, "tp1_fraction"):
                signal_engine.tp1_fraction = 0.0
        # Best baseline SL for formula: MEDIUM (BEAR still clamps to −1.25% / ≤$6@$500 live)
        try:
            execute_set_stop_loss(
                settings,
                "medium",
                env_path=env_path,
                environ=env_map if environ is not None else None,
                signal_engine=signal_engine,
            )
        except Exception as _sl_exc:
            import logging as _logging
            _logging.getLogger(__name__).warning("WF force SL medium failed: %s", _sl_exc)
            object.__setattr__(settings, "stop_loss_profile", "medium")
            updates["STOP_LOSS_PROFILE"] = "medium"
    else:
        saved = getattr(settings, "_winning_formula_saved", None) or {}
        if saved:
            for k, v in saved.items():
                try:
                    object.__setattr__(settings, k, v)
                    if k == "entry_threshold":
                        updates["ENTRY_THRESHOLD"] = str(int(float(v)))
                    elif k == "max_concurrent_positions":
                        updates["MAX_CONCURRENT_POSITIONS"] = str(int(v))
                    elif k == "min_tp_pct":
                        updates["MIN_TP_PCT"] = format_env_float(float(v))
                    elif k == "trail_fee_buffer_pct":
                        updates["TRAIL_FEE_BUFFER_PCT"] = format_env_float(float(v))
                    elif k == "elite_fee_lock_arm_pct":
                        updates["ELITE_FEE_LOCK_ARM_PCT"] = format_env_float(float(v))
                    elif k == "tp1_fraction":
                        updates["TP1_FRACTION"] = format_env_float(float(v))
                    elif k == "taker_fee_rate":
                        updates["TAKER_FEE_RATE"] = format_env_float(float(v))
                    elif k == "maker_fee_rate":
                        updates["MAKER_FEE_RATE"] = format_env_float(float(v))
                    elif k == "circuit_breaker_enabled":
                        updates["CIRCUIT_BREAKER_ENABLED"] = "true" if v else "false"
                except Exception:
                    pass
            if signal_engine is not None and "min_tp_pct" in saved:
                if hasattr(signal_engine, "min_tp_pct"):
                    signal_engine.min_tp_pct = float(saved["min_tp_pct"])
            if "stop_loss_profile" in saved and saved["stop_loss_profile"]:
                try:
                    execute_set_stop_loss(
                        settings,
                        str(saved["stop_loss_profile"]),
                        env_path=env_path,
                        environ=env_map,
                        signal_engine=signal_engine,
                    )
                except Exception:
                    pass

    object.__setattr__(settings, "winning_formula", bool(enabled))
    upsert_env_keys(_Path(env_path), updates)
    for k, v in updates.items():
        env_map[k] = v

    if enabled:
        return format_winning_formula_activated()
    return "winning_formula: OFF — restored manual profile settings."


def format_universe_status(*, mode: str, count: int) -> str:
    label = universe_friendly_label(mode)
    return f"Universe: {label} ({int(count)})\nUse /universe all, /universe allowlist, or /universe off to switch."


def format_universe_switched(*, mode: str, count: int, refreshing: bool = False) -> str:
    label = universe_friendly_label(mode)
    extra = " — refreshing liquid USD pairs…" if refreshing and normalize_symbol_mode(mode) == "DYNAMIC_ALL" else ""
    return f"Universe → {label} ({int(count)}){extra}"


def format_symbols_reply(symbols: Sequence[str], *, mode: str = "ALLOWLIST") -> str:
    from trading_bot.symbol_universe import format_symbols_chunks

    syms = [str(s) for s in symbols]
    def _is_xstock(sym: str) -> bool:
        base = sym.split("-", 1)[0]
        return base.endswith("x") or base.endswith("X")

    stocks = [s for s in syms if _is_xstock(s)]
    crypto = [s for s in syms if not _is_xstock(s)]
    label = universe_friendly_label(mode)
    parts = [f"Active pairs — {label} ({len(syms)} total)"]
    if stocks:
        parts.append(f"\nStocks / xStocks ({len(stocks)}):")
        parts.append(format_symbols_chunks(stocks, per_line=5))
    else:
        parts.append("\nStocks / xStocks (0): (off or empty — /universe_stocks on)")
    if crypto:
        parts.append(f"\nCrypto ({len(crypto)}):")
        parts.append(format_symbols_chunks(crypto, per_line=5))
    elif normalize_symbol_mode(mode) != "OFF":
        parts.append("\nCrypto (0): (none)")
    return "\n".join(parts)




def symbols_expand_keyboard(count: int) -> Dict[str, Any]:
    """Inline ▼ button under /status — expands the message in place."""
    n = int(count)
    label = f"▼ Symbols ({n})" if n else "▼ Symbols"
    return {"inline_keyboard": [[{"text": label, "callback_data": STATUS_SYMBOLS_CALLBACK}]]}


def symbols_collapse_keyboard(count: int) -> Dict[str, Any]:
    n = int(count)
    label = f"▲ Hide symbols ({n})" if n else "▲ Hide symbols"
    return {"inline_keyboard": [[{"text": label, "callback_data": STATUS_SYMBOLS_COLLAPSE}]]}


def status_with_symbols_button(text: str, *, symbol_count: int) -> TelegramReply:
    return TelegramReply(text=text, reply_markup=symbols_expand_keyboard(symbol_count))


def _status_compact_body(message_text: str) -> str:
    text = str(message_text or "")
    marker = "\n\n—— Symbols"
    idx = text.find("—— Symbols")
    if idx >= 0:
        # keep everything before the symbols panel (and prior blank line)
        return text[:idx].rstrip()
    return text.rstrip()

SET_LIMIT_USAGE = "Usage: /set_limit <trade_cap> <max_book> (e.g. /set_limit 100 1000)"


class SetLimitError(ValueError):
    """Invalid /set_limit arguments — callers must not change state."""


SET_THRESHOLD_USAGE = (
    "Usage: /set_threshold <pct>  or  /set_threshold custom <pct>\n"
    "Examples: /set_threshold 70 · /set_threshold custom 42 — range 15–95"
)
ENTRY_THRESHOLD_MIN = 15
ENTRY_THRESHOLD_MAX = 95


class SetThresholdError(ValueError):
    """Invalid /set_threshold arguments — callers must not change state."""




LIVE_CONFIRM_TTL_SECONDS = 90.0

REPLY_MODE_LIVE_PENDING = (
    "Type /confirm_live to switch to REAL capital execution."
)
REPLY_CONFIRM_EXPIRED = (
    "Confirmation expired or missing — run /mode live first."
)
ALERT_LIVE_ACTIVATED = "LIVE MODE ACTIVATED — real capital."
ALERT_PAPER_RESTORED = "PAPER MODE RESTORED."


RESET_PAPER_CONFIRM_TTL_SECONDS = 60.0
RESET_PAPER_USAGE = (
    "Usage: /reset_paper [cash] then /reset_paper confirm "
    "(e.g. /reset_paper 2500 → /reset_paper confirm). "
    "Optional cash defaults to ACCOUNT_EQUITY / 1600."
)
REPLY_RESET_PAPER_LIVE_REFUSED = (
    "/reset_paper is PAPER ONLY — refused in LIVE mode "
    "(never touches live Kraken balances). Switch with /mode paper first."
)
REPLY_RESET_PAPER_CONFIRM_EXPIRED = (
    "Confirmation expired or missing — run /reset_paper [cash] first "
    "(confirm within ~60s)."
)


class ResetPaperError(ValueError):
    """Invalid /reset_paper args or non-paper mode — callers must not wipe the book."""





def parse_command(text: str) -> Optional[tuple[str, List[str]]]:
    """Parse `/cmd@bot args` → (cmd, args) or None if not a known command."""
    if not text:
        return None
    raw = text.strip()
    if not raw.startswith("/"):
        return None
    parts = raw.split()
    head = parts[0][1:]  # drop leading /
    if "@" in head:
        head = head.split("@", 1)[0]
    cmd = head.strip().lower()
    if cmd not in KNOWN_COMMANDS:
        return None
    return cmd, parts[1:]


def format_status_reply(
    *,
    paper_cash: float,
    paper_equity: float,
    wallet_b4: Optional[float] = None,
    positions: Sequence[Dict[str, Any]],
    paused: bool,
    strategy_mode: str,
    last_tick_age_seconds: Optional[float],
    paper: bool = True,
    symbols: Optional[Sequence[str]] = None,
    max_notional_per_trade: Optional[float] = None,
    max_total_exposure: Optional[float] = None,
    target_setup: Optional[str] = None,
    entry_proximity: Optional[Dict[str, Any]] = None,
    focus_symbol: Optional[str] = None,
    focus_price: Optional[float] = None,
    entry_threshold: Optional[float] = None,
    max_spread_pct: Optional[float] = None,
    trade_profile: Optional[str] = None,
    pid: Optional[int] = None,  # deprecated — ignored (kept for call-site compat)
    last_scan_latency_ms: Optional[float] = None,
    last_scan_pair_count: Optional[int] = None,
    market_state: Optional[str] = None,
    win_rate_pct: Optional[float] = None,
    session_wins: Optional[int] = None,
    session_losses: Optional[int] = None,
    symbol_mode: Optional[str] = None,
    universe_stocks: Optional[bool] = None,
    stock_count: Optional[int] = None,
    spot_long_only: bool = True,
    focus_block_reason: Optional[str] = None,
    tod_gate_enabled: Optional[bool] = None,
    tod_custom_lock: bool = False,
    stop_loss_profile: Optional[str] = None,
    stop_loss_effective_pct: Optional[float] = None,
    stop_loss_clamped: bool = False,
    winning_formula: bool = False,
    circuit_breaker_on: bool = False,
    circuit_breaker_consec_losses: int = 0,
) -> str:
    """Unified /status reply (Apex Signals Now) — same layout on Kraken + Coinbase.

    Field order (operator layout):
      Apex Signals Now {PAPER|LIVE} status
      Last Scan Latency / last_tick_age / circuit breaker
      cash=… / equity=… (+ WR)
      pause=… (only when paused)
      Market: …
      caps=…
      (blank)
      winning_formula / Profile / tod_custom / stop_loss / Threshold / Spread
      Target Setup / Entry Proximity / focus
      positions
      Universe / Symbols
    """
    from trading_bot.utils.entry_proximity import get_entry_threshold, make_progress_bar

    _ = pid  # intentionally unused
    thresh_pct = int(round(float(
        entry_threshold if entry_threshold is not None else get_entry_threshold()
    )))

    nl = chr(10)
    mode = "PAPER" if paper else "LIVE"
    pause_s = "PAUSED (no new buys)" if paused else "running"
    if last_tick_age_seconds is None:
        tick_s = "n/a"
    else:
        tick_s = f"{float(last_tick_age_seconds):.1f}s"

    # Win rate block — big on the right of cash/equity/pause (paper session)
    wr_big = ""
    wr_sub = ""
    if paper:
        w = int(session_wins or 0)
        l = int(session_losses or 0)
        if win_rate_pct is not None:
            wr_big = f"WR {float(win_rate_pct):.0f}%"
            wr_sub = f"{w}W/{l}L"
        elif (w + l) > 0:
            wr_big = f"WR {100.0 * w / (w + l):.0f}%"
            wr_sub = f"{w}W/{l}L"
        else:
            wr_big = "WR —"
            wr_sub = "0W/0L"

    def _row(left: str, right: str = "", width: int = 36) -> str:
        if not right:
            return left
        # keep right edge readable on mobile Telegram
        gap = max(2, width - len(left) - len(right))
        return f"{left}{' ' * gap}{right}"

    # --- header + scan health (top) ---
    lines = [
        f"Apex Signals Now {mode} status",
        "",
    ]
    _ = strategy_mode  # kept for call-site compat; not shown on /status
    if last_scan_latency_ms is not None:
        try:
            lat = max(0.0, float(last_scan_latency_ms))
            pairs = int(last_scan_pair_count) if last_scan_pair_count is not None else 0
            if pairs <= 0 and symbols:
                pairs = len(list(symbols))
            if abs(lat - round(lat)) < 0.05:
                lat_s = f"{int(round(lat))}ms"
            else:
                lat_s = f"{lat:.1f}ms"
            lines.append(f"Last Scan Latency: {lat_s} across {pairs} pairs")
        except (TypeError, ValueError):
            pass
    lines.append(f"last_tick_age={tick_s}")
    lines.append("")  # space between tick age and circuit breaker
    cb_state = "ON" if circuit_breaker_on else "OFF"
    try:
        cl = max(0, int(circuit_breaker_consec_losses))
    except (TypeError, ValueError):
        cl = 0
    # Telegram bots cannot set font color; 🔴 is the supported "red" cue
    lines.append(
        f"(⚠️☣️circuit breaker ☣️⚠️) {cb_state} · {cl} consecutive loss"
        f"{'' if cl == 1 else 'es'}"
    )
    lines.append("")
    lines.append("")

    # --- cash / equity / WR (WR on its own line — Telegram fonts break right-align) ---
    lines.append(f"cash=${float(paper_cash):.2f}")
    lines.append(f"equity=${float(paper_equity):.2f}")
    if wallet_b4 is not None:
        try:
            lines.append(f"💳 Wallet B4=${float(wallet_b4):.2f}")
        except (TypeError, ValueError):
            pass
    if paper and (wr_big or wr_sub):
        wr_bits = [b for b in (wr_big, wr_sub) if b]
        lines.append(" · ".join(wr_bits) if wr_bits else "WR —")
    if paused:
        lines.append(f"pause={pause_s}")
    lines.append("")

    # --- market + caps ---
    ms_raw = str(market_state or "").strip()
    if ms_raw:
        ms_line = ms_raw
        msu = ms_raw.upper()
        if ("BEAR" in msu or "SHORT" in msu) and "BIAS=" not in msu:
            ms_line = f"{ms_raw} bias=SHORT"
        lines.append(f"Market: {ms_line}")
    trade_c = (
        f"${float(max_notional_per_trade):.0f}"
        if max_notional_per_trade is not None
        else "?"
    )
    exp_c = (
        f"${float(max_total_exposure):.0f}"
        if max_total_exposure is not None
        else "?"
    )
    lines.append(f"caps={trade_c}/trade {exp_c} exposure")
    lines.append("")  # space before profile block

    # --- profile block ---
    lines.append(format_winning_formula_status_line(enabled=bool(winning_formula)))
    prof = (trade_profile or "").strip().lower()
    if prof in ("aggressive", "medium", "low"):
        lines.append(f"Profile: {prof.upper()}")
    elif prof:
        lines.append(f"Profile: {prof.upper()}")
    else:
        lines.append("Profile: MEDIUM")
    if tod_gate_enabled is None:
        tod_s = "n/a"
    else:
        tod_s = "ON" if bool(tod_gate_enabled) else "OFF"
        if tod_custom_lock:
            tod_s += " 🔒"
    lines.append(f"tod_custom: {tod_s}")
    lines.append(format_stop_loss_status_line(
        stop_loss_profile or "medium",
        effective_sl_pct=stop_loss_effective_pct,
        clamped=bool(stop_loss_clamped),
    ))
    lines.append(f"Threshold: {thresh_pct}%")
    if max_spread_pct is not None:
        try:
            spread_pct_display = float(max_spread_pct) * 100.0
            spread_s = f"{spread_pct_display:.4f}".rstrip("0").rstrip(".")
            lines.append(f"Spread cap: {spread_s}%")
        except (TypeError, ValueError):
            lines.append("Spread cap: n/a")
    else:
        lines.append("Spread cap: n/a")



    prox = entry_proximity if isinstance(entry_proximity, dict) else None

    # Target Setup: LONG or WAIT only (Kraken spot — never SHORT).
    direction = None
    if prox is not None:
        direction = str(prox.get("direction") or "").upper()
    if direction not in ("LONG", "SHORT", "WAIT"):
        raw = (target_setup or "").strip().upper()
        if "SPOT MODE" in raw and "LONG" in raw:
            direction = "LONG"
        elif raw in ("LONG", "SHORT", "WAIT"):
            direction = raw
        elif "LONG" in raw.split():
            direction = "LONG"
        else:
            direction = "WAIT"
    if direction == "SHORT":
        direction = "WAIT"
    try:
        thr = float(entry_threshold) if entry_threshold is not None else float(get_entry_threshold())
    except Exception:
        thr = 60.0
    ms = str(market_state or "").upper()
    bearish = ("BEAR_CHOP" in ms or "BIAS=SHORT" in ms or ms == "SHORT")
    if spot_long_only and bearish:
        thr = thr * 1.10
    try:
        score_now = float((prox or {}).get("score") or 0.0) if prox else 0.0
    except (TypeError, ValueError):
        score_now = 0.0
    if direction == "LONG" and score_now < thr:
        direction = "WAIT"
    if direction not in ("LONG", "WAIT"):
        direction = "WAIT"

    lines.append("")
    if direction == "LONG" and spot_long_only:
        lines.append("Target Setup: LONG (Spot Mode)")
    else:
        lines.append(f"Target Setup: {direction}")

    if prox is not None:
        try:
            score = float(prox.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
    else:
        score = 0.0
    score = max(0.0, min(100.0, score))
    raw_bar = prox.get("bar") if prox is not None else None
    if isinstance(raw_bar, str) and raw_bar.startswith("[") and raw_bar.endswith("]"):
        bar = raw_bar
    elif isinstance(raw_bar, str) and raw_bar:
        bar = f"[{raw_bar}]"
    else:
        bar = f"[{make_progress_bar(score)}]"
    lines.append(f"Entry Proximity: {bar} {score:.1f}%")

    focus_sym = focus_symbol
    focus_px = focus_price
    if prox is not None:
        if not focus_sym:
            focus_sym = prox.get("symbol")
        if focus_px is None:
            focus_px = prox.get("price")
    if focus_sym:
        try:
            px = float(focus_px) if focus_px is not None else None
        except (TypeError, ValueError):
            px = None
        if px is not None and px > 0:
            focus_line = f"focus={focus_sym} @ ${_tg_fmt_price(px)}"
        else:
            focus_line = f"focus={focus_sym}"
        if direction == "WAIT" and score >= 100.0 - 1e-9:
            reason = (focus_block_reason or "").strip() or "entries frozen"
            focus_line += f" [BLOCKED: {reason}]"
        lines.append(focus_line)
    else:
        lines.append("focus=n/a")

    lines.append("")
    if not positions:

        lines.append("positions: (none)")
    else:
        lines.append(f"positions ({len(positions)}):")
        for p in positions:
            # Each open position is a self-contained block: symbol + own bar +
            # entry + PnL. Never share bar/score state across symbols.
            sym = p.get("symbol") or "?"
            try:
                qty = float(p.get("qty") or 0)
            except (TypeError, ValueError):
                qty = 0.0
            entry = p.get("avg_entry_price")
            mv = p.get("market_value")
            upl = p.get("unrealized_pl")
            side = str(p.get("side") or "long").lower()
            tp = p.get("take_profit")
            mark = p.get("mark_price")
            if mark is None and entry is not None and qty and mv is not None:
                try:
                    mark = float(mv) / abs(float(qty))
                except (TypeError, ValueError, ZeroDivisionError):
                    mark = None

            # Unrealized PnL $ / %
            try:
                upl_f = float(upl) if upl is not None else None
            except (TypeError, ValueError):
                upl_f = None
            if upl_f is None and entry is not None and mark is not None and qty:
                try:
                    e = float(entry)
                    m = float(mark)
                    if side == "short":
                        upl_f = (e - m) * abs(float(qty))
                    else:
                        upl_f = (m - e) * abs(float(qty))
                except (TypeError, ValueError):
                    upl_f = None
            pnl_pct = None
            try:
                if upl_f is not None and entry is not None and abs(float(qty)) > 0:
                    cost = abs(float(entry) * float(qty))
                    if cost > 0:
                        pnl_pct = 100.0 * float(upl_f) / cost
            except (TypeError, ValueError):
                pnl_pct = None
            if pnl_pct is None:
                try:
                    raw_pct = p.get("pnl_pct")
                    if raw_pct is not None:
                        pnl_pct = float(raw_pct)
                except (TypeError, ValueError):
                    pnl_pct = None

            # Progress = current_pnl_pct / tp_target_pct * 100 (tp floor 2%).
            # 100% ≈ +2% gross on a $500 ticket (~+$10 before fees).
            progress = None
            sl = p.get("stop_loss")
            try:
                e = float(entry) if entry is not None else 0.0
                m = float(mark) if mark is not None else 0.0
                if e > 0 and m > 0:
                    if side == "short":
                        cur_pnl_pct = (e - m) / e
                    else:
                        cur_pnl_pct = (m - e) / e
                    tp_target_pct = 0.02
                    if tp is not None and abs(float(tp) - e) > 1e-12:
                        if side == "short":
                            tp_target_pct = max(0.02, (e - float(tp)) / e)
                        else:
                            tp_target_pct = max(0.02, (float(tp) - e) / e)
                    progress = 100.0 * cur_pnl_pct / tp_target_pct
            except (TypeError, ValueError, ZeroDivisionError):
                progress = None
            if progress is None and pnl_pct is not None:
                progress = 100.0 * (float(pnl_pct) / 100.0) / 0.02
            if progress is None:
                progress = 0.0
            progress = max(0.0, min(100.0, float(progress)))
            bar = f"[{make_progress_bar(progress)}]"

            if entry is None:
                entry_s = "?"
            else:
                e = float(entry)
                entry_s = f"${_tg_fmt_price(e)}"
            mv_s = f"${float(mv):.2f}" if mv is not None else "?"
            if upl_f is None:
                pnl_s = "n/a"
            else:
                money = f"+${upl_f:.2f}" if upl_f >= 0 else f"-${abs(upl_f):.2f}"
                if pnl_pct is None:
                    pnl_s = money
                else:
                    pnl_s = f"{money} ({pnl_pct:+.2f}%)"
            side_s = "SHORT" if side == "short" else "LONG"

            if mark is None:
                live_s = "?"
            else:
                m = float(mark)
                live_s = f"${_tg_fmt_price(m)}"

            lines.append("")
            lines.append(f"  {sym}  ({side_s})")
            lines.append(f"  Progress: {bar} {progress:.1f}%")
            lines.append(f"  live={live_s}")
            lines.append(f"  entry={entry_s}  pnl={pnl_s}")
            lines.append(f"  qty={_tg_fmt_qty(qty)}  mv={mv_s}")

    # Universe line + pair list (always last; keep elite regime / WR layout intact)
    lines.append("")
    sym_list = [str(s) for s in symbols] if symbols is not None else []
    lines.append(format_universe_line(symbol_mode, len(sym_list), stocks_enabled=universe_stocks, stocks_count=stock_count))
    if universe_stocks is None:
        # Preserve the legacy compact footer for callers that do not request
        # stock-universe metadata (older integrations/tests).
        lines.append("symbols=" + ",".join(sym_list))
    else:
        # Keep /status short — full list opens via ▼ Symbols inline button.
        n = len(sym_list)
        if n:
            lines.append(f"Symbols: {n} pairs · tap ▼ to expand")
        else:
            lines.append("Symbols: (none) · tap ▼ to expand")

    return nl.join(lines)


def format_mode_reply(*, paper: bool, cash: float, equity: float) -> str:
    """Brief /mode reply: MODE: PAPER|LIVE plus cash/equity one-liner."""
    mode = "PAPER" if paper else "LIVE"
    return f"MODE: {mode} | cash=${float(cash):.2f} equity=${float(equity):.2f}"



def parse_set_limit_args(args: Sequence[str]) -> tuple[float, float]:
    """Parse `/set_limit <trade_cap> <max_book>` → (trade_cap, max_book).

    Both values must be positive numbers and ``max_book >= trade_cap``.
    Raises SetLimitError with a user-facing message on failure.
    """
    if len(args) != 2:
        raise SetLimitError(SET_LIMIT_USAGE)
    try:
        trade_cap = float(args[0])
        max_book = float(args[1])
    except (TypeError, ValueError) as exc:
        raise SetLimitError(
            f"Both values must be numbers. {SET_LIMIT_USAGE} (for proximity use /set_threshold <pct>, e.g. /set_threshold 70)"
        ) from exc
    if trade_cap != trade_cap or max_book != max_book:  # NaN
        raise SetLimitError(f"Both values must be numbers. {SET_LIMIT_USAGE} (for proximity use /set_threshold <pct>, e.g. /set_threshold 70)")
    if trade_cap <= 0 or max_book <= 0:
        raise SetLimitError(
            f"Both trade_cap and max_book must be positive. {SET_LIMIT_USAGE}"
        )
    if max_book < trade_cap:
        raise SetLimitError(
            f"max_book (${max_book:.2f}) must be >= trade_cap (${trade_cap:.2f}). "
            f"{SET_LIMIT_USAGE}"
        )
    return trade_cap, max_book


def format_set_limit_reply(trade_cap: float, max_book: float) -> str:
    """Telegram confirmation after a successful /set_limit."""
    return (
        f"Trade cap updated to ${float(trade_cap):.2f} | "
        f"Max book updated to ${float(max_book):.2f}"
    )


def execute_set_limit(
    settings: Any,
    args: Sequence[str],
    *,
    env_path: Path,
    environ: Optional[MutableMapping[str, str]] = None,
) -> str:
    """Validate, persist to .env, then mutate the live Settings object.

    Size caps only — paper/live mode is never changed. Persist happens before
    the in-memory update so a disk failure leaves runtime state unchanged.
    """
    from trading_bot.config import (
        ENV_KEY_MAX_BOOK,
        ENV_KEY_TRADE_CAP,
        apply_runtime_trade_caps,
        format_env_float,
        upsert_env_keys,
    )

    trade_cap, max_book = parse_set_limit_args(args)
    trade_s = format_env_float(trade_cap)
    book_s = format_env_float(max_book)
    upsert_env_keys(
        env_path,
        {
            ENV_KEY_TRADE_CAP: trade_s,
            ENV_KEY_MAX_BOOK: book_s,
        },
    )
    env_map = os.environ if environ is None else environ
    env_map[ENV_KEY_TRADE_CAP] = trade_s
    env_map[ENV_KEY_MAX_BOOK] = book_s
    apply_runtime_trade_caps(settings, trade_cap, max_book)
    return format_set_limit_reply(trade_cap, max_book)


def parse_set_threshold_args(args: Sequence[str]) -> int:
    """Parse `/set_threshold <pct>` or `/set_threshold custom <pct>` → int %.

    Valid range is 15–95 inclusive. Raises SetThresholdError on failure.
    """
    tokens = [str(a).strip() for a in args if str(a).strip()]
    if not tokens:
        raise SetThresholdError(SET_THRESHOLD_USAGE)
    # /set_threshold custom → prompt for the number
    if len(tokens) == 1 and tokens[0].lower() == "custom":
        raise SetThresholdError(
            "Custom threshold: send /set_threshold custom <pct> "
            f"(integer {ENTRY_THRESHOLD_MIN}–{ENTRY_THRESHOLD_MAX}, e.g. "
            "/set_threshold custom 42)"
        )
    # /set_threshold custom 42  OR  /set_threshold 42
    if tokens[0].lower() == "custom":
        if len(tokens) != 2:
            raise SetThresholdError(SET_THRESHOLD_USAGE)
        raw = tokens[1]
    else:
        if len(tokens) != 1:
            raise SetThresholdError(SET_THRESHOLD_USAGE)
        raw = tokens[0]
    if raw.endswith("%"):
        raw = raw[:-1].strip()
    if not raw:
        raise SetThresholdError(
            f"Threshold must be an integer. {SET_THRESHOLD_USAGE}"
        )
    try:
        num = float(raw)
    except (TypeError, ValueError) as exc:
        raise SetThresholdError(
            f"Threshold must be an integer. {SET_THRESHOLD_USAGE}"
        ) from exc
    if num != num:  # NaN
        raise SetThresholdError(
            f"Threshold must be an integer. {SET_THRESHOLD_USAGE}"
        )
    if abs(num - round(num)) > 1e-9:
        raise SetThresholdError(
            f"Threshold must be an integer. {SET_THRESHOLD_USAGE}"
        )
    val = int(round(num))
    if val < ENTRY_THRESHOLD_MIN or val > ENTRY_THRESHOLD_MAX:
        raise SetThresholdError(
            f"Threshold must be {ENTRY_THRESHOLD_MIN}–{ENTRY_THRESHOLD_MAX} "
            f"inclusive. Got {val}. {SET_THRESHOLD_USAGE}"
        )
    return val


def format_set_threshold_reply(threshold: int | float) -> str:
    """Telegram confirmation after a successful /set_threshold."""
    val = int(threshold)
    return (
        f"✅ Entry threshold updated to {val}%. "
        f"Signals reaching or exceeding {val}% proximity will now trigger paper trades."
    )


def execute_set_threshold(
    settings: Any,
    args: Sequence[str],
    *,
    env_path: Path,
    environ: Optional[MutableMapping[str, str]] = None,
    custom_lock: Optional[bool] = None,
) -> str:
    """Validate, persist ENTRY_THRESHOLD to .env, then mutate runtime + Settings.

    When custom_lock is True (or args start with ``custom``), set
    ENTRY_THRESHOLD_CUSTOM_LOCK so /aggressive|/medium|/low cannot overwrite it.
    When custom_lock is False (plain /set_threshold <pct>), clear the lock.
    """
    from trading_bot.config import (
        ENV_KEY_ENTRY_THRESHOLD,
        ENV_KEY_ENTRY_THRESHOLD_CUSTOM_LOCK,
        apply_runtime_entry_threshold,
        upsert_env_keys,
    )

    tokens = [str(a).strip() for a in args if str(a).strip()]
    is_custom = bool(tokens) and tokens[0].lower() == "custom"
    if custom_lock is None:
        custom_lock = is_custom

    val = parse_set_threshold_args(args)
    val_s = str(int(val))
    updates = {ENV_KEY_ENTRY_THRESHOLD: val_s}
    if custom_lock:
        updates[ENV_KEY_ENTRY_THRESHOLD_CUSTOM_LOCK] = "true"
    else:
        updates[ENV_KEY_ENTRY_THRESHOLD_CUSTOM_LOCK] = "false"
    upsert_env_keys(env_path, updates)
    env_map = os.environ if environ is None else environ
    for k, v in updates.items():
        env_map[k] = v
    apply_runtime_entry_threshold(settings, val)
    try:
        object.__setattr__(settings, "entry_threshold_custom_lock", bool(custom_lock))
    except Exception:
        pass
    reply = format_set_threshold_reply(val)
    if custom_lock:
        reply += (
            " Locked against /aggressive /medium /low — profiles will keep this threshold."
        )
    return reply


# Telegram /set_spread: user enters percent (0.5 → 0.5%); Settings/.env store fraction.
SPREAD_PCT_MIN = 0.01   # 0.01%
SPREAD_PCT_MAX = 5.0    # 5.0%
SET_SPREAD_USAGE = (
    "Usage: /set_spread <pct> (e.g. /set_spread 0.5) — percent units, range "
    f"{SPREAD_PCT_MIN:g}–{SPREAD_PCT_MAX:g}"
)


class SetSpreadError(ValueError):
    """Invalid /set_spread arguments — callers must not change state."""


def parse_set_spread_args(args: Sequence[str]) -> float:
    """Parse `/set_spread <pct>` → percent float (0.5 means 0.5%).

    Also accepts a single token like ``0.5%``. Raises SetSpreadError on failure.
    """
    if len(args) != 1:
        raise SetSpreadError(SET_SPREAD_USAGE)
    raw = str(args[0]).strip().replace(",", "")
    if raw.endswith("%"):
        raw = raw[:-1].strip()
    if not raw:
        raise SetSpreadError(f"Spread must be a number. {SET_SPREAD_USAGE}")
    try:
        pct = float(raw)
    except (TypeError, ValueError) as exc:
        raise SetSpreadError(f"Spread must be a number. {SET_SPREAD_USAGE}") from exc
    if pct != pct:  # NaN
        raise SetSpreadError(f"Spread must be a number. {SET_SPREAD_USAGE}")
    if pct < SPREAD_PCT_MIN or pct > SPREAD_PCT_MAX:
        raise SetSpreadError(
            f"Spread must be {SPREAD_PCT_MIN:g}–{SPREAD_PCT_MAX:g}% inclusive. "
            f"Got {pct:g}%. {SET_SPREAD_USAGE}"
        )
    return pct


def parse_set_max_spread_alias(args: Sequence[str]) -> float:
    """Parse `/set MAX_SPREAD_PCT 0.5` or `/set MAX_SPREAD_PCT=0.5%` → percent."""
    if not args:
        raise SetSpreadError(
            "Usage: /set MAX_SPREAD_PCT <pct> or /set_spread <pct> "
            f"(e.g. /set_spread 0.5) — range {SPREAD_PCT_MIN:g}–{SPREAD_PCT_MAX:g}"
        )
    tokens: list[str] = []
    for a in args:
        s = str(a).strip()
        if "=" in s:
            left, right = s.split("=", 1)
            if left:
                tokens.append(left)
            if right:
                tokens.append(right)
        else:
            tokens.append(s)
    if not tokens:
        raise SetSpreadError(SET_SPREAD_USAGE)
    key = tokens[0].strip().upper().replace("-", "_")
    if key not in ("MAX_SPREAD_PCT", "SPREAD", "SPREAD_PCT", "MAX_SPREAD"):
        raise SetSpreadError(
            f"Unknown /set key {tokens[0]!r}. Supported: MAX_SPREAD_PCT "
            f"(or use /set_spread <pct>)"
        )
    if len(tokens) != 2:
        raise SetSpreadError(
            "Usage: /set MAX_SPREAD_PCT <pct> (e.g. /set MAX_SPREAD_PCT 0.5 "
            "or /set MAX_SPREAD_PCT=0.5%)"
        )
    return parse_set_spread_args([tokens[1]])


def format_set_spread_reply(pct: float) -> str:
    """Telegram confirmation after successful /set_spread (pct is percent units)."""
    frac = float(pct) / 100.0
    frac_s = f"{frac:.10f}".rstrip("0").rstrip(".")
    return (
        f"✅ Spread cap updated to {float(pct):g}% "
        f"(MAX_SPREAD_PCT={frac_s}). "
        f"Entries wider than {float(pct):g}% mid-spread will be skipped."
    )


def execute_set_spread(
    settings: Any,
    args: Sequence[str],
    *,
    env_path: "Path",
    environ: Optional[MutableMapping[str, str]] = None,
    from_alias: bool = False,
) -> str:
    """Validate percent, persist fraction to .env MAX_SPREAD_PCT, mutate Settings."""
    from pathlib import Path as _Path
    from trading_bot.config import (
        ENV_KEY_MAX_SPREAD_PCT,
        apply_runtime_max_spread_pct,
        format_env_float,
        upsert_env_keys,
    )

    pct = parse_set_max_spread_alias(args) if from_alias else parse_set_spread_args(args)
    frac = float(pct) / 100.0
    frac_s = format_env_float(frac)
    upsert_env_keys(_Path(env_path), {ENV_KEY_MAX_SPREAD_PCT: frac_s})
    env_map = os.environ if environ is None else environ
    env_map[ENV_KEY_MAX_SPREAD_PCT] = frac_s
    apply_runtime_max_spread_pct(settings, frac)
    return format_set_spread_reply(pct)








# ---------------------------------------------------------------------------
# Trading aggressiveness profiles: /aggressive /medium /low /profile <name>
# ---------------------------------------------------------------------------

TRADE_PROFILE_NAMES = frozenset({"aggressive", "medium", "low"})

# Exact user-facing presets. Spreads are fractions of mid; disable_tod_gate=True
# means the UTC rollover blackout is skipped on every strategy tick.
TRADE_PROFILE_PRESETS: Dict[str, Dict[str, Any]] = {
    # Aggressive: proximity auto-fire friendly — RVOL/volume 1.0x, TOD off,
    # 35% threshold, 1m eval, ~12s poll, $1000/$1500 caps, 3 concurrent.
    "aggressive": {
        "entry_threshold": 35,
        "max_spread_pct": 0.005,
        "disable_tod_gate": True,
        "rvol_breakout_mult": 1.0,
        "prefilter_volume_spike_mult": 1.0,
        "max_concurrent_positions": 3,
        "agent_poll_seconds": 12.0,
        "bar_timeframe": "1Min",
        "max_notional_per_trade_usd": 1000.0,
        "max_total_exposure_usd": 3000.0,
    },
    "medium": {
        "entry_threshold": 65,
        "max_spread_pct": 0.0025,
        "disable_tod_gate": False,
        "rvol_breakout_mult": 2.0,
        "prefilter_volume_spike_mult": 2.75,
        "max_concurrent_positions": 2,
        "agent_poll_seconds": 30.0,
        "max_notional_per_trade_usd": 1000.0,
        "max_total_exposure_usd": 3000.0,
    },
    "low": {
        "entry_threshold": 80,
        "max_spread_pct": 0.0015,
        "disable_tod_gate": False,
        "rvol_breakout_mult": 2.5,
        "prefilter_volume_spike_mult": 2.75,
        "max_concurrent_positions": 2,
        "agent_poll_seconds": 60.0,
        "max_notional_per_trade_usd": 1000.0,
        "max_total_exposure_usd": 3000.0,
    },
}

SET_PROFILE_USAGE = (
    "Usage: /aggressive | /medium | /low | /profile <aggressive|medium|low>"
)


class TradeProfileError(ValueError):
    """Invalid /profile arguments — callers must not change state."""


def normalize_trade_profile(name: str) -> str:
    n = str(name or "").strip().lower()
    aliases = {
        "agg": "aggressive",
        "a": "aggressive",
        "med": "medium",
        "m": "medium",
        "preserve": "low",
        "l": "low",
        "conservative": "low",
        "safe": "low",
    }
    n = aliases.get(n, n)
    if n not in TRADE_PROFILE_NAMES:
        raise TradeProfileError(
            f"Unknown profile {name!r}. Choose aggressive, medium, or low. "
            f"{SET_PROFILE_USAGE}"
        )
    return n


def parse_profile_args(args: Sequence[str]) -> str:
    """Parse `/profile <name>` → normalized profile name."""
    if len(args) != 1:
        raise TradeProfileError(SET_PROFILE_USAGE)
    return normalize_trade_profile(args[0])


def format_set_profile_reply(
    profile: str,
    knobs: Dict[str, Any],
    *,
    threshold_locked: bool = False,
    locked_threshold: Optional[float] = None,
    tod_locked: bool = False,
    locked_tod_enabled: Optional[bool] = None,
    full_override: bool = False,
) -> str:
    """Return Telegram confirmation for each profile."""
    name = str(profile).strip().lower()
    replies = {
        "aggressive": "⚡ PROFILE: AGGRESSIVE ARMED — Threshold 35% | Spread 0.5% | TOD Gate Disabled | RVOL 1.0x | Auto paper-buy on proximity. Caps $1000/$3000 | 3 concurrent.",
        "medium": "⚖️ PROFILE: MEDIUM ARMED — Threshold 65% | Spread 0.25% | TOD Gate Enabled. Balanced intraday volume strategy.",
        "low": "🛡️ PROFILE: LOW ARMED — Threshold 80% | Spread 0.15% | TOD Gate Enabled. Selective mode preserved.",
    }
    body = replies.get(name, SET_PROFILE_USAGE)
    if full_override:
        body += (
            "\n✅ Full override — cleared winning_formula + custom threshold/TOD locks; "
            "all knobs match this profile."
        )
    return body



def execute_set_profile(
    settings: Any,
    profile: str,
    *,
    env_path: "Path",
    environ: Optional[MutableMapping[str, str]] = None,
    signal_engine: Any = None,
) -> str:
    """Validate profile, persist knobs to .env, mutate Settings (+ optional engine)."""
    from pathlib import Path as _Path
    from trading_bot.config import (
        ENV_KEY_AGENT_POLL_SECONDS,
        ENV_KEY_BAR_TIMEFRAME,
        ENV_KEY_DISABLE_TOD_GATE,
        ENV_KEY_ENTRY_THRESHOLD,
        ENV_KEY_MAX_BOOK,
        ENV_KEY_MAX_CONCURRENT_POSITIONS,
        ENV_KEY_MAX_SPREAD_PCT,
        ENV_KEY_PREFILTER_VOLUME_SPIKE_MULT,
        ENV_KEY_RVOL_BREAKOUT_MULT,
        ENV_KEY_TRADE_CAP,
        ENV_KEY_TRADE_PROFILE,
        apply_runtime_trade_profile,
        format_env_float,
        upsert_env_keys,
    )

    name = normalize_trade_profile(profile)
    knobs = dict(TRADE_PROFILE_PRESETS[name])
    from trading_bot.config import (
        ENV_KEY_ENTRY_THRESHOLD_CUSTOM_LOCK,
        ENV_KEY_TOD_CUSTOM_LOCK,
        ENV_KEY_TOD_GATE_ENABLED,
        ENV_KEY_WINNING_FORMULA,
    )

    # /aggressive|/medium|/low = FULL override of trade setup
    env_updates = {
        ENV_KEY_TRADE_PROFILE: name,
        ENV_KEY_MAX_SPREAD_PCT: format_env_float(float(knobs["max_spread_pct"])),
        ENV_KEY_DISABLE_TOD_GATE: "true" if knobs["disable_tod_gate"] else "false",
        ENV_KEY_TOD_GATE_ENABLED: "false" if knobs["disable_tod_gate"] else "true",
        ENV_KEY_ENTRY_THRESHOLD: str(int(knobs["entry_threshold"])),
        ENV_KEY_ENTRY_THRESHOLD_CUSTOM_LOCK: "false",
        ENV_KEY_TOD_CUSTOM_LOCK: "false",
        ENV_KEY_WINNING_FORMULA: "false",
    }
    object.__setattr__(settings, "entry_threshold_custom_lock", False)
    object.__setattr__(settings, "tod_custom_lock", False)
    object.__setattr__(settings, "winning_formula", False)
    try:
        object.__setattr__(settings, "_winning_formula_saved", None)
    except Exception:
        pass

    if "rvol_breakout_mult" in knobs:
        env_updates[ENV_KEY_RVOL_BREAKOUT_MULT] = format_env_float(
            float(knobs["rvol_breakout_mult"])
        )
    if "prefilter_volume_spike_mult" in knobs:
        env_updates[ENV_KEY_PREFILTER_VOLUME_SPIKE_MULT] = format_env_float(
            float(knobs["prefilter_volume_spike_mult"])
        )
    if "max_concurrent_positions" in knobs:
        env_updates[ENV_KEY_MAX_CONCURRENT_POSITIONS] = str(
            int(knobs["max_concurrent_positions"])
        )
    if "agent_poll_seconds" in knobs:
        env_updates[ENV_KEY_AGENT_POLL_SECONDS] = format_env_float(
            float(knobs["agent_poll_seconds"])
        )
    if "bar_timeframe" in knobs:
        env_updates[ENV_KEY_BAR_TIMEFRAME] = str(knobs["bar_timeframe"])
    if "max_notional_per_trade_usd" in knobs:
        env_updates[ENV_KEY_TRADE_CAP] = format_env_float(
            float(knobs["max_notional_per_trade_usd"])
        )
    if "max_total_exposure_usd" in knobs:
        env_updates[ENV_KEY_MAX_BOOK] = format_env_float(
            float(knobs["max_total_exposure_usd"])
        )
    upsert_env_keys(_Path(env_path), env_updates)
    env_map = os.environ if environ is None else environ
    for k, v in env_updates.items():
        env_map[k] = v
    apply_runtime_trade_profile(settings, name, knobs, signal_engine=signal_engine)
    return format_set_profile_reply(name, knobs, full_override=True)



TEST_TRADE_USAGE = "Usage: /test_trade <symbol> (e.g. /test_trade SOL-USD or /test_trade LINK)"
TEST_TRADE_NOTIONAL_USD = 100.0
REPLY_TEST_TRADE_LIVE_REFUSED = (
    "/test_trade is PAPER ONLY — switch to paper with /mode paper first."
)


class TestTradeError(ValueError):
    """Invalid /test_trade args or non-paper mode — callers must not place orders."""

    __test__ = False  # not a pytest test class


def normalize_test_trade_symbol(raw: str) -> str:
    """Uppercase/slash-normalize; bare bases like ``SOL`` become ``SOL-USD``."""
    s = str(raw or "").strip().upper().replace("/", "-").replace("_", "-")
    if not s:
        return s
    if "-" not in s:
        s = f"{s}-USD"
    return s


def parse_test_trade_args(args: Sequence[str], settings: Any = None) -> str:
    """Parse `/test_trade <symbol>` → allowlisted ``BASE-USD`` symbol.

    Raises TestTradeError with a user-facing message on failure.
    """
    from trading_bot.config import HARD_SYMBOL_ALLOWLIST

    if len(args) != 1:
        raise TestTradeError(TEST_TRADE_USAGE)
    sym = normalize_test_trade_symbol(args[0])
    if not sym or sym == "-USD":
        raise TestTradeError(TEST_TRADE_USAGE)
    if settings is not None and hasattr(settings, "is_allowlisted"):
        ok = bool(settings.is_allowlisted(sym))
    else:
        ok = sym in HARD_SYMBOL_ALLOWLIST
    if not ok:
        raise TestTradeError(
            f"Symbol {sym} is not on the active symbol universe. {TEST_TRADE_USAGE}"
        )
    return sym


def assert_paper_mode_for_test_trade(*, paper: bool) -> None:
    """Refuse /test_trade outside paper mode (never places live orders)."""
    if not paper:
        raise TestTradeError(REPLY_TEST_TRADE_LIVE_REFUSED)


def assert_paper_mode_for_reset_paper(*, paper: bool) -> None:
    """Refuse /reset_paper outside paper mode (never touches live balances)."""
    if not paper:
        raise ResetPaperError(REPLY_RESET_PAPER_LIVE_REFUSED)


def parse_reset_paper_args(args: Sequence[str]) -> tuple[bool, float | None]:
    """Parse `/reset_paper` args → ``(confirm, cash_or_None)``.

    Forms:
      []                  → (False, None)   arm with default cash
      [cash]              → (False, cash)   arm with explicit cash
      [confirm]           → (True, None)    confirm using pending/default
      [confirm, cash]     → (True, cash)    confirm with explicit cash

    Raises ResetPaperError on bad args.
    """
    if not args:
        return False, None
    tokens = [str(a).strip() for a in args if str(a).strip()]
    if not tokens:
        return False, None
    head = tokens[0].lower().replace("-", "_")
    if head == "confirm":
        if len(tokens) == 1:
            return True, None
        if len(tokens) == 2:
            return True, _parse_reset_paper_cash(tokens[1])
        raise ResetPaperError(RESET_PAPER_USAGE)
    if len(tokens) == 1:
        return False, _parse_reset_paper_cash(tokens[0])
    raise ResetPaperError(RESET_PAPER_USAGE)


def _parse_reset_paper_cash(raw: str) -> float:
    s = str(raw or "").strip().replace(",", "").replace("$", "")
    if s.endswith("%"):
        raise ResetPaperError(f"Invalid cash amount {raw!r}. {RESET_PAPER_USAGE}")
    try:
        val = float(s)
    except ValueError as exc:
        raise ResetPaperError(f"Invalid cash amount {raw!r}. {RESET_PAPER_USAGE}") from exc
    if not (val > 0) or val != val:  # NaN check
        raise ResetPaperError(f"Cash must be a positive number. {RESET_PAPER_USAGE}")
    if val > 10_000_000:
        raise ResetPaperError(f"Cash amount too large ({val:g}). {RESET_PAPER_USAGE}")
    return float(val)


def default_reset_paper_cash(account_equity: float | None = None) -> float:
    """Baseline cash: ACCOUNT_EQUITY when positive, else 1600."""
    try:
        eq = float(account_equity) if account_equity is not None else 0.0
    except (TypeError, ValueError):
        eq = 0.0
    if eq > 0:
        return eq
    return 1600.0


def _fmt_reset_cash_arg(cash: float) -> str:
    """Compact cash token for confirm hint (e.g. 2500 or 1600.5)."""
    s = f"{float(cash):.10f}".rstrip("0").rstrip(".")
    return s if s else "0"


def format_reset_paper_pending_reply(
    cash: float, *, ttl_seconds: float = RESET_PAPER_CONFIRM_TTL_SECONDS
) -> str:
    cash_s = f"{float(cash):.2f}"
    arg = _fmt_reset_cash_arg(cash)
    return (
        f"Confirm paper book RESET to cash=${cash_s} (wipes open paper positions). "
        f"Reply `/reset_paper confirm` or `/reset_paper confirm {arg}` "
        f"within ~{int(ttl_seconds)}s."
    )


def format_reset_paper_done_reply(
    cash: float,
    *,
    equity: float | None = None,
    positions: int = 0,
    account_equity_updated: bool = False,
) -> str:
    eq = float(equity if equity is not None else cash)
    msg = (
        f"Paper book reset: cash=${float(cash):.2f} equity=${eq:.2f} "
        f"positions={int(positions)}. Check /status."
    )
    if account_equity_updated:
        msg += (
            f" ACCOUNT_EQUITY={_fmt_reset_cash_arg(cash)} saved to .env "
            "(baseline sticks across restarts)."
        )
    return msg



WIPE_PAPER_CONFIRM_TTL_SECONDS = 60.0
WIPE_PAPER_USAGE = (
    "Usage: /wipe_paper [cash] then /wipe_paper confirm "
    "(e.g. /wipe_paper 1600 → /wipe_paper confirm). "
    "Alias: /factory_reset. "
    "Full paper scratch: book + history DB + paper logs (default ACCOUNT_EQUITY / 1600)."
)
REPLY_WIPE_PAPER_LIVE_REFUSED = (
    "/wipe_paper is PAPER ONLY — refused in LIVE mode "
    "(never touches live Kraken balances). Switch with /mode paper first."
)
REPLY_WIPE_PAPER_CONFIRM_EXPIRED = (
    "Confirmation expired or missing — run /wipe_paper [cash] first "
    "(confirm within ~60s)."
)


class WipePaperError(ValueError):
    """Invalid /wipe_paper args or non-paper mode — callers must not wipe artifacts."""


def assert_paper_mode_for_wipe_paper(*, paper: bool) -> None:
    """Refuse /wipe_paper outside paper mode (never touches live balances)."""
    if not paper:
        raise WipePaperError(REPLY_WIPE_PAPER_LIVE_REFUSED)


def parse_wipe_paper_args(args: Sequence[str]) -> tuple[bool, float | None]:
    """Parse `/wipe_paper` args → ``(confirm, cash_or_None)``.

    Same forms as `/reset_paper`. Raises WipePaperError on bad args.
    """
    if not args:
        return False, None
    tokens = [str(a).strip() for a in args if str(a).strip()]
    if not tokens:
        return False, None
    head = tokens[0].lower().replace("-", "_")
    if head == "confirm":
        if len(tokens) == 1:
            return True, None
        if len(tokens) == 2:
            try:
                return True, _parse_reset_paper_cash(tokens[1])
            except ResetPaperError as exc:
                msg = str(exc).replace("/reset_paper", "/wipe_paper").replace(
                    RESET_PAPER_USAGE, WIPE_PAPER_USAGE
                )
                raise WipePaperError(msg) from exc
        raise WipePaperError(WIPE_PAPER_USAGE)
    if len(tokens) == 1:
        try:
            return False, _parse_reset_paper_cash(tokens[0])
        except ResetPaperError as exc:
            msg = str(exc).replace("/reset_paper", "/wipe_paper").replace(
                RESET_PAPER_USAGE, WIPE_PAPER_USAGE
            )
            raise WipePaperError(msg) from exc
    raise WipePaperError(WIPE_PAPER_USAGE)


def format_wipe_paper_pending_reply(
    cash: float, *, ttl_seconds: float = WIPE_PAPER_CONFIRM_TTL_SECONDS
) -> str:
    cash_s = f"{float(cash):.2f}"
    arg = _fmt_reset_cash_arg(cash)
    return (
        f"Confirm FULL paper WIPE to cash=${cash_s} "
        f"(book + history DB + paper logs; open positions wiped). "
        f"Reply `/wipe_paper confirm` or `/wipe_paper confirm {arg}` "
        f"within ~{int(ttl_seconds)}s. Alias: /factory_reset confirm."
    )


def format_wipe_paper_done_reply(
    cash: float,
    *,
    equity: float | None = None,
    positions: int = 0,
    account_equity_updated: bool = False,
    history_cleared: bool = True,
    logs_cleared: bool = True,
    ledger_rows_deleted: int = 0,
    trading_tables_cleared: Optional[Dict[str, int]] = None,
) -> str:
    eq = float(equity if equity is not None else cash)
    tables = trading_tables_cleared or {}
    tables_s = (
        ",".join(f"{k}:{v}" for k, v in sorted(tables.items())) if tables else "none"
    )
    msg = (
        f"Paper factory wipe: cash=${float(cash):.2f} equity=${eq:.2f} "
        f"positions={int(positions)}. "
        f"history_cleared={'yes' if history_cleared else 'no'} "
        f"(ledger_rows={int(ledger_rows_deleted)}; tables={tables_s}). "
        f"logs_cleared={'yes' if logs_cleared else 'no'}. "
        f"Check /status /history /logs."
    )
    if account_equity_updated:
        msg += (
            f" ACCOUNT_EQUITY={_fmt_reset_cash_arg(cash)} saved to .env "
            "(baseline sticks across restarts)."
        )
    return msg


# Tables cleared in the instance trading SQLite (fills / locks / trade logger).
WIPE_PAPER_TRADING_DB_TABLES = (
    "events",
    "trade_memory",
    "buy_dedupe",
    "symbol_lockouts",
    "post_stop_cooldown",
    "trade_failures",
)


def wipe_paper_artifacts(
    *,
    project_root: Any,
    sqlite_path: Optional[Any] = None,
    ledger_path: Optional[Any] = None,
    log_path: Optional[Any] = None,
) -> Dict[str, Any]:
    """Clear paper history DBs + truncate paper log. Never deletes .env or DB files.

    Returns a summary dict for Telegram replies / tests.
    """
    import sqlite3
    from pathlib import Path

    root = Path(project_root)
    ledger = Path(ledger_path) if ledger_path else root / "data" / "paper_ledger.db"
    trading_db = Path(sqlite_path) if sqlite_path else root / "data" / "trading_bot_2.db"
    if log_path is not None:
        log = Path(log_path)
    else:
        resolved = resolve_instance_log_path(root)
        log = Path(resolved) if resolved is not None else root / "data" / "paper_loop.log"

    summary: Dict[str, Any] = {
        "ledger_path": str(ledger),
        "ledger_rows_deleted": 0,
        "trading_db_path": str(trading_db),
        "trading_tables_cleared": {},
        "log_path": str(log),
        "log_truncated": False,
    }

    if ledger.exists():
        try:
            conn = sqlite3.connect(str(ledger))
            try:
                try:
                    n = int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
                except Exception:
                    n = 0
                conn.execute("DELETE FROM events")
                try:
                    conn.execute("DELETE FROM sqlite_sequence WHERE name='events'")
                except Exception:
                    pass
                conn.commit()
                summary["ledger_rows_deleted"] = n
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("wipe_paper ledger clear failed (%s): %s", ledger, exc)

    if trading_db.exists():
        cleared: Dict[str, int] = {}
        try:
            conn = sqlite3.connect(str(trading_db))
            try:
                existing = {
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                for table in WIPE_PAPER_TRADING_DB_TABLES:
                    if table not in existing:
                        continue
                    try:
                        n = int(
                            conn.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()[0]
                        )
                    except Exception:
                        n = 0
                    conn.execute(f"DELETE FROM [{table}]")
                    try:
                        conn.execute(
                            "DELETE FROM sqlite_sequence WHERE name=?", (table,)
                        )
                    except Exception:
                        pass
                    cleared[table] = n
                conn.commit()
            finally:
                conn.close()
            summary["trading_tables_cleared"] = cleared
        except Exception as exc:
            logger.warning(
                "wipe_paper trading DB clear failed (%s): %s", trading_db, exc
            )

    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "w", encoding="utf-8") as fh:
            fh.write("")
        summary["log_truncated"] = True
    except Exception as exc:
        logger.warning("wipe_paper log truncate failed (%s): %s", log, exc)

    # Paper expectancy sources — clear so /weekly_digest_101 paper resets
    trades_db = root / "data" / "trades.db"
    summary["trades_db_cleared"] = 0
    if trades_db.exists():
        try:
            conn = sqlite3.connect(str(trades_db))
            try:
                try:
                    n = int(conn.execute("SELECT COUNT(*) FROM closed_trades").fetchone()[0])
                except Exception:
                    n = 0
                conn.execute("DELETE FROM closed_trades")
                try:
                    conn.execute("DELETE FROM blacklist")
                except Exception:
                    pass
                try:
                    conn.execute("DELETE FROM sqlite_sequence WHERE name='closed_trades'")
                except Exception:
                    pass
                conn.commit()
                summary["trades_db_cleared"] = n
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("wipe_paper trades.db clear failed: %s", exc)

    mem_json = root / "data" / "trade_memory.json"
    try:
        if mem_json.exists():
            mem_json.write_text(
                '{"outcomes": [], "tightened": false, "session_wins": 0, "session_losses": 0}\n',
                encoding="utf-8",
            )
            summary["trade_memory_json_reset"] = True
    except Exception as exc:
        logger.warning("wipe_paper trade_memory.json reset failed: %s", exc)

    return summary


def format_test_trade_reply(
    symbol: str,
    fill_price: float,
    *,
    notional: float = TEST_TRADE_NOTIONAL_USD,
) -> str:
    """Telegram confirmation after a successful paper test BUY."""
    notion_s = f"{float(notional):.0f}"
    return (
        f"🧪 TEST TRADE EXECUTED: Bought ${notion_s} of {symbol} "
        f"@ ${float(fill_price):.2f}. Check /status to view open position."
    )


def build_test_trade_order(
    symbol: str,
    *,
    price: float,
    notional: float = TEST_TRADE_NOTIONAL_USD,
    qty_precision: int = 8,
    paper: bool = True,
):
    """Build an immediate paper BUY OrderRequest (LIMIT at mark — fills via paper book).

    Bypasses strategy/risk sizing; still allowlist-gated by Executor.submit.
    """
    from trading_bot.models import OrderRequest, OrderSide, OrderType
    from trading_bot.risk_manager import floor_qty

    px = float(price)
    if px <= 0 or px != px:
        raise TestTradeError(f"/test_trade failed: invalid price for {symbol}")
    notion = float(notional)
    if notion <= 0:
        raise TestTradeError("/test_trade failed: invalid notional")
    precision = int(qty_precision) if qty_precision else 8
    qty = floor_qty(notion / px, precision)
    if qty <= 0:
        raise TestTradeError(
            f"/test_trade failed: could not size ${notion:.0f} of {symbol} @ ${px}"
        )
    return OrderRequest(
        symbol=symbol,
        side=OrderSide.BUY,
        qty=qty,
        order_type=OrderType.LIMIT,
        limit_price=px,
        paper=bool(paper),
        post_only=True,
    )



# ---------------------------------------------------------------------------
# C2 commands: /ping /positions /balance /history /grok /regime /logs /help
# ---------------------------------------------------------------------------

COMMAND_HELP: Dict[str, str] = {
    "status": "/status — Apex PAPER/LIVE snapshot (cash, equity, positions, proximity)",
    "pause": "/pause — skip new buys (exits/brackets still active)",
    "resume": "/resume — re-enable new buys",
    "pnl": "/pnl — send day P&L performance report",
    "kill": "/kill — cancel, flatten allowlist, stop loop",
    "mode": "/mode | /mode live | /mode paper — show or switch PAPER/LIVE (live needs /confirm_live)",
    "confirm_live": "/confirm_live — confirm pending LIVE switch (after /mode live)",
    "set_limit": "/set_limit <trade_cap> <max_book> — update size caps (e.g. /set_limit 100 1000)",
    "set_threshold": "/set_threshold <pct> | custom <pct> — entry proximity 15–95 (e.g. /set_threshold custom 42)",
    "set_threshold_custom": "/set_threshold_custom <pct> — same as custom (15–95, e.g. /set_threshold_custom 42)",
    "winning_formula": "/winning_formula [on|off|status] — institutional expectancy mode",
    "weekly_digest_101": "/weekly_digest_101 [paper|live] — 7-day expectancy (paper resets on wipe; live = real fills)",
    "circuity_breaker_manually": "/circuity_breaker_manually [on|off|status] — CB auto-pause/cooldown; losses always counted",
    "stop_loss": "/stop_loss <tight|medium|free> — SL distance profile",
    "tod_custom": "/tod_custom [on|off|toggle] — TOD gate (locks vs profiles)",
    "set_spread": "/set_spread <pct> — max bid-ask spread % (e.g. /set_spread 0.5 → 0.5%)",
    "set": "/set MAX_SPREAD_PCT <pct> — alias for /set_spread (also MAX_SPREAD_PCT=0.5%)",
    "aggressive": "/aggressive — Threshold 35%, Spread 0.5%, TOD off, RVOL 1.0x, auto paper-buy, $1000/$1500, 3 pos",
    "medium": "/medium — Threshold 65%, Spread 0.25%, TOD Gate Enabled",
    "low": "/low — Threshold 80%, Spread 0.15%, TOD Gate Enabled",
    "profile": "/profile <aggressive|medium|low> — alias for the three profile toggles",
    "test_trade": "/test_trade <symbol> — paper-only ~$100 BUY (e.g. /test_trade SOL-USD)",
    "reset_paper": "/reset_paper [cash] then /reset_paper confirm — wipe paper book to cash (default ACCOUNT_EQUITY/1600; e.g. /reset_paper 2500)",
    "wipe_paper": "/wipe_paper [cash] then /wipe_paper confirm — FULL paper scratch (book + history DB + paper logs; alias /factory_reset)",
    "factory_reset": "/factory_reset — alias for /wipe_paper (same two-step confirm)",
    "ping": "/ping — heartbeat + handler latency (pong + ms)",
    "positions": "/positions — open positions with qty, entry, PnL%, SL, TP",
    "balance": "/balance — PAPER/LIVE cash, available margin/cash, equity",
    "history": "/history — last 5 executed trades with realized PnL",
    "grok": "/grok — immediate Grok/xAI sentiment (BUY/SELL/HOLD + confidence)",
    "regime": "/regime — market regime + live ADX / Choppiness",
    "logs": "/logs — last 20 lines of this instance paper/app log (secrets redacted)",
    "universe": "/universe [all|allowlist|off] — crypto universe mode (off = stocks-only)",
    "universe_all": "/universe_all — switch to Kraken discovery (all liquid USD pairs)",
    "universe_stocks": "/universe_stocks [on|off|toggle] — include liquid xStocks",
    "symbols": "/symbols — list currently active trading pairs",
    "close": "/close SYMBOL [confirm] — fee preview; thin exits (±0.8% gross) need confirm",
    "clear_positions": "/clear_positions [xstocks|all] [confirm] — fee warn; ±0.8% thin needs confirm",
    "help": "/help — list active commands + short syntax",
}


def armed_commands_log_fragment(commands: Optional[Sequence[str]] = None) -> str:
    """Slash-list for the 'Telegram commands armed' log line."""
    cmds = list(commands) if commands is not None else sorted(KNOWN_COMMANDS)
    # Prefer stable help-ish order when using full set
    preferred = [
        "status", "pause", "resume", "pnl", "kill", "mode", "confirm_live",
        "set_limit", "set_threshold", "set_threshold_custom", "set_spread", "tod_custom", "stop_loss", "winning_formula", "weekly_digest_101", "circuity_breaker_manually", "set", "aggressive", "medium", "low", "profile", "test_trade", "reset_paper", "wipe_paper", "factory_reset",
        "ping", "positions", "balance", "history", "grok", "regime", "logs", "universe", "universe_all", "universe_stocks", "symbols", "help",
    ]
    if commands is None:
        ordered = [c for c in preferred if c in KNOWN_COMMANDS]
        ordered.extend(sorted(c for c in KNOWN_COMMANDS if c not in ordered))
    else:
        ordered = cmds
    return " ".join(f"/{c}" for c in ordered)


def format_help_reply(commands: Optional[Sequence[str]] = None) -> str:
    """Active command list + short syntax (only commands this build knows)."""
    if commands is None:
        preferred = [
            "status", "pause", "resume", "pnl", "kill", "mode", "confirm_live",
            "set_limit", "set_threshold", "set_threshold_custom", "set_spread", "tod_custom", "stop_loss", "winning_formula", "weekly_digest_101", "circuity_breaker_manually", "set", "aggressive", "medium", "low", "profile", "test_trade", "reset_paper", "wipe_paper", "factory_reset",
            "ping", "positions", "balance", "history", "grok", "regime", "logs", "universe", "universe_all", "universe_stocks", "symbols", "help",
        ]
        cmds = [c for c in preferred if c in KNOWN_COMMANDS]
        cmds.extend(sorted(c for c in KNOWN_COMMANDS if c not in cmds))
    else:
        cmds = [str(c).lower() for c in commands if str(c).lower() in KNOWN_COMMANDS]
    lines = ["Apex Signals Now — commands:", ""]
    for c in cmds:
        lines.append(COMMAND_HELP.get(c, f"/{c}"))
    lines.append("")
    lines.append(
        "Weekly Digest 101: /weekly_digest_101 paper|live · morning equity 7:00 AM CT."
    )
    lines.append("Tip: only the authorized Telegram chat can run these.")
    return "\n".join(lines)


def format_ping_reply(latency_ms: float) -> str:
    return f"pong {float(latency_ms):.0f}ms"


def _fmt_opt_price(val: Any) -> str:
    if val is None:
        return "n/a"
    try:
        f = float(val)
    except (TypeError, ValueError):
        return "n/a"
    if f != f:  # NaN
        return "n/a"
    return f"${_tg_fmt_price(f)}"


def format_positions_reply(
    positions: Sequence[Dict[str, Any]],
    *,
    paper: bool = True,
) -> str:
    mode = "PAPER" if paper else "LIVE"
    if not positions:
        return f"Open positions ({mode}): (none)"
    lines = [f"Open positions ({mode}) — {len(positions)}:"]
    for p in positions:
        sym = p.get("symbol") or "?"
        try:
            qty = float(p.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        entry = p.get("avg_entry_price")
        pnl_pct = p.get("pnl_pct")
        if pnl_pct is None:
            # derive from unrealized_pl + entry*qty if possible
            try:
                upl = p.get("unrealized_pl")
                if upl is not None and entry is not None and float(entry) > 0 and qty:
                    cost = float(entry) * abs(qty)
                    if cost > 0:
                        pnl_pct = 100.0 * float(upl) / cost
            except (TypeError, ValueError):
                pnl_pct = None
        if pnl_pct is None:
            pnl_s = "n/a"
        else:
            try:
                pct = float(pnl_pct)
                pnl_s = f"{pct:+.2f}%"
            except (TypeError, ValueError):
                pnl_s = "n/a"
        sl = _fmt_opt_price(p.get("stop_loss"))
        tp = _fmt_opt_price(p.get("take_profit"))
        entry_s = _fmt_opt_price(entry)
        lines.append(
            f"  {sym} qty={_tg_fmt_qty(qty)} entry={entry_s} pnl={pnl_s} sl={sl} tp={tp}"
        )
    return "\n".join(lines)


def format_balance_reply(
    *,
    paper: bool,
    cash: float,
    equity: float,
    available: Optional[float] = None,
    available_label: str = "available",
) -> str:
    mode = "PAPER" if paper else "LIVE"
    avail = float(cash if available is None else available)
    # Paper books usually have no margin — label as cash; live may expose buying_power
    label = available_label or ("cash" if paper else "available margin")
    return (
        f"Balance ({mode})\n"
        f"cash=${float(cash):.2f}\n"
        f"{label}=${avail:.2f}\n"
        f"equity=${float(equity):.2f}"
    )


def recent_trades_from_ledger(
    db_path: Optional[str] = None,
    *,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """Last N BUY/SELL fills from paper_ledger.db (realized pnl on sells)."""
    import sqlite3
    from pathlib import Path

    path = (
        Path(db_path)
        if db_path
        else Path(__file__).resolve().parents[1] / "data" / "paper_ledger.db"
    )
    if not path.exists():
        return []
    lim = max(1, min(int(limit), 50))
    try:
        conn = sqlite3.connect(str(path))
        rows = list(
            conn.execute(
                "SELECT ts, kind, symbol, side, qty, price, notional, fee, pnl "
                "FROM events WHERE kind IN ('BUY','SELL','FILL') "
                "ORDER BY id DESC LIMIT ?",
                (lim,),
            )
        )
        conn.close()
    except Exception as exc:
        logger.debug("recent_trades_from_ledger failed: %s", exc)
        return []
    out: List[Dict[str, Any]] = []
    for ts, kind, symbol, side, qty, price, notional, fee, pnl in rows:
        try:
            when = datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(_CT)
            when_s = when.strftime("%m/%d %H:%M CT")
        except (TypeError, ValueError, OSError):
            when_s = "?"
        side_u = (side or kind or "").upper()
        out.append(
            {
                "when": when_s,
                "symbol": str(symbol or "?"),
                "side": side_u,
                "qty": float(qty or 0),
                "price": float(price or 0),
                "notional": float(notional or 0),
                "pnl": float(pnl or 0),
            }
        )
    return out


def format_history_reply(trades: Sequence[Dict[str, Any]]) -> str:
    if not trades:
        return "History: (no fills in paper_ledger)"
    lines = [f"Last {len(trades)} trades:"]
    for t in trades:
        pnl = float(t.get("pnl") or 0)
        pnl_s = f"-${abs(pnl):.2f}" if pnl < 0 else f"+${pnl:.2f}"
        lines.append(
            f"  {t.get('when','?')} {t.get('side','?')} {t.get('symbol','?')} "
            f"qty={_tg_fmt_qty(float(t.get('qty') or 0))} @ ${_tg_fmt_price(float(t.get('price') or 0))} "
            f"pnl={pnl_s}"
        )
    return "\n".join(lines)


def format_grok_reply(
    *,
    action: str,
    confidence: float,
    error: Optional[str] = None,
) -> str:
    if error:
        return f"Grok: {error}"
    act = str(action or "HOLD").upper()
    if act not in ("BUY", "SELL", "HOLD"):
        act = "HOLD"
    try:
        conf = float(confidence)
    except (TypeError, ValueError):
        conf = 0.0
    return f"Grok: {act} confidence={conf:.2f}"


def format_regime_reply(
    *,
    market_regime: Optional[str],
    vol_regime: Optional[str] = None,
    regime_updated_at: Optional[str] = None,
    indicators: Optional[Sequence[Dict[str, Any]]] = None,
    note: Optional[str] = None,
) -> str:
    lines = ["Market regime:"]
    if market_regime:
        lines.append(f"  macro={market_regime}")
    else:
        lines.append("  macro=n/a (Phase 2 market_regime not in active_params yet)")
    if vol_regime:
        lines.append(f"  optimizer_vol={vol_regime}")
    if regime_updated_at:
        lines.append(f"  updated={regime_updated_at}")
    if note:
        lines.append(f"  note={note}")
    inds = list(indicators or [])
    if not inds:
        lines.append("  indicators: (no live ADX/CHOP in memory yet)")
    else:
        lines.append("  indicators (live ticks):")
        for row in inds[:12]:
            sym = row.get("symbol") or "?"
            adx = row.get("adx")
            chop = row.get("chop", row.get("choppiness"))
            try:
                adx_s = f"{float(adx):.1f}" if adx is not None else "n/a"
            except (TypeError, ValueError):
                adx_s = "n/a"
            try:
                chop_s = f"{float(chop):.1f}" if chop is not None else "n/a"
            except (TypeError, ValueError):
                chop_s = "n/a"
            lines.append(f"    {sym} ADX={adx_s} CHOP={chop_s}")
    return "\n".join(lines)


_SECRET_REDACT_KEYS = (
    "BOT_TOKEN",
    "API_KEY",
    "API_SECRET",
    "SECRET",
    "PASSWORD",
    "PRIVATE_KEY",
    "BEARER",
    "AUTHORIZATION",
    "XAI_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "KRAKEN_API",
    "COINBASE",
)


def redact_secrets(text: str) -> str:
    """Best-effort scrub of tokens/keys from log snippets (Telegram-safe)."""
    import re

    s = text or ""
    # long base64-ish / hex tokens
    s = re.sub(r"(?i)(bot\d{6,}:[A-Za-z0-9_-]{20,})", "[redacted-telegram-token]", s)
    s = re.sub(
        r"(?i)\b([A-Za-z0-9_-]*(?:api[_-]?key|api[_-]?secret|token|password|secret)[A-Za-z0-9_-]*)\s*[=:]\s*\S+",
        r"\1=[redacted]",
        s,
    )
    for key in _SECRET_REDACT_KEYS:
        s = re.sub(
            rf"(?i)\b{re.escape(key)}\b\s*[=:]\s*\S+",
            f"{key}=[redacted]",
            s,
        )
    return s


def resolve_instance_log_path(project_root: Optional[Any] = None) -> Optional[Any]:
    """Prefer Instance paper_loop.log, then logs/app.log."""
    from pathlib import Path

    root = Path(project_root) if project_root else Path(__file__).resolve().parents[1]
    candidates = [
        root / "data" / "paper_loop.log",
        root / "logs" / "app.log",
        root / "data" / "app.log",
    ]
    for p in candidates:
        if p.exists() and p.is_file():
            return p
    return None


def read_tail_log_lines(
    path: Optional[Any] = None,
    *,
    project_root: Optional[Any] = None,
    n: int = 20,
    max_chars: int = 3500,
) -> str:
    """Last n lines of the instance log; secrets redacted; Telegram length-capped."""
    from pathlib import Path

    p = Path(path) if path else resolve_instance_log_path(project_root)
    if p is None or not p.exists():
        return "Logs: (no paper_loop.log or logs/app.log found for this instance)"
    try:
        # Efficient-ish tail for typical log sizes
        with open(p, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            block = min(size, 64_000)
            fh.seek(max(0, size - block))
            raw = fh.read().decode("utf-8", errors="replace")
        lines = raw.splitlines()
        tail = lines[-max(1, int(n)) :]
        body = "\n".join(redact_secrets(x) for x in tail)
        header = f"Last {len(tail)} lines ({p.name}):"
        out = header + "\n" + body
        if len(out) > max_chars:
            out = out[: max_chars - 20] + "\n…[truncated]"
        return out
    except Exception as exc:
        logger.debug("read_tail_log_lines failed: %s", exc)
        return f"Logs: failed to read {p}: {exc}"




WEEKLY_DIGEST_USAGE = "Usage: /weekly_digest_101 [paper|live]"


def parse_weekly_digest_args(args: Sequence[str]) -> Optional[str]:
    """Return 'paper'|'live'|None (None = use bot current mode)."""
    if not args:
        return None
    if len(args) != 1:
        raise ValueError(WEEKLY_DIGEST_USAGE)
    a = str(args[0]).strip().lower()
    if a in ("paper", "p"):
        return "paper"
    if a in ("live", "l", "real"):
        return "live"
    if a in ("mode", "status", "?"):
        return None
    raise ValueError(WEEKLY_DIGEST_USAGE)


def _classify_exit_bucket(*, reason: str, entry: float, exit_px: float, pnl: float) -> str:
    """Map close → Hard SL / Fee Floor / Profit Runner / Other."""
    r = str(reason or "").upper()
    gross = 0.0
    try:
        if entry and float(entry) > 0 and exit_px is not None:
            gross = (float(exit_px) - float(entry)) / float(entry)
    except Exception:
        gross = 0.0
    if "PROFIT" in r and "RUNNER" in r:
        return "Profit Runner (>+2.50%)"
    if r in ("TP2", "TP", "TAKE_PROFIT", "TRAIL") or gross >= 0.025:
        if gross >= 0.025 or r in ("TP2", "TP", "TAKE_PROFIT"):
            return "Profit Runner (>+2.50%)"
    if (
        "FEE" in r
        or "MAKER_BE" in r
        or "TIME_EXIT_MAKER" in r
        or "RISK FREE" in r
        or (0.012 <= gross < 0.025)
    ):
        return "Fee Floor (+1.25% Lock)"
    if r in ("SL", "STOP_LOSS", "STOP", "TIME-STOP", "TIME_STOP") or gross < 0.012:
        if float(pnl) <= 0 or gross <= 0.0 or r in ("SL", "STOP_LOSS", "STOP", "TIME-STOP", "TIME_STOP"):
            return "Hard SL (-1.50%)"
    if float(pnl) > 0 and gross >= 0.012:
        return "Fee Floor (+1.25% Lock)"
    if float(pnl) > 0:
        return "Profit Runner (>+2.50%)"
    return "Hard SL (-1.50%)"


def build_weekly_expectancy_digest(
    *,
    days: int = 7,
    mode: str = "paper",
    trades_db: Optional[Path] = None,
    memory_db: Optional[Path] = None,
    root: Optional[Path] = None,
    live_closes: Optional[Sequence[Dict[str, Any]]] = None,
) -> str:
    """7-day expectancy digest. mode=paper|live. live_closes optional Kraken fills."""
    import sqlite3
    from collections import defaultdict
    from datetime import datetime, timedelta, timezone
    from pathlib import Path as _Path

    mode_u = "LIVE" if str(mode).lower().startswith("live") else "PAPER"
    base = _Path(root) if root else _Path(__file__).resolve().parents[1]
    if mode_u == "LIVE":
        tdb = base / "data" / "trades_live.db"
    else:
        tdb = _Path(trades_db) if trades_db else base / "data" / "trades.db"
    mdb = _Path(memory_db) if memory_db else base / "data" / "trading_bot_2.db"
    days = max(1, min(int(days), 90))
    now = datetime.now(timezone.utc)
    cutoff_ts = (now - timedelta(days=days)).timestamp()
    cutoff_iso = (now - timedelta(days=days)).isoformat()

    closes: List[Dict[str, Any]] = []
    if tdb.exists():
        try:
            conn = sqlite3.connect(str(tdb))
            rows = conn.execute(
                "SELECT symbol, entry, exit, net_pnl, timestamp FROM closed_trades "
                "WHERE timestamp >= ? ORDER BY timestamp ASC",
                (cutoff_ts,),
            ).fetchall()
            conn.close()
            for sym, entry, exit_px, pnl, ts in rows:
                closes.append(
                    {
                        "symbol": str(sym or "?"),
                        "entry": float(entry or 0),
                        "exit": float(exit_px or 0),
                        "pnl": float(pnl or 0),
                        "ts": float(ts or 0),
                        "reason": "",
                    }
                )
        except Exception as exc:
            logger.debug("weekly digest closed_trades: %s", exc)

    # Match reasons from trade_memory (SELL)
    mem: List[Dict[str, Any]] = []
    if mdb.exists():
        try:
            conn = sqlite3.connect(str(mdb))
            rows = conn.execute(
                "SELECT ts, symbol, reason, pnl FROM trade_memory "
                "WHERE UPPER(action)='SELL' AND ts >= ? ORDER BY id ASC",
                (cutoff_iso,),
            ).fetchall()
            conn.close()
            for ts, sym, reason, pnl in rows:
                mem.append(
                    {
                        "ts": str(ts or ""),
                        "symbol": str(sym or "?"),
                        "reason": str(reason or ""),
                        "pnl": float(pnl or 0),
                    }
                )
        except Exception as exc:
            logger.debug("weekly digest trade_memory: %s", exc)

    # Attach nearest memory reason by symbol + pnl proximity
    for c in closes:
        best = None
        best_score = 1e18
        for m in mem:
            if m["symbol"] != c["symbol"]:
                continue
            score = abs(float(m["pnl"]) - float(c["pnl"]))
            if score < best_score:
                best_score = score
                best = m
        if best is not None and best_score < 0.05:
            c["reason"] = best["reason"]

    if not closes and mem and mode_u == "PAPER":
        # fallback: memory-only window (paper)
        for m in mem:
            closes.append(
                {
                    "symbol": m["symbol"],
                    "entry": 0.0,
                    "exit": 0.0,
                    "pnl": m["pnl"],
                    "ts": 0.0,
                    "reason": m["reason"],
                }
            )

    if mode_u == "LIVE" and live_closes:
        closes = []
        for row in live_closes:
            closes.append(
                {
                    "symbol": str(row.get("symbol") or "?"),
                    "entry": float(row.get("entry") or 0),
                    "exit": float(row.get("exit") or row.get("price") or 0),
                    "pnl": float(row.get("pnl") or row.get("net_pnl") or 0),
                    "ts": float(row.get("ts") or 0),
                    "reason": str(row.get("reason") or "LIVE_FILL"),
                }
            )

    if not closes:
        return (
            f"📊 [WEEKLY DIGEST 101 - LAST {days} DAYS]\n"
            f"Mode: {mode_u}\n"
            "No closed trades in this window."
            + ("\n(Live: no Kraken fills in range / keys missing.)" if mode_u == "LIVE" else
               "\n(Paper: wipe or no closes yet — resets on /wipe_paper.)")
        )

    total = len(closes)
    net = sum(c["pnl"] for c in closes)
    wins = sum(1 for c in closes if c["pnl"] > 0)
    losses = sum(1 for c in closes if c["pnl"] <= 0)
    wr = 100.0 * wins / total if total else 0.0
    gross_wins = sum(c["pnl"] for c in closes if c["pnl"] > 0)
    gross_losses = abs(sum(c["pnl"] for c in closes if c["pnl"] < 0))
    pf = (gross_wins / gross_losses) if gross_losses > 1e-9 else (999.0 if gross_wins > 0 else 0.0)

    buckets: Dict[str, Dict[str, float]] = defaultdict(lambda: {"count": 0, "pnl": 0.0})
    for c in closes:
        b = _classify_exit_bucket(
            reason=c.get("reason") or "",
            entry=c.get("entry") or 0.0,
            exit_px=c.get("exit") or 0.0,
            pnl=c.get("pnl") or 0.0,
        )
        buckets[b]["count"] += 1
        buckets[b]["pnl"] += float(c["pnl"])

    by_sym: Dict[str, float] = defaultdict(float)
    by_sym_n: Dict[str, int] = defaultdict(int)
    for c in closes:
        by_sym[c["symbol"]] += float(c["pnl"])
        by_sym_n[c["symbol"]] += 1
    worst = sorted(by_sym.items(), key=lambda kv: kv[1])[:3]
    best = sorted(by_sym.items(), key=lambda kv: kv[1], reverse=True)[:3]

    def money(x: float) -> str:
        return f"+${x:,.2f}" if x >= 0 else f"-${abs(x):,.2f}"

    order = [
        "Hard SL (-1.50%)",
        "Fee Floor (+1.25% Lock)",
        "Profit Runner (>+2.50%)",
    ]
    lines = [
        f"📊 [WEEKLY DIGEST 101 - LAST {days} DAYS]",
        f"Mode: {mode_u}",
        "",
        f"• Total Trades: {total}",
        f"• Net P&L: {money(net)} (Fees Included)",
        f"• Win Rate: {wr:.1f}% ({wins}W / {losses}L)",
        f"• Profit Factor: {pf:.2f}" if pf < 900 else f"• Profit Factor: ∞",
        "",
        "📈 EXIT REASON BREAKDOWN:",
    ]
    shown = [k for k in order if k in buckets]
    shown.extend(sorted(k for k in buckets if k not in order))
    for i, k in enumerate(shown):
        branch = "└─" if i == len(shown) - 1 else "├─"
        lines.append(
            f" {branch} {k}: {int(buckets[k]['count'])} trades ({money(buckets[k]['pnl'])})"
        )

    lines.append("")
    lines.append("⚠️ BOTTOM 3 SYMBOLS (LEAKS):")
    if not worst or all(v >= 0 for _, v in worst):
        lines.append(" (none — no net losers this window)")
    else:
        for i, (sym, pnl) in enumerate(worst, 1):
            if pnl >= 0:
                continue
            lines.append(f" {i}. {sym}: {money(pnl)} ({by_sym_n[sym]} trades)")

    lines.append("")
    lines.append("🏆 TOP 3 SYMBOLS:")
    if not best or all(v <= 0 for _, v in best):
        lines.append(" (none — no net winners this window)")
    else:
        for i, (sym, pnl) in enumerate(best, 1):
            if pnl <= 0:
                continue
            lines.append(f" {i}. {sym}: {money(pnl)} ({by_sym_n[sym]} trades)")

    return "\n".join(lines)



def day_trades_from_ledger(
    db_path: Optional[str] = None,
    *,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Build performance_report trade rows from today's SELL fills in paper_ledger."""
    import sqlite3
    from pathlib import Path

    path = Path(db_path) if db_path else Path(__file__).resolve().parents[1] / "data" / "paper_ledger.db"
    if not path.exists():
        return []
    now_dt = now or datetime.now(_CT)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=_CT)
    else:
        now_dt = now_dt.astimezone(_CT)
    day_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    start_ts = day_start.timestamp()

    try:
        conn = sqlite3.connect(str(path))
        rows = list(
            conn.execute(
                "SELECT ts, kind, symbol, side, qty, price, notional, fee, pnl "
                "FROM events WHERE ts >= ? AND kind IN ('BUY','SELL','FILL') ORDER BY id",
                (start_ts,),
            )
        )
        conn.close()
    except Exception as exc:
        logger.debug("day_trades_from_ledger failed: %s", exc)
        return []

    # Pair: use SELL rows with pnl; cost from notional or prior BUY
    buys_by_sym: Dict[str, List[Dict[str, Any]]] = {}
    trades: List[Dict[str, Any]] = []
    for ts, kind, symbol, side, qty, price, notional, fee, pnl in rows:
        side_u = (side or "").upper()
        kind_u = (kind or "").upper()
        is_buy = kind_u == "BUY" or side_u == "BUY"
        is_sell = kind_u == "SELL" or side_u == "SELL"
        when = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        sym = str(symbol or "")
        if is_buy and not is_sell:
            buys_by_sym.setdefault(sym, []).append(
                {
                    "when": when,
                    "cost": float(notional or 0) or (float(qty or 0) * float(price or 0)),
                    "price": float(price or 0),
                    "qty": float(qty or 0),
                }
            )
            continue
        if is_sell:
            cost = 0.0
            pending = buys_by_sym.get(sym) or []
            if pending:
                b = pending.pop(0)
                cost = float(b.get("cost") or 0)
            exit_n = float(notional or 0) or (float(qty or 0) * float(price or 0))
            trades.append(
                {
                    "when": when,
                    "symbol": sym,
                    "cost": cost,
                    "exit": exit_n,
                    "pnl": float(pnl or 0),
                }
            )
    return trades


class OpsControlState:
    """Shared pause / kill / live-confirm flags for the trading loop."""

    # Circuit-breaker gated auto-resume (seconds). Does not affect manual /pause.
    CB_AUTO_RESUME_COOLDOWN_SEC = 45 * 60

    def __init__(self) -> None:
        self.paused: bool = False
        self.kill_requested: bool = False
        self.kill_liquidate: bool = False
        self._pending_live_confirm_until: float = 0.0
        self._pending_reset_paper_until: float = 0.0
        self._pending_reset_paper_cash: float | None = None
        self._pending_reset_paper_explicit: bool = False
        self._pending_wipe_paper_until: float = 0.0
        self._pending_wipe_paper_cash: float | None = None
        self._pending_wipe_paper_explicit: bool = False
        # Circuit-breaker auto-resume (only armed by consecutive-loss CB)
        self.cb_auto_resume_armed: bool = False
        self.cb_auto_resume_at: float = 0.0  # time.monotonic() deadline
        self.cb_win_since_trip: bool = False
        self.cb_active: bool = False  # True while CB pause is in effect
        self.cb_enabled: bool = True  # master switch (losses always counted)

    def set_pause(self, value: bool) -> None:
        self.paused = bool(value)

    def arm_cb_auto_resume(self, cooldown_sec: float | None = None) -> int:
        """Arm gated auto-resume after circuit breaker. Returns cooldown minutes."""
        cd = float(
            cooldown_sec
            if cooldown_sec is not None
            else self.CB_AUTO_RESUME_COOLDOWN_SEC
        )
        self.cb_auto_resume_armed = True
        self.cb_auto_resume_at = time.monotonic() + max(60.0, cd)
        self.cb_win_since_trip = False
        self.cb_active = True
        return int(round(cd / 60.0))

    def clear_cb_auto_resume(self) -> None:
        self.cb_auto_resume_armed = False
        self.cb_auto_resume_at = 0.0
        self.cb_win_since_trip = False
        self.cb_active = False

    def cb_auto_resume_ready(self, *, regime_bull_ok: bool) -> bool:
        """True when cooldown elapsed AND (win since trip OR BULL_OK)."""
        if not self.cb_auto_resume_armed or not self.paused:
            return False
        if time.monotonic() < float(self.cb_auto_resume_at or 0.0):
            return False
        return bool(self.cb_win_since_trip) or bool(regime_bull_ok)

    def request_kill(self, *, liquidate: bool = True) -> None:
        self.kill_requested = True
        self.kill_liquidate = bool(liquidate)

    def arm_live_confirm(self, ttl: float = LIVE_CONFIRM_TTL_SECONDS) -> float:
        """Arm pending LIVE confirm; returns TTL seconds used."""
        ttl_f = float(ttl)
        self._pending_live_confirm_until = time.monotonic() + ttl_f
        return ttl_f

    def clear_live_confirm(self) -> None:
        self._pending_live_confirm_until = 0.0

    def has_pending_live_confirm(self, now: Optional[float] = None) -> bool:
        """True if a non-expired /mode live confirm is pending."""
        deadline = float(self._pending_live_confirm_until or 0.0)
        if deadline <= 0:
            return False
        now_m = time.monotonic() if now is None else float(now)
        if now_m > deadline:
            self._pending_live_confirm_until = 0.0
            return False
        return True

    def pending_live_confirm_remaining(self, now: Optional[float] = None) -> float:
        if not self.has_pending_live_confirm(now=now):
            return 0.0
        now_m = time.monotonic() if now is None else float(now)
        return max(0.0, float(self._pending_live_confirm_until) - now_m)

    def arm_reset_paper_confirm(
        self,
        cash: float,
        *,
        explicit: bool = False,
        ttl: float = RESET_PAPER_CONFIRM_TTL_SECONDS,
    ) -> float:
        """Arm pending paper-book reset; returns TTL seconds used."""
        ttl_f = float(ttl)
        self._pending_reset_paper_until = time.monotonic() + ttl_f
        self._pending_reset_paper_cash = float(cash)
        self._pending_reset_paper_explicit = bool(explicit)
        return ttl_f

    def clear_reset_paper_confirm(self) -> None:
        self._pending_reset_paper_until = 0.0
        self._pending_reset_paper_cash = None
        self._pending_reset_paper_explicit = False

    def has_pending_reset_paper_confirm(self, now: Optional[float] = None) -> bool:
        deadline = float(self._pending_reset_paper_until or 0.0)
        if deadline <= 0:
            return False
        now_m = time.monotonic() if now is None else float(now)
        if now_m > deadline:
            self.clear_reset_paper_confirm()
            return False
        return True

    def pending_reset_paper_cash(self) -> float | None:
        if not self.has_pending_reset_paper_confirm():
            return None
        return self._pending_reset_paper_cash

    def pending_reset_paper_explicit(self) -> bool:
        if not self.has_pending_reset_paper_confirm():
            return False
        return bool(self._pending_reset_paper_explicit)

    def arm_wipe_paper_confirm(
        self,
        cash: float,
        *,
        explicit: bool = False,
        ttl: float = WIPE_PAPER_CONFIRM_TTL_SECONDS,
    ) -> float:
        """Arm pending full paper wipe; returns TTL seconds used."""
        ttl_f = float(ttl)
        self._pending_wipe_paper_until = time.monotonic() + ttl_f
        self._pending_wipe_paper_cash = float(cash)
        self._pending_wipe_paper_explicit = bool(explicit)
        return ttl_f

    def clear_wipe_paper_confirm(self) -> None:
        self._pending_wipe_paper_until = 0.0
        self._pending_wipe_paper_cash = None
        self._pending_wipe_paper_explicit = False

    def has_pending_wipe_paper_confirm(self, now: Optional[float] = None) -> bool:
        deadline = float(self._pending_wipe_paper_until or 0.0)
        if deadline <= 0:
            return False
        now_m = time.monotonic() if now is None else float(now)
        if now_m > deadline:
            self.clear_wipe_paper_confirm()
            return False
        return True

    def pending_wipe_paper_cash(self) -> float | None:
        if not self.has_pending_wipe_paper_confirm():
            return None
        return self._pending_wipe_paper_cash

    def pending_wipe_paper_explicit(self) -> bool:
        if not self.has_pending_wipe_paper_confirm():
            return False
        return bool(self._pending_wipe_paper_explicit)



BOT_COMMAND_SPECS: List[Dict[str, str]] = [
    {"command": "status", "description": "PAPER/LIVE snapshot"},
    {"command": "pause", "description": "Skip new buys"},
    {"command": "resume", "description": "Re-enable new buys"},
    {"command": "pnl", "description": "Day P&L report"},
    {"command": "kill", "description": "Flatten + stop loop"},
    {"command": "mode", "description": "Show/switch PAPER/LIVE"},
    {"command": "confirm_live", "description": "Confirm LIVE switch"},
    {"command": "set_limit", "description": "Update size caps"},
    {"command": "set_threshold", "description": "Entry threshold 15-95"},
    {"command": "set_threshold_custom", "description": "Custom entry threshold 15-95"},
    {"command": "set_spread", "description": "Max bid-ask spread %"},
    {"command": "tod_custom", "description": "TOD gate on/off (locks vs profiles)"},
    {"command": "stop_loss", "description": "SL profile tight/medium/free"},
    {"command": "winning_formula", "description": "Winning formula on/off"},
    {"command": "weekly_digest_101", "description": "Weekly digest paper|live"},
    {"command": "circuity_breaker_manually", "description": "CB auto on/off (counts always)"},
    {"command": "aggressive", "description": "Aggressive trade profile"},
    {"command": "medium", "description": "Medium trade profile"},
    {"command": "low", "description": "Low trade profile"},
    {"command": "profile", "description": "Set aggressiveness profile"},
    {"command": "test_trade", "description": "Paper ~$100 BUY"},
    {"command": "reset_paper", "description": "Wipe paper book"},
    {"command": "wipe_paper", "description": "Full paper scratch"},
    {"command": "factory_reset", "description": "Factory reset paper (danger)"},
    {"command": "set", "description": "Set knobs / profile alias"},
    {"command": "ping", "description": "Heartbeat latency"},
    {"command": "positions", "description": "Open positions"},
    {"command": "balance", "description": "Cash / equity"},
    {"command": "history", "description": "Last 5 trades"},
    {"command": "grok", "description": "Grok sentiment"},
    {"command": "regime", "description": "Market regime"},
    {"command": "logs", "description": "Tail paper log"},
    {"command": "universe", "description": "Crypto universe mode (off = stocks-only)"},
    {"command": "universe_all", "description": "Switch to Kraken discovery (all liquid coins)"},
    {"command": "universe_stocks", "description": "Toggle liquid xStocks"},
    {"command": "symbols", "description": "List active trading pairs"},
    {"command": "close", "description": "Close symbol (fee preview / confirm)"},
    {"command": "clear_positions", "description": "Paper close xStocks (or all) at BE"},
    {"command": "help", "description": "Command list"},
]


async def register_bot_commands(bot_token: str, *, timeout: float = 15.0) -> bool:
    """Best-effort setMyCommands so Telegram menu includes /universe /symbols."""
    token = (bot_token or "").strip()
    if not token:
        return False
    try:
        import httpx
    except ImportError:
        return False
    url = f"https://api.telegram.org/bot{token}/setMyCommands"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json={"commands": BOT_COMMAND_SPECS})
            if resp.status_code >= 400:
                logger.warning("setMyCommands status %s", resp.status_code)
                return False
            data = resp.json()
            ok = bool(data.get("ok"))
            if ok:
                logger.info("Telegram setMyCommands registered (%s cmds)", len(BOT_COMMAND_SPECS))
            return ok
    except Exception as exc:
        logger.warning("setMyCommands failed: %s", exc)
        return False


class TelegramCommandListener:
    """Long-poll getUpdates; only TELEGRAM_CHAT_ID; reply briefly per command."""

    def __init__(
        self,
        *,
        bot_token: str,
        chat_id: str,
        handlers: Dict[str, CommandHandler],
        callback_handlers: Optional[Dict[str, CallbackHandler]] = None,
        enabled: bool = True,
        timeout: float = 25.0,
        poll_timeout: int = 25,
    ) -> None:
        self.bot_token = (bot_token or "").strip()
        self.chat_id = str(chat_id or "").strip()
        self.handlers = handlers
        self.callback_handlers = dict(callback_handlers or {})
        self.enabled = bool(enabled)
        self.timeout = timeout
        self.poll_timeout = int(poll_timeout)
        self._offset: int = 0
        self._running = False
        self._seen_update_ids: set[int] = set()
        self._lock_fd = None

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.bot_token and self.chat_id)

    async def _send_reply(self, reply: Union[str, TelegramReply, None]) -> None:
        if not self.configured or reply is None:
            return
        if isinstance(reply, TelegramReply):
            text = reply.text or ""
            reply_markup = reply.reply_markup
            parse_mode = reply.parse_mode
        else:
            text = str(reply or "")
            reply_markup = None
            parse_mode = None
        if not text and not reply_markup:
            return
        try:
            import httpx
        except ImportError:
            logger.warning("httpx missing — telegram command reply skipped")
            return
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        # Split long replies so the full symbol list stays scrollable.
        chunks: List[str] = []
        body = text or ""
        while body:
            chunks.append(body[:3500])
            body = body[3500:]
        if not chunks:
            chunks = [""]
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                for i, chunk in enumerate(chunks):
                    payload: Dict[str, Any] = {
                        "chat_id": self.chat_id,
                        "text": chunk or "(empty)",
                        "disable_web_page_preview": True,
                    }
                    if parse_mode and i == 0:
                        payload["parse_mode"] = parse_mode
                    # Attach ▼ button only on the first (status) chunk.
                    if reply_markup and i == 0:
                        payload["reply_markup"] = reply_markup
                    resp = await client.post(url, json=payload)
                    if resp.status_code >= 400:
                        logger.warning(
                            "Telegram command reply status %s body=%s",
                            resp.status_code,
                            (resp.text or "")[:200],
                        )
        except Exception as exc:
            logger.warning("Telegram command reply failed: %s", exc)

    async def _answer_callback(self, callback_query_id: str, *, text: str = "") -> None:
        if not self.configured or not callback_query_id:
            return
        try:
            import httpx
        except ImportError:
            return
        url = f"https://api.telegram.org/bot{self.bot_token}/answerCallbackQuery"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                await client.post(
                    url,
                    json={"callback_query_id": callback_query_id, "text": (text or "")[:200]},
                )
        except Exception as exc:
            logger.debug("answerCallbackQuery failed: %s", exc)

    async def _get_updates(self) -> List[Dict[str, Any]]:
        try:
            import httpx
        except ImportError:
            return []
        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
        import json as _json
        params = {
            "offset": self._offset,
            "timeout": self.poll_timeout,
            "allowed_updates": _json.dumps(["message", "callback_query"]),
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout + self.poll_timeout + 5) as client:
                resp = await client.get(url, params=params)
                if resp.status_code >= 400:
                    logger.warning("getUpdates status %s", resp.status_code)
                    return []
                data = resp.json()
                if not data.get("ok"):
                    return []
                return list(data.get("result") or [])
        except Exception as exc:
            logger.debug("getUpdates error: %s", exc)
            return []

    async def handle_update(self, update: Dict[str, Any]) -> None:
        uid = int(update.get("update_id") or 0)
        if uid and uid in self._seen_update_ids:
            return
        if uid:
            self._seen_update_ids.add(uid)
            if len(self._seen_update_ids) > 500:
                # drop oldest-ish half (set has no order — reset when bloated)
                self._seen_update_ids = set(list(self._seen_update_ids)[-200:])
        cb = update.get("callback_query")
        if cb:
            await self._handle_callback(cb)
            return
        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        if chat_id != self.chat_id:
            logger.debug("Ignoring telegram update from chat_id=%s", chat_id)
            return
        text = str(msg.get("text") or "")
        parsed = parse_command(text)
        if parsed is None:
            return
        cmd, args = parsed
        handler = self.handlers.get(cmd)
        if handler is None:
            await self._send_reply(f"Unknown command /{cmd}")
            return
        try:
            reply = await handler(cmd, args)
        except Exception as exc:
            logger.exception("telegram command /%s failed", cmd)
            reply = f"/{cmd} error: {exc}"
        if reply:
            await self._send_reply(reply)

    async def _edit_message(
        self,
        *,
        chat_id: str,
        message_id: int,
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            import httpx
        except ImportError:
            return
        url = f"https://api.telegram.org/bot{self.bot_token}/editMessageText"
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": int(message_id),
            "text": (text or "")[:4090],
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code >= 400:
                    logger.warning(
                        "editMessageText status %s body=%s",
                        resp.status_code,
                        (resp.text or "")[:200],
                    )
        except Exception as exc:
            logger.warning("editMessageText failed: %s", exc)

    async def _handle_callback(self, cb: Dict[str, Any]) -> None:
        cq_id = str(cb.get("id") or "")
        data = str(cb.get("data") or "")
        msg = cb.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        message_id = msg.get("message_id")
        if chat_id != self.chat_id:
            await self._answer_callback(cq_id, text="Unauthorized")
            return

        if data in (STATUS_SYMBOLS_CALLBACK, STATUS_SYMBOLS_COLLAPSE) and message_id is not None:
            compact = _status_compact_body(str(msg.get("text") or ""))
            n_line = 0
            for line in compact.splitlines():
                if line.startswith("Symbols:") and "pairs" in line:
                    try:
                        n_line = int(line.split("Symbols:", 1)[1].split("pairs", 1)[0].strip())
                    except ValueError:
                        n_line = 0
                    break
            if data == STATUS_SYMBOLS_COLLAPSE:
                await self._answer_callback(cq_id, text="Collapsed")
                await self._edit_message(
                    chat_id=chat_id,
                    message_id=int(message_id),
                    text=compact,
                    reply_markup=symbols_expand_keyboard(n_line),
                )
                return
            handler = self.callback_handlers.get(STATUS_SYMBOLS_CALLBACK)
            if handler is None:
                await self._answer_callback(cq_id, text="Unknown button")
                return
            try:
                symbols_body = await handler(data)
            except Exception as exc:
                logger.exception("telegram callback %s failed", data)
                await self._answer_callback(cq_id, text="Error")
                await self._send_reply(f"Button error: {exc}")
                return
            symbols_text = symbols_body.text if isinstance(symbols_body, TelegramReply) else str(symbols_body or "")
            expanded = compact.rstrip() + "\n\n—— Symbols ——\n" + symbols_text.strip()
            if len(expanded) > 4090:
                expanded = expanded[:4080] + "\n… (truncated — /symbols)"
            await self._answer_callback(cq_id, text="Expanded")
            await self._edit_message(
                chat_id=chat_id,
                message_id=int(message_id),
                text=expanded,
                reply_markup=symbols_collapse_keyboard(n_line),
            )
            return

        handler = self.callback_handlers.get(data)
        if handler is None:
            await self._answer_callback(cq_id, text="Unknown button")
            return
        try:
            reply = await handler(data)
        except Exception as exc:
            logger.exception("telegram callback %s failed", data)
            await self._answer_callback(cq_id, text="Error")
            await self._send_reply(f"Button error: {exc}")
            return
        await self._answer_callback(cq_id, text="OK")
        if reply:
            await self._send_reply(reply)

    async def run(self, stop_event: Any) -> None:
        """Background long-poll until stop_event is set."""
        if not self.configured:
            logger.info("Telegram commands disabled or not configured — listener idle")
            return
        # One getUpdates consumer per bot token (stops duplicate /status replies).
        bot_id = self.bot_token.split(":", 1)[0]
        lock_path = f"/tmp/cruzbot_tg_{bot_id}.lock"
        try:
            self._lock_fd = open(lock_path, "a+", encoding="utf-8")
            fcntl.flock(self._lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_fd.seek(0)
            self._lock_fd.truncate()
            self._lock_fd.write(f"{os.getpid()}\n")
            self._lock_fd.flush()
        except BlockingIOError:
            logger.error(
                "Telegram getUpdates lock held (%s) — another poller owns this bot; listener idle",
                lock_path,
            )
            if self._lock_fd is not None:
                try:
                    self._lock_fd.close()
                except Exception:
                    pass
                self._lock_fd = None
            return
        except Exception as exc:
            logger.warning("Telegram lock open failed (continuing unlocked): %s", exc)

        # Never log full bot token URLs
        logging.getLogger("httpx").setLevel(logging.WARNING)

        self._running = True
        logger.info(
            "Telegram commands armed (chat_id=%s) — %s",
            self.chat_id,
            armed_commands_log_fragment(),
        )
        try:
            await register_bot_commands(self.bot_token)
        except Exception as exc:
            logger.debug("setMyCommands skipped: %s", exc)
        # Drop pending updates so we don't reply to stale commands after restart
        try:
            import httpx

            url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, params={"offset": -1, "timeout": 0})
                if resp.status_code < 400:
                    data = resp.json()
                    results = list((data or {}).get("result") or [])
                    if results:
                        self._offset = int(results[-1].get("update_id", 0)) + 1
        except Exception as exc:
            logger.debug("telegram offset warm-up skipped: %s", exc)

        try:
            while self._running and not stop_event.is_set():
                try:
                    updates = await self._get_updates()
                    for upd in updates:
                        uid = int(upd.get("update_id") or 0)
                        if uid >= self._offset:
                            self._offset = uid + 1
                        await self.handle_update(upd)
                except Exception as exc:
                    logger.warning("telegram poll loop error: %s", exc)
                    await _sleep_interruptible(stop_event, 3.0)
                if stop_event.is_set():
                    break
        finally:
            self._running = False
            if self._lock_fd is not None:
                try:
                    fcntl.flock(self._lock_fd.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
                try:
                    self._lock_fd.close()
                except Exception:
                    pass
                self._lock_fd = None
            logger.info("Telegram command listener stopped")

    def stop(self) -> None:
        self._running = False


async def _sleep_interruptible(stop_event: Any, seconds: float) -> None:
    import asyncio

    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except Exception:
        pass


def current_pid() -> int:
    return os.getpid()
