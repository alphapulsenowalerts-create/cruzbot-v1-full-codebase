"""ReAct-style agent: observe → reason → act with rigid Decision schema.

Hybrid execution: deterministic Python pre-filter gates expensive reasoning.
LLM is only invoked when a setup fires (configurable VWAP boundary + volume spike).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Protocol, Union

from pydantic import ValidationError

from trading_bot.models import (
    Action,
    AgentObservation,
    Decision,
)


logger = logging.getLogger(__name__)


def fee_clearance_ok(
    price: float,
    atr: Optional[float],
    *,
    take_profit_atr_mult: float,
    taker_fee_rate: float,
    fee_clear_mult: float = 2.0,
) -> tuple[bool, str]:
    """
    Hard BUY gate: expected TP distance (as fraction of price) must clear
    round-trip taker fees with cushion. Skip when ATR is tiny — never widen multiples.
    """
    if price is None or price <= 0:
        return False, "fee_clearance: invalid price"
    if atr is None or atr <= 0:
        return False, "fee_clearance: missing/tiny ATR"
    tp_dist_pct = (float(take_profit_atr_mult) * float(atr)) / float(price)
    required = 2.0 * float(taker_fee_rate) * float(fee_clear_mult)
    if tp_dist_pct + 1e-12 < required:
        return (
            False,
            (
                f"fee_clearance: TP dist {tp_dist_pct:.4%} < required "
                f"{required:.4%} (ATR={atr:.6g} price={price:.6g})"
            ),
        )
    return True, f"fee_clearance ok TP={tp_dist_pct:.4%} >= {required:.4%}"


class LLMHook(Protocol):
    """Optional LLM plug-in: given observation JSON, return decision JSON string."""

    async def complete(self, prompt: str, observation: Dict[str, Any]) -> str: ...


class SignalEngine(Protocol):
    """Anything that turns an AgentObservation into a Decision."""

    def reason(self, obs: AgentObservation) -> Decision: ...


def parse_decision(raw: Any, symbol: str) -> Decision:
    """
    Rigid JSON validation. On any parse/validation failure → HOLD.
    Accepts dict or JSON string.
    """
    try:
        if isinstance(raw, Decision):
            return raw
        if isinstance(raw, str):
            text = raw.strip()
            # extract JSON object if wrapped in prose
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                text = text[start : end + 1]
            data = json.loads(text)
        elif isinstance(raw, dict):
            data = raw
        else:
            return Decision.hold(symbol, f"unsupported decision type: {type(raw).__name__}")

        if "symbol" not in data:
            data["symbol"] = symbol
        return Decision.model_validate(data)
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
        logger.warning("Decision parse failure → HOLD (%s): %s", symbol, exc)
        return Decision.hold(symbol, f"parse failure: {exc}")


class SetupPreFilter:
    """
    Deterministic gate: VWAP reclaim (close >= VWAP near boundary, or recent
    cross above) AND volume spike exceeds average by a configurable multiple.

    When this fails the agent HOLDs quickly — no LLM, no full scalp scoring.
    """

    def __init__(
        self,
        *,
        vwap_boundary_pct: float = 0.002,
        volume_spike_mult: float = 2.75,
    ) -> None:
        self.vwap_boundary_pct = vwap_boundary_pct
        self.volume_spike_mult = volume_spike_mult

    def evaluate(self, obs: AgentObservation) -> tuple[bool, str]:
        ind = obs.indicators
        close = ind.close
        if not close or ind.vwap is None or ind.vwap <= 0:
            return False, "missing close/VWAP"

        dist = abs(close - ind.vwap) / ind.vwap
        above = close >= ind.vwap
        near_above = above and dist <= self.vwap_boundary_pct

        # Preferred: close crossed above VWAP in last 1–3 bars (if history present)
        reclaim_bars = ind.extras.get("vwap_reclaim_bars")
        crossed_up = False
        if isinstance(reclaim_bars, (int, float)) and 1 <= int(reclaim_bars) <= 3:
            crossed_up = True
        elif ind.extras.get("vwap_crossed_up") is True:
            crossed_up = True
        else:
            # Fallback: scan close_vs_vwap history if provided ([bool above, oldest→newest])
            hist = ind.extras.get("close_above_vwap_hist")
            if isinstance(hist, (list, tuple)) and len(hist) >= 2:
                recent = [bool(x) for x in hist[-4:]]
                # any bar below followed by current above within window
                if recent[-1] and any(not x for x in recent[:-1]):
                    crossed_up = True

        if not (near_above or (above and crossed_up)):
            if not above:
                return False, f"VWAP reclaim required (close < VWAP, dist={dist:.4%})"
            return False, (
                f"price above VWAP but not near boundary "
                f"(dist={dist:.4%} > {self.vwap_boundary_pct:.4%}) and no recent reclaim"
            )

        vol_ratio = ind.extras.get("volume_ratio")
        if not isinstance(vol_ratio, (int, float)):
            return False, "volume_ratio unavailable"
        if float(vol_ratio) < self.volume_spike_mult:
            return (
                False,
                f"volume spike {float(vol_ratio):.2f}x < {self.volume_spike_mult:.2f}x",
            )

        tag = "VWAP reclaim cross" if crossed_up else f"near VWAP reclaim (dist={dist:.4%})"
        return True, f"{tag} + vol_ratio={float(vol_ratio):.2f}"


class VwapMomentumScalpEngine:
    """
    VWAP momentum scalp engine (default) — long-biased crypto day scalps.

    BUY scalp (momentum continuation, not deep RSI mean-reversion):
      - Hard filter: close must be >= VWAP (no BUY below VWAP)
      - Price reclaiming/holding above VWAP
      - Rising volume (volume_ratio > 1.2 when present)
      - MACD hist positive / flipping positive
      - EMA fast > slow (or fresh bullish cross)

    SELL / exit:
      - Price loses VWAP + weak volume or bearish EMA/MACD
      - Flat-suppression (no short) is handled by AgentCore

    Reasoning strings always mention "VWAP scalp".
    """

    def __init__(
        self,
        *,
        min_confidence: float = 62.0,
        volume_ratio_threshold: float = 1.2,
        stop_loss_atr_mult: float = 1.0,
        take_profit_atr_mult: float = 1.5,
        taker_fee_rate: float = 0.009,
        fee_clear_mult: float = 2.0,
        rsi_buy_cap: float = 72.0,
    ) -> None:
        self.min_confidence = min_confidence
        self.volume_ratio_threshold = volume_ratio_threshold
        self.stop_loss_atr_mult = stop_loss_atr_mult
        self.take_profit_atr_mult = take_profit_atr_mult
        self.taker_fee_rate = taker_fee_rate
        self.fee_clear_mult = fee_clear_mult
        self.rsi_buy_cap = rsi_buy_cap

    def reason(self, obs: AgentObservation) -> Decision:
        ind = obs.indicators
        symbol = obs.symbol
        close = ind.close
        parts: list[str] = ["VWAP scalp"]
        score = 0.0  # positive → buy, negative → sell

        # Optional HTF tilt
        if obs.htf is not None:
            if obs.htf.ema_200_slope is not None:
                if obs.htf.ema_200_slope > 0:
                    score += 6
                    parts.append("1h EMA200 slope+")
                elif obs.htf.ema_200_slope < 0:
                    score -= 6
                    parts.append("1h EMA200 slope-")
            if obs.htf.resistance is not None and close and close >= obs.htf.resistance * 0.998:
                score -= 4
                parts.append("near 1h resistance")
            if obs.htf.support is not None and close and close <= obs.htf.support * 1.002:
                score += 3
                parts.append("near 1h support")

        above_vwap = False
        below_vwap = False
        if ind.vwap is not None and close:
            if close >= ind.vwap:
                above_vwap = True
                reclaim_edge = (close - ind.vwap) / ind.vwap if ind.vwap else 0.0
                score += 22
                if reclaim_edge < 0.002:
                    parts.append("price reclaiming/holding VWAP")
                else:
                    parts.append("price>VWAP")
            else:
                below_vwap = True
                score -= 22
                parts.append("price<VWAP")
        else:
            parts.append("VWAP unavailable")

        # Volume confirmation (rising volume favors continuation)
        vol_ratio = ind.extras.get("volume_ratio")
        weak_vol = False
        if isinstance(vol_ratio, (int, float)):
            parts.append(f"vol_ratio={float(vol_ratio):.2f}")
            if vol_ratio > self.volume_ratio_threshold:
                score += 18
            elif vol_ratio < 0.8:
                weak_vol = True
                score -= 12

        # MACD hist positive / flipping positive (momentum)
        macd_hist = ind.macd_hist
        macd_prev = ind.extras.get("macd_hist_prev")
        if macd_hist is not None:
            if macd_hist > 0:
                score += 16
                parts.append("MACD hist>0")
                if isinstance(macd_prev, (int, float)) and macd_prev <= 0:
                    score += 8
                    parts.append("MACD hist flip+")
            else:
                score -= 16
                parts.append("MACD hist<0")
                if isinstance(macd_prev, (int, float)) and macd_prev >= 0:
                    score -= 8
                    parts.append("MACD hist flip-")

        # EMA structure / fresh bullish cross
        if ind.ema_cross == "bullish":
            score += 28
            parts.append("EMA bullish cross")
        elif ind.ema_cross == "bearish":
            score -= 28
            parts.append("EMA bearish cross")

        if ind.ema_fast is not None and ind.ema_slow is not None:
            if ind.ema_fast > ind.ema_slow:
                score += 14
                parts.append("EMA fast>slow")
            else:
                score -= 14
                parts.append("EMA fast<slow")

        # Light RSI tilt toward momentum (not deep oversold mean-reversion)
        rsi = ind.rsi
        if rsi is not None:
            parts.append(f"RSI={rsi:.1f}")
            if 45 <= rsi <= 70:
                score += 6
            elif rsi > 75:
                score -= 6
            elif rsi < 30:
                score += 2

        atr = ind.atr
        conf = min(100.0, abs(score))
        reasoning = "; ".join(parts)

        # BUY: hard VWAP + momentum + RSI + fee-clearance gates
        if score >= self.min_confidence:
            if below_vwap or (ind.vwap is not None and close and close < ind.vwap):
                return Decision.hold(
                    symbol,
                    f"VWAP scalp: BUY blocked (close < VWAP); score={score:.1f}; {reasoning}",
                )
            if ind.vwap is None:
                return Decision.hold(
                    symbol,
                    f"VWAP scalp: BUY blocked (VWAP unavailable); score={score:.1f}; {reasoning}",
                )
            # Momentum confirm: EMA fast > slow AND MACD hist > 0 (when available)
            if ind.ema_fast is None or ind.ema_slow is None or not (ind.ema_fast > ind.ema_slow):
                return Decision.hold(
                    symbol,
                    f"VWAP scalp: BUY blocked (EMA fast<=slow); score={score:.1f}; {reasoning}",
                )
            if macd_hist is not None and not (macd_hist > 0):
                return Decision.hold(
                    symbol,
                    f"VWAP scalp: BUY blocked (MACD hist<=0); score={score:.1f}; {reasoning}",
                )
            if rsi is not None and rsi > self.rsi_buy_cap:
                return Decision.hold(
                    symbol,
                    f"VWAP scalp: BUY blocked RSI overbought ({rsi:.1f}>{self.rsi_buy_cap}); "
                    f"score={score:.1f}; {reasoning}",
                )
            ok_fee, fee_detail = fee_clearance_ok(
                float(close),
                atr,
                take_profit_atr_mult=self.take_profit_atr_mult,
                taker_fee_rate=self.taker_fee_rate,
                fee_clear_mult=self.fee_clear_mult,
            )
            if not ok_fee:
                logger.info("SKIP %s | %s", symbol, fee_detail)
                return Decision.hold(symbol, fee_detail)
            sl = tp = None
            if atr and close:
                sl = close - self.stop_loss_atr_mult * atr
                tp = close + self.take_profit_atr_mult * atr
            return Decision(
                action=Action.BUY,
                symbol=symbol,
                confidence=round(max(conf, self.min_confidence), 1),
                stop_loss=round(sl, 4) if sl else None,
                take_profit=round(tp, 4) if tp else None,
                reasoning=f"BUY VWAP scalp: {reasoning}; {fee_detail}",
            )

        # SELL / exit: lose VWAP + weak vol or bearish EMA/MACD
        if score <= -self.min_confidence:
            exit_ok = below_vwap or weak_vol or ind.ema_cross == "bearish" or (
                macd_hist is not None and macd_hist < 0
            )
            if not exit_ok and not below_vwap:
                return Decision.hold(
                    symbol,
                    f"VWAP scalp: SELL score={score:.1f} but exit filters weak; {reasoning}",
                )
            sl = tp = None
            if atr and close:
                sl = close + self.stop_loss_atr_mult * atr
                tp = close - self.take_profit_atr_mult * atr
            return Decision(
                action=Action.SELL,
                symbol=symbol,
                confidence=round(max(conf, self.min_confidence), 1),
                stop_loss=round(sl, 4) if sl else None,
                take_profit=round(tp, 4) if tp else None,
                reasoning=f"SELL VWAP scalp: {reasoning}",
            )

        return Decision.hold(
            symbol,
            f"VWAP scalp: score={score:.1f} below threshold; {reasoning}",
        )


# Backward-compatible name used by older tests / imports
RuleBasedSignalEngine = VwapMomentumScalpEngine


class AgentCore:
    """
    Hybrid ReAct loop:
      Observe → Pre-filter → Reason (rules or LLM only on setup) → Act (Decision)

    Does NOT call the LLM every tick. Pre-filter skip → fast HOLD.
    """

    def __init__(
        self,
        signal_engine: Optional[Union[VwapMomentumScalpEngine, SignalEngine]] = None,
        llm_hook: Optional[LLMHook] = None,
        use_llm: bool = False,
        prefilter: Optional[SetupPreFilter] = None,
        *,
        vwap_boundary_pct: float = 0.002,
        volume_spike_mult: float = 2.75,
        require_prefilter: bool = True,
    ) -> None:
        self.signal_engine: SignalEngine = signal_engine or VwapMomentumScalpEngine()
        self.llm_hook = llm_hook
        self.use_llm = use_llm and llm_hook is not None
        self.prefilter = prefilter or SetupPreFilter(
            vwap_boundary_pct=vwap_boundary_pct,
            volume_spike_mult=volume_spike_mult,
        )
        self.require_prefilter = require_prefilter

    def _obs_dict(self, obs: AgentObservation) -> Dict[str, Any]:
        return {
            "symbol": obs.symbol,
            "indicators": obs.indicators.model_dump(mode="json"),
            "has_position": obs.position is not None and abs(obs.position.qty) > 0,
            "position_qty": obs.position.qty if obs.position else 0,
            "entry_timeframe": obs.entry_timeframe,
            "htf": obs.htf.model_dump(mode="json") if obs.htf else None,
            "trade_memory": [t.model_dump(mode="json") for t in obs.trade_memory[-5:]],
            "macro_paused": obs.macro_paused,
            "revenge_locked": obs.revenge_locked,
        }

    def _suppress_flat_sell(self, decision: Decision, obs: AgentObservation) -> Decision:
        if decision.action == Action.SELL:
            qty = obs.position.qty if obs.position else 0.0
            if qty <= 0:
                return Decision.hold(
                    obs.symbol,
                    f"SELL signal suppressed (flat). Original: {decision.reasoning}",
                )
        return decision

    def _llm_prompt(self) -> str:
        return (
            "You are a VWAP momentum scalp day-trading agent. "
            "Use 1m/5m entry data plus 1h trend (EMA200 slope, S/R) and last trades memory. "
            "Reply ONLY with JSON: "
            '{"action":"BUY"|"SELL"|"HOLD","symbol":"...","confidence":0-100,'
            '"stop_loss":float|null,"take_profit":float|null,"reasoning":"..."}'
        )

    async def decide(self, obs: AgentObservation) -> Decision:
        # Guardrails already applied by caller (macro / revenge) — still honor flags
        if obs.macro_paused:
            return Decision.hold(obs.symbol, "HOLD: macro event pause")
        if obs.revenge_locked:
            return Decision.hold(obs.symbol, "HOLD: revenge lockout active")

        logger.debug("OBSERVE %s", obs.symbol)

        # Deterministic pre-filter — never call LLM every tick
        passed, detail = self.prefilter.evaluate(obs)
        if self.require_prefilter and not passed:
            logger.info("PREFILTER_SKIP %s | %s", obs.symbol, detail)
            return Decision.hold(obs.symbol, f"PREFILTER_SKIP: {detail}")

        logger.info("PREFILTER_PASS %s | %s", obs.symbol, detail)

        # REASON — LLM only when hook enabled AND setup fired
        if self.use_llm and self.llm_hook is not None:
            logger.info("LLM_INVOKED %s", obs.symbol)
            obs_dict = self._obs_dict(obs)
            try:
                raw = await self.llm_hook.complete(self._llm_prompt(), obs_dict)
                decision = parse_decision(raw, obs.symbol)
            except Exception as exc:
                logger.exception("LLM hook failed → rule engine fallback: %s", exc)
                decision = self.signal_engine.reason(obs)
        else:
            # No LLM hook / use_llm false: deterministic engine when pre-filter passes
            decision = self.signal_engine.reason(obs)

        decision = self._suppress_flat_sell(decision, obs)
        decision = parse_decision(decision.model_dump(), obs.symbol)
        logger.info(
            "ACT %s %s conf=%.1f | %s",
            decision.action.value,
            decision.symbol,
            decision.confidence,
            decision.reasoning,
        )
        return decision

    def decide_sync(self, obs: AgentObservation) -> Decision:
        """Synchronous path for tests / rule engine (still respects pre-filter when enabled)."""
        if obs.macro_paused:
            return Decision.hold(obs.symbol, "HOLD: macro event pause")
        if obs.revenge_locked:
            return Decision.hold(obs.symbol, "HOLD: revenge lockout active")

        if self.require_prefilter:
            passed, detail = self.prefilter.evaluate(obs)
            if not passed:
                logger.info("PREFILTER_SKIP %s | %s", obs.symbol, detail)
                return Decision.hold(obs.symbol, f"PREFILTER_SKIP: {detail}")
            logger.info("PREFILTER_PASS %s | %s", obs.symbol, detail)

        decision = self.signal_engine.reason(obs)
        decision = self._suppress_flat_sell(decision, obs)
        return parse_decision(decision.model_dump(), obs.symbol)
