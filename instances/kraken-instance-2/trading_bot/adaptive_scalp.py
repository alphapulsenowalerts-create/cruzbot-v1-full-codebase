"""Adaptive Quick-Scalp Engine + Rolling Performance Memory + Stagnant exit."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

QUICK_TP_PCT = 0.02  # min 2% floor (was 1% micro-TP)
QUICK_SL_PCT = 0.006
# Aligned with fee-buffer trail (was arm +0.5% / lock +0.25% — too thin vs RT fees)
MICRO_ARM_PCT = 0.0085
MICRO_LOCK_PCT = 0.0085
STAGNANT_BAND = 0.002
STAGNANT_SECONDS = 12 * 60
STAGNANT_RT_FEE_PCT = 0.0026  # ~0.26% Kraken-ish round-trip
AGGRESSIVE_THRESH = 35.0
TIGHTENED_THRESH = 55.0
MEMORY_SIZE = 5
WINRATE_FLOOR = 0.40
CONSEC_WINS_RESTORE = 3


def wants_elite_risk(settings: Any) -> bool:
    """Elite ATR+fee-lock preferred over quick-scalp when enabled (default on)."""
    return bool(getattr(settings, "elite_risk_enabled", True))


def wants_quick_scalp(settings: Any) -> bool:
    profile = str(getattr(settings, "trade_profile", "") or "").strip().lower()
    if profile in {"aggressive", "agg", "a"}:
        return True
    try:
        return float(getattr(settings, "entry_threshold", 99) or 99) <= 35.0
    except (TypeError, ValueError):
        return False


def quick_scalp_brackets(entry: float, *, short: bool = False) -> Tuple[float, float]:
    """Return (stop_loss, take_profit)."""
    e = float(entry)
    if short:
        return e * (1.0 + QUICK_SL_PCT), e * (1.0 - QUICK_TP_PCT)
    return e * (1.0 - QUICK_SL_PCT), e * (1.0 + QUICK_TP_PCT)


def unrealized_pnl_pct(entry: float, mark: float, *, short: bool = False) -> float:
    e = float(entry)
    if e <= 0:
        return 0.0
    if short:
        return (e - float(mark)) / e
    return (float(mark) - e) / e


def micro_trail_lock_sl(entry: float, *, short: bool = False) -> float:
    e = float(entry)
    if short:
        return e * (1.0 - MICRO_LOCK_PCT)
    return e * (1.0 + MICRO_LOCK_PCT)


def maybe_micro_trail_sl(
    entry: float,
    mark: float,
    current_sl: Optional[float],
    *,
    short: bool = False,
) -> Optional[float]:
    """If UPL >= +0.5%, return tightened SL (entry±0.25%); else None."""
    if float(entry) <= 0:
        return None
    if unrealized_pnl_pct(entry, mark, short=short) < MICRO_ARM_PCT:
        return None
    lock = micro_trail_lock_sl(entry, short=short)
    if current_sl is None:
        return lock
    cur = float(current_sl)
    if short:
        return lock if lock < cur else None  # tighten short SL downward
    return lock if lock > cur else None  # tighten long SL upward


def estimate_net_pnl(
    entry: float,
    mark: float,
    qty: float,
    *,
    short: bool = False,
    rt_fee_pct: float = STAGNANT_RT_FEE_PCT,
) -> float:
    q = abs(float(qty))
    e = float(entry)
    m = float(mark)
    gross = (e - m) * q if short else (m - e) * q
    fees = e * q * float(rt_fee_pct)
    return gross - fees


class StagnantTracker:
    """Per-symbol timer while |pnl%| stays in ±0.2%."""

    def __init__(self) -> None:
        self._since: Dict[str, float] = {}

    def update(self, symbol: str, pnl_pct: float, now: Optional[float] = None) -> float:
        """Return seconds continuously stagnant (0 if outside band)."""
        now = time.monotonic() if now is None else float(now)
        sym = symbol.upper()
        if abs(float(pnl_pct)) > STAGNANT_BAND:
            self._since.pop(sym, None)
            return 0.0
        if sym not in self._since:
            self._since[sym] = now
        return max(0.0, now - self._since[sym])

    def clear(self, symbol: str) -> None:
        self._since.pop(symbol.upper(), None)

    def should_exit(self, symbol: str, pnl_pct: float, now: Optional[float] = None) -> bool:
        return self.update(symbol, pnl_pct, now=now) > STAGNANT_SECONDS


class SmartMemory:
    """Rolling last-5 win/loss → bump/restore AGGRESSIVE entry threshold."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data = self._load()

    def _load(self) -> Dict[str, Any]:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except Exception:
                pass
        return {"outcomes": [], "tightened": False, "session_wins": 0, "session_losses": 0}

    def _save(self) -> None:
        self.path.write_text(json.dumps(self._data, indent=2))

    @property
    def outcomes(self) -> List[str]:
        return list(self._data.get("outcomes") or [])

    @property
    def tightened(self) -> bool:
        return bool(self._data.get("tightened"))

    def record_close(
        self,
        *,
        won: bool,
        symbol: str = "",
        pnl: Optional[float] = None,
        tag: Optional[str] = None,
    ) -> Dict[str, Any]:
        outs: List[str] = list(self._data.get("outcomes") or [])
        # NET_FEE_FLAT: gross>0 but net loss from fees — not a CB streak loss,
        # but DOES count toward session W/L (user-facing stop-out / fee scrape).
        if str(tag or "").lower() in ("net_fee_flat", "flat", "fee_flat"):
            outs.append("flat")
            self._data["last_tag"] = "NET_FEE_FLAT"
            self._data["session_losses"] = int(self._data.get("session_losses") or 0) + 1
        else:
            outs.append("win" if won else "loss")
            self._data["last_tag"] = "win" if won else "loss"
            if won:
                self._data["session_wins"] = int(self._data.get("session_wins") or 0) + 1
            else:
                self._data["session_losses"] = int(self._data.get("session_losses") or 0) + 1
        outs = outs[-MEMORY_SIZE:]
        self._data["outcomes"] = outs
        self._data["last_symbol"] = symbol
        self._data["last_pnl"] = pnl
        self._save()
        return self.evaluate()

    def session_record(self) -> Tuple[int, int, Optional[float]]:
        """Return (wins, losses, win_rate_pct 0-100 or None if no closed trades)."""
        w = int(self._data.get("session_wins") or 0)
        l = int(self._data.get("session_losses") or 0)
        n = w + l
        if n <= 0:
            return w, l, None
        return w, l, 100.0 * w / n

    def reset_session(self) -> None:
        """Clear rolling memory + session win/loss (paper reset/wipe)."""
        self._data = {
            "outcomes": [],
            "tightened": False,
            "session_wins": 0,
            "session_losses": 0,
        }
        self._save()
        logger.warning("[SMART_MEMORY] session win-rate reset (paper reset/wipe)")

    def winrate(self) -> Tuple[int, int, float]:
        outs = [o for o in self.outcomes if o in ("win", "loss")]
        n = len(outs)
        wins = sum(1 for o in outs if o == "win")
        rate = (wins / n) if n else 0.0
        return wins, n, rate

    def consecutive_wins(self) -> int:
        n = 0
        for o in reversed(self.outcomes):
            if o != "win":
                break
            n += 1
        return n

    def consecutive_losses(self) -> int:
        n = 0
        for o in reversed(self.outcomes):
            if o != "loss":
                break
            n += 1
        return n

    def clear_circuit_breaker_streak(self) -> int:
        """Zero consecutive-loss streak for CB (keeps session W/L totals). Returns prior streak."""
        prior = self.consecutive_losses()
        outs = list(self._data.get("outcomes") or [])
        while outs and outs[-1] == "loss":
            outs.pop()
        self._data["outcomes"] = outs
        self._save()
        logger.warning(
            "[SMART_MEMORY] circuit-breaker streak cleared (was %s consecutive losses)",
            prior,
        )
        return prior


    def evaluate(self) -> Dict[str, Any]:
        """Return action dict: {action: none|tighten|restore, wins, n, rate, threshold}."""
        wins, n, rate = self.winrate()
        consec = self.consecutive_wins()
        result: Dict[str, Any] = {
            "action": "none",
            "wins": wins,
            "n": n,
            "rate": rate,
            "consec_wins": consec,
            "threshold": None,
            "tightened": self.tightened,
        }
        if n >= MEMORY_SIZE and rate < WINRATE_FLOOR and not self.tightened:
            self._data["tightened"] = True
            self._save()
            result["action"] = "tighten"
            result["threshold"] = TIGHTENED_THRESH
            result["tightened"] = True
            logger.warning(
                "[SMART_MEMORY] winrate=%s/%s → threshold %.0f→%.0f",
                wins,
                n,
                AGGRESSIVE_THRESH,
                TIGHTENED_THRESH,
            )
        elif self.tightened and consec >= CONSEC_WINS_RESTORE:
            self._data["tightened"] = False
            self._save()
            result["action"] = "restore"
            result["threshold"] = AGGRESSIVE_THRESH
            result["tightened"] = False
            logger.warning(
                "[SMART_MEMORY] %s consecutive wins → threshold %.0f→%.0f",
                consec,
                TIGHTENED_THRESH,
                AGGRESSIVE_THRESH,
            )
        return result

    def status_note(self) -> Optional[str]:
        if not self.tightened:
            return None
        wins, n, _ = self.winrate()
        return f"smart-memory tightened ({wins}/{n} wins)"
