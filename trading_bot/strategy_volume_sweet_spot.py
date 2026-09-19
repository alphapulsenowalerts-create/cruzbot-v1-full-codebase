"""Volume Sweet Spot strategy — RVOL breakout + low-volume retest; no ATR exits."""

from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from trading_bot.models import Action, AgentObservation, Bar, Decision
from trading_bot.structural_guardrails import required_min_tp_pct
from trading_bot.utils.indicators import (
    check_cvd_divergence,
    check_l2_imbalance,
    check_liq_sweep,
    check_mtf_align,
    check_regime_filter,
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
        # Phase 1 CVD / liquidation-sweep gates (default False keeps legacy tests green)
        cvd_divergence_enabled: bool = False,
        liq_sweep_required: bool = False,
        liq_sweep_short_usd: float = 50_000.0,
        cvd_warmup_fail_closed: bool = True,
        leadlag: Any = None,
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
        self.cvd_divergence_enabled = bool(cvd_divergence_enabled)
        self.liq_sweep_required = bool(liq_sweep_required)
        self.liq_sweep_short_usd = float(liq_sweep_short_usd)
        self.cvd_warmup_fail_closed = bool(cvd_warmup_fail_closed)
        self.leadlag = leadlag  # optional PerpLeadLagEngine; strategy reads snapshots only

    def set_leadlag(self, leadlag: Any) -> None:
        """Wire lead-lag/CVD/liq engine after construction (main constructs leadlag later)."""
        self.leadlag = leadlag

    def _resolve_cvd_liq(self, obs: AgentObservation) -> tuple[dict, dict]:
        """Prefer extras snapshots (tests); else non-blocking leadlag snapshot reads."""
        extras = obs.indicators.extras or {}
        cvd: dict = {}
        liq: dict = {}
        raw_cvd = extras.get("cvd_snapshot")
        if isinstance(raw_cvd, dict):
            cvd = dict(raw_cvd)
        else:
            # Flat extras keys
            if "cvd_5m" in extras or "cvd_1m" in extras:
                cvd = {
                    "cvd_1m": extras.get("cvd_1m"),
                    "cvd_5m": extras.get("cvd_5m"),
                    "ready": extras.get("cvd_ready", True),
                    "updated_at": extras.get("cvd_updated_at"),
                }
        raw_liq = extras.get("liq_snapshot")
        if isinstance(raw_liq, dict):
            liq = dict(raw_liq)
        else:
            if "short_liq_1m_usd" in extras or "long_liq_1m_usd" in extras:
                liq = {
                    "short_liq_1m_usd": extras.get("short_liq_1m_usd"),
                    "long_liq_1m_usd": extras.get("long_liq_1m_usd"),
                    "ready": extras.get("liq_ready", True),
                }
        # Live engine snapshots (non-blocking)
        if self.leadlag is not None:
            try:
                if not cvd and hasattr(self.leadlag, "get_cvd_snapshot"):
                    cvd = dict(self.leadlag.get_cvd_snapshot(obs.symbol) or {})
                if not liq and hasattr(self.leadlag, "get_liq_snapshot"):
                    liq = dict(self.leadlag.get_liq_snapshot(obs.symbol) or {})
            except Exception:
                pass
        return cvd, liq

    def _setup_bar_open_close(self, obs: AgentObservation, bars: list) -> tuple[Optional[float], Optional[float]]:
        """Prefer explicit 5m setup bar extras; else last recent bar open/close."""
        extras = obs.indicators.extras or {}
        if extras.get("setup_bar_open") is not None and extras.get("setup_bar_close") is not None:
            try:
                return float(extras["setup_bar_open"]), float(extras["setup_bar_close"])
            except (TypeError, ValueError):
                pass
        if extras.get("bar_5m_open") is not None and extras.get("bar_5m_close") is not None:
            try:
                return float(extras["bar_5m_open"]), float(extras["bar_5m_close"])
            except (TypeError, ValueError):
                pass
        if bars:
            return _bar_field(bars[-1], "open"), _bar_field(bars[-1], "close")
        # Fall back to indicator close vs extras open
        ind = obs.indicators
        if extras.get("open") is not None and ind.close is not None:
            try:
                return float(extras["open"]), float(ind.close)
            except (TypeError, ValueError):
                pass
        return None, None

    def reason(self, obs: AgentObservation) -> Decision:
        ind = obs.indicators
        symbol = obs.symbol
        close = float(ind.close or 0)
        if close <= 0:
            return Decision.hold(symbol, "sweet_spot: invalid close")

        extras = ind.extras or {}

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

        # --- Phase 1: CVD divergence BLOCK + liquidation-sweep REQUIRE (long only) ---
        if self.cvd_divergence_enabled or self.liq_sweep_required:
            cvd_snap, liq_snap = self._resolve_cvd_liq(obs)
            if self.cvd_divergence_enabled:
                bar_o, bar_c = self._setup_bar_open_close(obs, bars)
                cvd_5m = cvd_snap.get("cvd_5m") if cvd_snap else None
                cvd_ready = bool(cvd_snap.get("ready")) if cvd_snap else False
                ok_cvd, cvd_detail = check_cvd_divergence(
                    bar_o,
                    bar_c,
                    cvd_5m if cvd_5m is not None else None,
                    enabled=True,
                    ready=cvd_ready,
                    warmup_fail_closed=self.cvd_warmup_fail_closed,
                )
                if not ok_cvd:
                    logger.info("SKIP %s | %s", symbol, cvd_detail)
                    return Decision.hold(symbol, cvd_detail)
            if self.liq_sweep_required:
                short_liq = liq_snap.get("short_liq_1m_usd") if liq_snap else None
                liq_ready = bool(liq_snap.get("ready")) if liq_snap else False
                ok_liq, liq_detail = check_liq_sweep(
                    short_liq if short_liq is not None else None,
                    min_short_usd=self.liq_sweep_short_usd,
                    enabled=True,
                    ready=liq_ready,
                    warmup_fail_closed=self.cvd_warmup_fail_closed,
                )
                if not ok_liq:
                    logger.info("SKIP %s | %s", symbol, liq_detail)
                    return Decision.hold(symbol, liq_detail)

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
        conf = max(self.min_confidence, min(100.0, 60.0 + float(meta.get("breakout_rvol") or 2) * 5))
        reason = (
            f"BUY {detail}; SL=swing_low*(1-{self.swing_sl_buffer_pct})={sl:.6g}; "
            f"TP1=1R={tp1:.6g}; TP2={self.tp2_rr}R={tp2:.6g}; {tp_detail}; "
            f"delta_proxy={meta.get('delta_proxy', 'n/a')}"
        )
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
