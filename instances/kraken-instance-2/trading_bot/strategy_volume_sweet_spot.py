"""Volume Sweet Spot strategy — RVOL breakout + low-volume retest; no ATR exits."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional, Sequence

from trading_bot.models import Action, AgentObservation, Bar, Decision
from trading_bot.structural_guardrails import required_min_tp_pct
from trading_bot.strategy import (
    GrokSentimentFilter,
    MarketRegimeCache,
    grok_allows_long,
    market_regime_blocks_retest,
    normalize_market_regime,
    regime_trade_cap_mult,
)
from trading_bot.utils.indicators import (
    check_l2_imbalance,
    check_mtf_align,
    check_regime_filter,
)
from trading_bot.utils.entry_proximity import (
    get_entry_proximity_from_obs,
    get_entry_threshold,
)

logger = logging.getLogger(__name__)


def structural_stop(swing_low: float, buffer_pct: float = 0.002) -> float:
    """SL = swing_low * (1 - buffer)."""
    return float(swing_low) * (1.0 - float(buffer_pct))


def min_tp_clearance_ok(
    entry: float,
    tp: float,
    *,
    min_tp_pct: float = 0.025,
    resistance: Optional[float] = None,
    min_clear_to_resistance_pct: float = 0.02,
) -> tuple[bool, str]:
    """Technical TP distance must be >= min_tp_pct and clear resistance by min_clear."""
    if entry <= 0 or tp is None:
        return False, "min_tp: invalid entry/tp"
    tp_pct = (float(tp) - float(entry)) / float(entry)
    if tp_pct + 1e-12 < float(min_tp_pct):
        return (
            False,
            f"min_tp: TP path {tp_pct:.4%} < required {float(min_tp_pct):.4%}",
        )
    if resistance is not None and resistance > 0:
        clear = (float(resistance) - float(entry)) / float(entry)
        if clear + 1e-12 < float(min_clear_to_resistance_pct):
            return (
                False,
                (
                    f"min_tp: clear-to-resistance {clear:.4%} < "
                    f"{float(min_clear_to_resistance_pct):.4%}"
                ),
            )
    return True, f"min_tp ok TP={tp_pct:.4%} (>= {float(min_tp_pct):.4%})"


def _bars_from_obs(obs: AgentObservation) -> list[Any]:
    bars = list(obs.recent_bars or [])
    if bars:
        return bars
    # Fall back to extras-provided OHLC snapshots
    raw = obs.indicators.extras.get("recent_bars_ohlcv")
    if isinstance(raw, (list, tuple)):
        return list(raw)
    return []


def _bar_field(b: Any, key: str, default: float = 0.0) -> float:
    if isinstance(b, dict):
        try:
            return float(b.get(key, default) or default)
        except (TypeError, ValueError):
            return default
    try:
        return float(getattr(b, key, default) or default)
    except (TypeError, ValueError):
        return default


def detect_breakout_and_retest(
    bars: Sequence[Any],
    *,
    vwap: Optional[float],
    ema_fast: Optional[float],
    ema_slow: Optional[float],
    rvol_breakout_mult: float,
    pullback_vol_frac: float,
    volume_sma_period: int = 20,
) -> tuple[bool, str, dict[str, Any]]:
    """
    Find a recent RVOL breakout above VWAP, then require current bar is a
    low-volume retest (pullback vol < frac * breakout vol) near VWAP/EMA.
    """
    meta: dict[str, Any] = {}
    if not bars or len(bars) < max(5, volume_sma_period // 2):
        # Allow extras-driven path without full history
        return False, "insufficient bars for sweet-spot detect", meta

    vols = [_bar_field(b, "volume") for b in bars]
    closes = [_bar_field(b, "close") for b in bars]
    opens = [_bar_field(b, "open") for b in bars]
    lows = [_bar_field(b, "low") for b in bars]
    highs = [_bar_field(b, "high") for b in bars]

    n = len(bars)
    # Volume SMA ending at each bar (use prior 20 excl current when possible)
    def rvol_at(i: int) -> float:
        start = max(0, i - volume_sma_period)
        window = vols[start:i]  # exclude bar i
        if not window:
            window = vols[start : i + 1]
        mean = sum(window) / max(1, len(window))
        if mean <= 0:
            return 0.0
        return vols[i] / mean

    # Search recent bars (not the last) for breakout spike
    search_from = max(1, n - 12)
    breakout_i = None
    for i in range(search_from, n - 1):  # exclude current bar as breakout
        rv = rvol_at(i)
        above = True
        if vwap is not None and vwap > 0:
            above = closes[i] >= float(vwap) * 0.999
        # Positive delta proxy: close > open on breakout bar
        bull = closes[i] > opens[i]
        if rv >= rvol_breakout_mult and above and bull:
            breakout_i = i
            # keep most recent qualifying breakout
    if breakout_i is None:
        return False, "no RVOL>thresh breakout above VWAP in lookback", meta

    bvol = vols[breakout_i]
    meta["breakout_idx"] = breakout_i
    meta["breakout_volume"] = bvol
    meta["breakout_rvol"] = rvol_at(breakout_i)
    meta["delta_proxy"] = "close>open"
    meta["breakout_close"] = closes[breakout_i]
    meta["breakout_low"] = lows[breakout_i]

    # Current bar = retest candidate — must NOT be the spike itself
    cur_vol = vols[-1]
    meta["pullback_volume"] = cur_vol
    if bvol <= 0:
        return False, "breakout volume invalid", meta
    if cur_vol >= bvol * pullback_vol_frac:
        return (
            False,
            (
                f"reject spike/no low-vol retest: pullback_vol={cur_vol:.4g} "
                f">= {pullback_vol_frac:.0%} of breakout_vol={bvol:.4g}"
            ),
            meta,
        )

    # Price near VWAP or EMA9/EMA21 (pullback toward structure)
    close = closes[-1]
    near = False
    anchors = []
    for name, level in (("VWAP", vwap), ("EMA9", ema_fast), ("EMA21", ema_slow)):
        if level is None or level <= 0:
            continue
        dist = abs(close - float(level)) / float(level)
        anchors.append((name, dist))
        if dist <= 0.004:  # within 0.4%
            near = True
    # Also accept if price pulled back from breakout high toward VWAP (between VWAP and breakout)
    if not near and vwap and vwap > 0:
        brk_c = closes[breakout_i]
        if min(float(vwap), brk_c) <= close <= max(float(vwap), brk_c) * 1.002:
            near = True
            meta["retest_zone"] = "between_vwap_and_breakout"
    if not near:
        return False, f"pullback not near VWAP/EMA (anchors={anchors})", meta

    # Swing low of setup: min low from breakout through current
    swing = min(lows[breakout_i:])
    meta["swing_low"] = swing
    meta["retest_ok"] = True
    return True, "RVOL Breakout + Low-Volume VWAP Retest", meta



def check_time_of_day_gate(
    now: Optional[datetime] = None,
    *,
    enabled: bool = True,
) -> tuple[bool, str]:
    """Block new entries during UTC rollover chop: 23:00–00:30 UTC inclusive-start.

    Window: hour==23 OR (hour==0 and minute < 30). Returns (ok, detail).
    """
    if not enabled:
        return True, "tod_gate disabled"
    from datetime import datetime, timezone

    if isinstance(now, str):
        try:
            now = datetime.fromisoformat(now.replace("Z", "+00:00"))
        except ValueError:
            now = None
    now = now or datetime.now(timezone.utc)
    if getattr(now, "tzinfo", None) is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    h, m = now.hour, now.minute
    in_window = h == 23 or (h == 0 and m < 30)
    if in_window:
        return (
            False,
            f"tod_gate: block entries 23:00–00:30 UTC (now={now.strftime('%H:%M')}Z)",
        )
    return True, f"tod_gate ok ({now.strftime('%H:%M')}Z)"


def check_daily_drawdown_circuit(
    day_pnl: Optional[float],
    day_start_equity: Optional[float],
    *,
    limit_pct: float = 0.03,
    enabled: bool = True,
) -> tuple[bool, str]:
    """3% daily drawdown circuit vs UTC-day realized SQLite PnL.

    Trips when day_pnl <= -limit_pct * day_start_equity. Missing inputs → allow
    (caller should populate extras from state_store / paper book).
    """
    if not enabled:
        return True, "daily_dd_circuit disabled"
    if day_pnl is None or day_start_equity is None:
        return True, "daily_dd_circuit: no day_pnl/equity yet"
    try:
        pnl = float(day_pnl)
        start = float(day_start_equity)
        lim = float(limit_pct)
    except (TypeError, ValueError):
        return True, "daily_dd_circuit: invalid inputs"
    if start <= 0 or lim <= 0:
        return True, "daily_dd_circuit: non-positive start/limit"
    threshold = -lim * start
    if pnl <= threshold + 1e-12:
        dd = (-pnl / start) if start else 0.0
        return (
            False,
            f"daily_dd_circuit: UTC-day PnL ${pnl:.2f} <= -{lim:.0%} of "
            f"start ${start:.2f} (dd={dd:.2%}); entries locked",
        )
    return True, f"daily_dd_circuit ok day_pnl=${pnl:.2f} start=${start:.2f}"

class VolumeSweetSpotEngine:
    """
    Entry: RVOL expansion breakout + low-volume retest (absorption).
    Exits are structural (handled by bracket manager) — engine only emits BUY
    with swing SL and 2.5R TP; never ATR trail.
    """

    def __init__(
        self,
        *,
        rvol_breakout_mult: float = 2.0,
        pullback_vol_frac: float = 0.5,
        min_tp_pct: float = 0.025,
        min_clear_to_resistance_pct: float = 0.02,
        swing_sl_buffer_pct: float = 0.002,
        maker_fee_rate: float = 0.005,
        fee_to_target_mult: float = 2.5,
        tp1_rr: float = 1.0,
        tp2_rr: float = 2.5,
        volume_sma_period: int = 20,
        min_confidence: float = 55.0,
        # Intelligence gates — main wires True from settings; default False keeps legacy tests green
        l2_imbalance_enabled: bool = False,
        l2_imbalance_min_ratio: float = 1.2,
        regime_filter_enabled: bool = False,
        adx_min: float = 25.0,
        chop_max: float = 60.0,
        mtf_align_enabled: bool = False,
        # Phase 1 extras (precomputed by main from lead-lag CVD / short-liq)
        phase1_gate_enabled: bool = False,
        # Low-token intel: ToD rollover gate + SQLite UTC-day PnL circuit
        tod_gate_enabled: bool = False,
        # Canonical profile switch. When true, the TOD blackout is skipped on every tick.
        disable_tod_gate: Optional[bool] = None,
        daily_dd_sqlite_enabled: bool = False,
        daily_dd_limit_pct: float = 0.03,
        sentiment_filter: Optional[GrokSentimentFilter] = None,
        # Phase 2: nightly ATR+ADX market regime gate (default off keeps legacy tests green)
        regime_gate_enabled: bool = False,
        market_regime: str = "RANGING",
        active_params_path: Optional[str] = None,
    ) -> None:
        self.rvol_breakout_mult = rvol_breakout_mult
        self.pullback_vol_frac = pullback_vol_frac
        self.min_tp_pct = min_tp_pct
        self.min_clear_to_resistance_pct = min_clear_to_resistance_pct
        self.swing_sl_buffer_pct = swing_sl_buffer_pct
        self.maker_fee_rate = maker_fee_rate
        self.fee_to_target_mult = fee_to_target_mult
        self.tp1_rr = tp1_rr
        self.tp2_rr = tp2_rr
        self.volume_sma_period = volume_sma_period
        self.min_confidence = min_confidence
        self.l2_imbalance_enabled = l2_imbalance_enabled
        self.l2_imbalance_min_ratio = l2_imbalance_min_ratio
        self.regime_filter_enabled = regime_filter_enabled
        self.adx_min = adx_min
        self.chop_max = chop_max
        self.mtf_align_enabled = mtf_align_enabled
        self.phase1_gate_enabled = phase1_gate_enabled
        if disable_tod_gate is None:
            self.tod_gate_enabled = bool(tod_gate_enabled)
            self.disable_tod_gate = not self.tod_gate_enabled
        else:
            self.disable_tod_gate = bool(disable_tod_gate)
            self.tod_gate_enabled = not self.disable_tod_gate
        self.daily_dd_sqlite_enabled = daily_dd_sqlite_enabled
        self.daily_dd_limit_pct = daily_dd_limit_pct
        self.sentiment_filter = sentiment_filter
        self.regime_gate_enabled = bool(regime_gate_enabled)
        self.active_params_path = active_params_path
        self._regime_cache = MarketRegimeCache(
            active_params_path,
            initial=market_regime,
            default="RANGING",
        )
        self.market_regime = normalize_market_regime(
            market_regime, default="RANGING"
        )

    @property
    def regime_trade_cap_mult(self) -> float:
        """0.5 under HIGH_VOLATILITY when gate enabled; else 1.0. In-memory <1ms."""
        if not self.regime_gate_enabled:
            return 1.0
        return float(regime_trade_cap_mult(self.market_regime))

    def set_market_regime(self, regime: Any, *, features: Optional[dict] = None) -> None:
        """Hot-apply regime from optimizer callback / tests (no restart)."""
        self.market_regime = normalize_market_regime(regime, default="RANGING")
        self._regime_cache.set_regime(self.market_regime, features=features)

    def maybe_reload_regime(self) -> str:
        """Reload market_regime when active_params.json mtime changes."""
        if not self.active_params_path:
            return self.market_regime
        label = self._regime_cache.maybe_reload()
        self.market_regime = label
        return label

    def maybe_reload_market_regime(self) -> str:
        """Alias used by main.py /regime hot-reload path."""
        return self.maybe_reload_regime()

    def evaluate_entry(self, obs: AgentObservation) -> Decision:
        """Entry decision path used by the running bot (alias of reason)."""
        return self.reason(obs)

    def reason(self, obs: AgentObservation) -> Decision:
        ind = obs.indicators
        symbol = obs.symbol
        close = float(ind.close or 0)
        if close <= 0:
            return Decision.hold(symbol, "sweet_spot: invalid close")

        extras = ind.extras or {}

        # --- Phase 2: nightly market regime (ATR+ADX) — <1ms in-memory lookup ---
        self.maybe_reload_regime()
        blocked, reg_detail = market_regime_blocks_retest(
            self.market_regime, enabled=self.regime_gate_enabled
        )
        if blocked:
            logger.info("SKIP %s | %s", symbol, reg_detail)
            return Decision.hold(symbol, reg_detail)

        # --- Time-of-day gate (UTC rollover chop 23:00–00:30) ---
        # DISABLE_TOD_GATE is checked directly so aggressive skips the blackout
        # on every tick, including the 23:00–00:30 UTC rollover window.
        if not self.disable_tod_gate:
            now_tod = extras.get("now_utc")
            ok_tod, tod_detail = check_time_of_day_gate(now_tod, enabled=True)
            if not ok_tod:
                logger.info("SKIP %s | %s", symbol, tod_detail)
                return Decision.hold(symbol, tod_detail)

        # --- 3% UTC-day SQLite PnL circuit breaker ---
        if self.daily_dd_sqlite_enabled:
            day_pnl = extras.get("utc_day_pnl", extras.get("day_pnl"))
            day_start_eq = extras.get("day_start_equity")
            ok_dd, dd_detail = check_daily_drawdown_circuit(
                day_pnl,
                day_start_eq,
                limit_pct=self.daily_dd_limit_pct,
                enabled=True,
            )
            if not ok_dd:
                logger.info("SKIP %s | %s", symbol, dd_detail)
                return Decision.hold(symbol, dd_detail)

        # Fast reject: sitting on the high-volume spike candle (no retest)
        if extras.get("on_breakout_spike") is True and extras.get("retest_ok") is not True:
            return Decision.hold(
                symbol,
                "sweet_spot: reject entry on spike without low-vol retest",
            )

        bars = _bars_from_obs(obs)
        meta: dict[str, Any] = {}
        ok = False
        detail = ""

        # Prefer precomputed extras (tests / enriched feed)
        if extras.get("retest_ok") is True and extras.get("breakout_volume"):
            bvol = float(extras["breakout_volume"])
            pvol = float(extras.get("pullback_volume", ind.volume or 0))
            if bvol > 0 and pvol < bvol * self.pullback_vol_frac:
                ok = True
                detail = str(
                    extras.get("entry_reason")
                    or "RVOL Breakout + Low-Volume VWAP Retest"
                )
                meta = {
                    "breakout_volume": bvol,
                    "pullback_volume": pvol,
                    "swing_low": extras.get("swing_low"),
                    "delta_proxy": extras.get("delta_proxy", "close>open"),
                    "breakout_rvol": extras.get("breakout_rvol")
                    or extras.get("volume_ratio"),
                }
            else:
                return Decision.hold(
                    symbol,
                    (
                        f"sweet_spot: reject spike/no low-vol retest "
                        f"(pullback_vol={pvol:.4g} vs breakout={bvol:.4g})"
                    ),
                )
        else:
            # RVOL gate from volume_ratio when bars thin
            vol_ratio = extras.get("volume_ratio")
            ok_detect, detail, meta = detect_breakout_and_retest(
                bars,
                vwap=ind.vwap,
                ema_fast=ind.ema_fast,
                ema_slow=ind.ema_slow,
                rvol_breakout_mult=self.rvol_breakout_mult,
                pullback_vol_frac=self.pullback_vol_frac,
                volume_sma_period=self.volume_sma_period,
            )
            if not ok_detect:
                # If no bars, require explicit volume_ratio breakout + retest extras
                if not bars and isinstance(vol_ratio, (int, float)):
                    if float(vol_ratio) < self.rvol_breakout_mult:
                        return Decision.hold(
                            symbol,
                            f"sweet_spot: RVOL {float(vol_ratio):.2f}x < {self.rvol_breakout_mult:.2f}x",
                        )
                    return Decision.hold(
                        symbol,
                        "sweet_spot: reject entry on spike without low-vol retest",
                    )
                return Decision.hold(symbol, f"sweet_spot: {detail}")
            ok = True
            if meta.get("delta_proxy"):
                logger.info(
                    "sweet_spot %s delta_proxy=%s (buy>sell unavailable)",
                    symbol,
                    meta["delta_proxy"],
                )

        if not ok:
            return Decision.hold(symbol, f"sweet_spot: {detail or 'no setup'}")

        # --- Intelligence gates (L2 imbalance, regime, MTF align) ---
        if self.l2_imbalance_enabled:
            ratio = extras.get("l2_imbalance_ratio")
            if ratio is None and extras.get("l2_imbalance") is not None:
                ratio = extras.get("l2_imbalance")
            try:
                ratio_f = float(ratio) if ratio is not None else None
            except (TypeError, ValueError):
                ratio_f = None
            ok_l2, l2_detail = check_l2_imbalance(
                ratio_f,
                min_ratio=self.l2_imbalance_min_ratio,
                enabled=True,
            )
            if not ok_l2:
                logger.info("SKIP %s | %s", symbol, l2_detail)
                return Decision.hold(symbol, l2_detail)

        if self.regime_filter_enabled:
            adx_v = extras.get("adx")
            chop_v = extras.get("chop", extras.get("choppiness"))
            try:
                adx_f = float(adx_v) if adx_v is not None else None
            except (TypeError, ValueError):
                adx_f = None
            try:
                chop_f = float(chop_v) if chop_v is not None else None
            except (TypeError, ValueError):
                chop_f = None
            ok_reg, reg_detail = check_regime_filter(
                adx_f,
                chop_f,
                adx_min=self.adx_min,
                chop_max=self.chop_max,
                enabled=True,
            )
            if not ok_reg:
                logger.info("SKIP %s | %s", symbol, reg_detail)
                return Decision.hold(symbol, reg_detail)

        if self.mtf_align_enabled:
            ema_1h = None
            ema_4h = None
            if obs.htf is not None:
                ema_1h = obs.htf.ema_200_1h if obs.htf.ema_200_1h is not None else obs.htf.ema_200
                ema_4h = obs.htf.ema_200_4h
            if ema_1h is None and extras.get("ema_200_1h") is not None:
                try:
                    ema_1h = float(extras["ema_200_1h"])
                except (TypeError, ValueError):
                    ema_1h = None
            if ema_4h is None and extras.get("ema_200_4h") is not None:
                try:
                    ema_4h = float(extras["ema_200_4h"])
                except (TypeError, ValueError):
                    ema_4h = None
            ok_mtf, mtf_detail = check_mtf_align(
                close, ema_1h, ema_4h, enabled=True
            )
            if not ok_mtf:
                logger.info("SKIP %s | %s", symbol, mtf_detail)
                return Decision.hold(symbol, mtf_detail)

        if self.phase1_gate_enabled and extras.get("phase1_allow") is False:
            p1 = str(extras.get("phase1_reason") or "phase1: blocked")
            logger.info("SKIP %s | %s", symbol, p1)
            return Decision.hold(symbol, p1)

        swing_low = meta.get("swing_low") or extras.get("swing_low")
        if swing_low is None and obs.htf and obs.htf.swing_low is not None:
            swing_low = obs.htf.swing_low
        if swing_low is None:
            # last-resort: 1% below close
            swing_low = close * 0.99
        swing_low = float(swing_low)
        if swing_low <= 0 or swing_low >= close:
            return Decision.hold(
                symbol,
                f"sweet_spot: invalid swing_low={swing_low} vs close={close}",
            )

        sl = structural_stop(swing_low, self.swing_sl_buffer_pct)
        risk = close - sl
        if risk <= 0:
            return Decision.hold(symbol, "sweet_spot: non-positive risk to SL")

        tp2 = close + self.tp2_rr * risk
        resistance = None
        if obs.htf and obs.htf.resistance is not None:
            resistance = float(obs.htf.resistance)
        elif extras.get("resistance") is not None:
            try:
                resistance = float(extras["resistance"])
            except (TypeError, ValueError):
                resistance = None

        effective_min_tp = required_min_tp_pct(
            self.min_tp_pct, self.maker_fee_rate, self.fee_to_target_mult
        )
        ok_tp, tp_detail = min_tp_clearance_ok(
            close,
            tp2,
            min_tp_pct=effective_min_tp,
            resistance=resistance,
            min_clear_to_resistance_pct=self.min_clear_to_resistance_pct,
        )
        if not ok_tp:
            logger.info("SKIP %s | %s", symbol, tp_detail)
            return Decision.hold(symbol, tp_detail)

        tp1 = close + self.tp1_rr * risk
        # Prefer ATR brackets as active SL/TP when enabled (sweet-spot TP1/TP2 math retained as fallback)
        atr_mode = bool(getattr(self, "atr_bracket_exits", True))
        atr_v = None
        try:
            if ind.atr is not None:
                atr_v = float(ind.atr)
        except Exception:
            atr_v = None
        if atr_mode and atr_v and atr_v > 0:
            from trading_bot.utils.decision_filters import atr_bracket_levels

            sl_m = float(getattr(self, "atr_bracket_sl_mult", 1.8) or 1.8)
            tp_m = float(getattr(self, "atr_bracket_tp_mult", 3.0) or 3.0)
            sl_floor = float(getattr(self, "atr_bracket_sl_min_pct", 0.01) or 0.01)
            sl_ceil = float(getattr(self, "atr_bracket_sl_max_pct", 0.012) or 0.012)
            tp_floor = float(getattr(self, "atr_bracket_tp_min_pct", 0.02) or 0.02)
            sl, tp2 = atr_bracket_levels(
                close, atr_v, sl_mult=sl_m, tp_mult=tp_m, sl_min_pct=sl_floor, tp_min_pct=tp_floor, sl_max_pct=sl_ceil
            )
            tp1 = close + 1.0 * (close - sl)
            tp_detail = (
                f"ATR_BRACKET SL=max({sl_floor:.3%}*{close:.6g},{sl_m}*ATR) "
                f"TP={tp_m}*ATR atr={atr_v:.6g}"
            )
        # Hard TP floor + sub-fee reject
        calc_tp_pct = (float(tp2) - float(close)) / float(close) if close else 0.0
        if calc_tp_pct + 1e-12 < 0.015:
            detail = f"sub_fee_tp: TP {calc_tp_pct:.2%} < 1.50% — reject"
            logger.info("SKIP %s | %s", symbol, detail)
            return Decision.hold(symbol, detail)
        tp_target_pct = max(calc_tp_pct, 0.020)
        tp2 = float(close) * (1.0 + tp_target_pct)
        if tp1 is not None and float(tp1) < float(close) * 1.01:
            tp1 = float(close) * 1.01
        tp_detail = f"{tp_detail}; tp_floor={tp_target_pct:.2%}"
        # Cap SL distance at sl_max (default 1.2%) even on structural stops
        try:
            sl_ceil = float(getattr(self, "atr_bracket_sl_max_pct", 0.012) or 0.012)
            if close > 0 and sl is not None and sl_ceil > 0:
                max_sl_dist = close * sl_ceil
                if close - float(sl) > max_sl_dist + 1e-12:
                    sl = close - max_sl_dist
        except Exception:
            pass

        conf = max(self.min_confidence, min(100.0, 60.0 + float(meta.get("breakout_rvol") or 2) * 5))
        reason = (
            f"BUY {detail}; SL={sl:.6g}; "
            f"TP1={tp1:.6g}; TP2={tp2:.6g}; {tp_detail}; "
            f"delta_proxy={meta.get('delta_proxy', 'n/a')}"
        )
        # Gate longs: block on Grok SELL, or HOLD with confidence > 0.6 (missing filter → allow).
        ok_grok, grok_detail = grok_allows_long(self.sentiment_filter)
        if not ok_grok:
            logger.info("SKIP %s | %s", symbol, grok_detail)
            return Decision.hold(symbol, grok_detail)
        reason = (
            f"{reason}; {grok_detail}; market_regime={self.market_regime} "
            f"cap_mult={self.regime_trade_cap_mult:.2f}"
        )
        # Entry proximity gate: require score >= active threshold (same as /status)
        sent = None
        sf = self.sentiment_filter
        if sf is not None:
            raw = getattr(sf, "latest_sentiment", None)
            if isinstance(raw, dict):
                sent = raw
        from trading_bot.utils.decision_filters import effective_entry_threshold, extract_adx

        adx_now = extract_adx(extras)
        base_thresh = get_entry_threshold()
        thresh, raised = effective_entry_threshold(
            base_thresh,
            adx_now,
            adx_floor=float(getattr(self, "adx_threshold_floor", 20.0) or 20.0),
            raise_by=float(getattr(self, "adx_threshold_raise", 15.0) or 15.0),
        )
        if raised:
            logger.info(
                "ADX threshold raised %s: ADX=%.1f → ENTRY_THRESHOLD %.0f→%.0f",
                symbol,
                float(adx_now or 0),
                base_thresh,
                thresh,
            )
        prox = get_entry_proximity_from_obs(
            obs,
            sentiment=sent,
            rvol_breakout_mult=float(self.rvol_breakout_mult),
            long_threshold=thresh,
        )
        score = float(prox.get("score") or 0.0)
        if score < thresh:
            detail_prox = (
                f"sweet_spot: entry proximity {score:.1f}% < threshold {thresh:.0f}%"
            )
            logger.info("SKIP %s | %s", symbol, detail_prox)
            return Decision.hold(symbol, detail_prox)

        # Stash TP1 on limit_price unused field? Use quantity=None; TP1 via reasoning parse is fragile.
        # Put TP1 into Decision via take_profit as TP2 (final); main stores TP1 from 1R math.
        return Decision(
            action=Action.BUY,
            symbol=symbol,
            confidence=round(conf, 1),
            stop_loss=round(sl, 8),
            take_profit=round(tp2, 8),
            limit_price=round(close, 8),  # post-only limit at mark
            reasoning=reason,
        )

    def get_entry_proximity(self, obs: AgentObservation) -> dict:
        """Long-only entry proximity score 0–100 for Telegram /status (<1ms)."""
        sent = None
        sf = self.sentiment_filter
        if sf is not None:
            raw = getattr(sf, "latest_sentiment", None)
            if isinstance(raw, dict):
                sent = raw
        return get_entry_proximity_from_obs(
            obs,
            sentiment=sent,
            rvol_breakout_mult=float(self.rvol_breakout_mult),
            long_threshold=get_entry_threshold(),
        )

