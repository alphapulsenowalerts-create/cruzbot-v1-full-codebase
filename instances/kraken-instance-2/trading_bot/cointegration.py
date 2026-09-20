"""Cross-asset cointegration / Z-score mean-reversion (PAPER intel v3).

Rolling ~24h correlation + OLS hedge-ratio residual Z-score for pairs among
the allowlist (e.g. SOL/AVAX, ETH/LINK, BTC/ETH). Long-only: when |Z| > entry
and Z is reverting, BUY the undervalued (lagging) leg only.

Emits optional BUY candidates that still pass fee/spread/dedupe/post-only/caps.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_PAIRS: Tuple[Tuple[str, str], ...] = (
    ("SOL-USD", "AVAX-USD"),
    ("ETH-USD", "LINK-USD"),
    ("BTC-USD", "ETH-USD"),
)

ENTRY_REASON = "coint: zscore_mean_reversion"


@dataclass
class CointSignal:
    symbol: str  # leg to BUY (undervalued)
    pair: Tuple[str, str]
    zscore: float
    hedge_ratio: float
    correlation: float
    confidence: float
    reason: str = ENTRY_REASON


@dataclass
class PairState:
    a: str
    b: str
    prices_a: List[float] = field(default_factory=list)
    prices_b: List[float] = field(default_factory=list)
    last_z: Optional[float] = None
    prev_z: Optional[float] = None


def parse_pairs(spec: str) -> List[Tuple[str, str]]:
    """Parse 'SOL-USD/AVAX-USD,ETH-USD/LINK-USD' into list of tuples."""
    out: List[Tuple[str, str]] = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "/" in part:
            a, b = part.split("/", 1)
        elif ":" in part:
            a, b = part.split(":", 1)
        else:
            continue
        a, b = a.strip().upper(), b.strip().upper()
        if a and b and a != b:
            out.append((a, b))
    return out


def ols_hedge_ratio(y: np.ndarray, x: np.ndarray) -> float:
    """OLS beta for y ~ alpha + beta * x (no intercept in beta via demean)."""
    if len(y) < 3 or len(x) < 3:
        return 1.0
    x_d = x - x.mean()
    y_d = y - y.mean()
    denom = float(np.dot(x_d, x_d))
    if denom <= 1e-18:
        return 1.0
    return float(np.dot(x_d, y_d) / denom)


def residual_zscore(y: np.ndarray, x: np.ndarray, beta: float) -> float:
    """Z-score of latest residual y - beta*x."""
    resid = y - beta * x
    if len(resid) < 3:
        return 0.0
    mu = float(resid.mean())
    sigma = float(resid.std(ddof=1))
    if sigma <= 1e-18:
        return 0.0
    return float((resid[-1] - mu) / sigma)


def rolling_corr(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < 3:
        return 0.0
    if a.std() <= 1e-18 or b.std() <= 1e-18:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def z_reverting(prev_z: Optional[float], z: float) -> bool:
    """True when |z| is shrinking (mean-reversion underway)."""
    if prev_z is None:
        return False
    return abs(float(z)) < abs(float(prev_z)) - 1e-9


def undervalued_leg(
    pair: Tuple[str, str],
    z: float,
    *,
    prices_a: Sequence[float],
    prices_b: Sequence[float],
    beta: float,
) -> Optional[str]:
    """When z = residual(a - beta*b) is high, A is rich → buy B; if low, buy A."""
    del prices_a, prices_b, beta  # residual sign convention is enough
    a, b = pair
    if z <= -1e-12:
        return a  # A undervalued
    if z >= 1e-12:
        return b  # B undervalued (A rich)
    return None


class CointegrationEngine:
    """Maintain rolling closes and emit long-only mean-reversion BUY signals."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        pairs: Optional[Sequence[Tuple[str, str]]] = None,
        z_entry: float = 2.0,
        window: int = 96,  # ~24h of 15m or ~1.5h of 1m — configurable
        min_corr: float = 0.5,
        confidence_base: float = 68.0,
    ) -> None:
        self.enabled = bool(enabled)
        self.pairs = list(pairs) if pairs else list(DEFAULT_PAIRS)
        self.z_entry = float(z_entry or 2.0)
        self.window = max(20, int(window or 96))
        self.min_corr = float(min_corr or 0.5)
        self.confidence_base = float(confidence_base or 68.0)
        self._states: Dict[Tuple[str, str], PairState] = {
            (a, b): PairState(a=a, b=b) for a, b in self.pairs
        }
        self._last_signals: Dict[str, CointSignal] = {}
        self._updated_at = 0.0

    def update_price(self, symbol: str, price: float) -> None:
        if not self.enabled or price <= 0:
            return
        sym = symbol.upper()
        for key, st in self._states.items():
            if sym == st.a:
                st.prices_a.append(float(price))
                if len(st.prices_a) > self.window:
                    st.prices_a = st.prices_a[-self.window :]
            elif sym == st.b:
                st.prices_b.append(float(price))
                if len(st.prices_b) > self.window:
                    st.prices_b = st.prices_b[-self.window :]
        self._updated_at = time.time()

    def update_prices(self, prices: Dict[str, float]) -> None:
        for sym, px in (prices or {}).items():
            self.update_price(sym, float(px))

    def evaluate_pair(self, pair: Tuple[str, str]) -> Optional[CointSignal]:
        st = self._states.get(pair)
        if st is None:
            return None
        n = min(len(st.prices_a), len(st.prices_b))
        if n < max(20, self.window // 3):
            return None
        ya = np.asarray(st.prices_a[-n:], dtype=float)
        xb = np.asarray(st.prices_b[-n:], dtype=float)
        beta = ols_hedge_ratio(ya, xb)
        z = residual_zscore(ya, xb, beta)
        corr = rolling_corr(ya, xb)
        prev = st.last_z
        st.prev_z = prev
        st.last_z = z

        if abs(corr) < self.min_corr:
            return None
        if abs(z) < self.z_entry:
            return None
        if not z_reverting(prev, z):
            return None

        leg = undervalued_leg(pair, z, prices_a=ya, prices_b=xb, beta=beta)
        if not leg:
            return None
        # Confidence scales with |Z| excess over entry
        excess = abs(z) - self.z_entry
        conf = min(95.0, self.confidence_base + excess * 5.0 + abs(corr) * 5.0)
        sig = CointSignal(
            symbol=leg,
            pair=pair,
            zscore=z,
            hedge_ratio=beta,
            correlation=corr,
            confidence=conf,
            reason=f"{ENTRY_REASON} z={z:.2f} corr={corr:.2f} buy={leg}",
        )
        self._last_signals[leg] = sig
        logger.info(
            "COINT SIGNAL buy=%s pair=%s/%s z=%.2f corr=%.2f beta=%.4f conf=%.1f",
            leg,
            pair[0],
            pair[1],
            z,
            corr,
            beta,
            conf,
        )
        return sig

    def scan(self) -> List[CointSignal]:
        if not self.enabled:
            return []
        out: List[CointSignal] = []
        try:
            for pair in self.pairs:
                sig = self.evaluate_pair(pair)
                if sig is not None:
                    out.append(sig)
        except Exception as exc:
            logger.warning("COINT scan failed (degrade): %s", exc)
        return out

    def signal_for(self, symbol: str) -> Optional[CointSignal]:
        return self._last_signals.get(symbol.upper())
