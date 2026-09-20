"""On-chain exchange flow + stablecoin mint guards (PAPER intel v3).

Public / free endpoints (documented):
  - Prefer configurable URLs that mirror CoinGlass / CryptoQuant-style
    exchange-flow + stablecoin supply JSON (optional API key via env).
  - Without keys/URLs, mock/fixture mode returns deterministic series for tests.
  - On HTTP failure: log + skip filter (never crash the bot).

Skip reason:  onchain: exchange_inflow_spike
Boost tag:    onchain: stablecoin_mint_boost
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, Optional, Sequence, Tuple

import httpx

logger = logging.getLogger(__name__)

SKIP_EXCHANGE_INFLOW = "onchain: exchange_inflow_spike"
BOOST_STABLECOIN_MINT = "onchain: stablecoin_mint_boost"

FLOW_ASSETS = ("BTC", "ETH")
STABLES = ("USDT", "USDC")


@dataclass
class OnchainSnapshot:
    asset: str
    net_inflow: Optional[float] = None  # positive = onto exchanges
    stable_supply: Optional[float] = None
    stable_supply_delta: Optional[float] = None
    fetched_at: float = 0.0
    source: str = "none"


@dataclass
class OnchainEval:
    allow_buy: bool = True
    confidence_delta: float = 0.0
    skip_reason: str = ""
    boost_tag: str = ""


def inflow_spike(current: float, baseline: float, *, mult: float = 2.5) -> bool:
    """True when net exchange deposits spike above rolling baseline * mult."""
    if baseline <= 0 or mult <= 0:
        return False
    return float(current) > float(mult) * float(baseline)


def stablecoin_mint_boost(supply_delta: float, *, min_delta: float = 0.0) -> bool:
    """True when fresh stablecoin supply increases (mint / exchange inflow proxy)."""
    return float(supply_delta) > float(min_delta)


class OnchainGuards:
    """Poll exchange net inflows + stablecoin supply; block/boost BUY."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        poll_seconds: float = 120.0,
        cache_ttl_sec: float = 180.0,
        inflow_spike_mult: float = 2.5,
        mint_boost: float = 3.0,
        baseline_window: int = 12,
        flow_url: str = "",
        stable_url: str = "",
        api_key: str = "",
        mock_mode: bool = False,
        mock_inflows: Optional[Dict[str, Sequence[float]]] = None,
        mock_stable_deltas: Optional[Sequence[float]] = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.poll_seconds = max(60.0, float(poll_seconds or 120.0))
        self.cache_ttl_sec = max(60.0, min(300.0, float(cache_ttl_sec or 180.0)))
        self.inflow_spike_mult = float(inflow_spike_mult or 2.5)
        self.mint_boost = float(mint_boost or 3.0)
        self.baseline_window = max(3, int(baseline_window or 12))
        self.flow_url = (flow_url or "").strip()
        self.stable_url = (stable_url or "").strip()
        self.api_key = (api_key or "").strip()
        self.mock_mode = bool(mock_mode) or (not self.flow_url and not self.stable_url)
        self._mock_inflows = {
            k.upper(): deque(float(x) for x in v)
            for k, v in (mock_inflows or {}).items()
        }
        self._mock_stable_deltas: Deque[float] = deque(
            float(x) for x in (mock_stable_deltas or [])
        )
        self._history: Dict[str, Deque[float]] = defaultdict(
            lambda: deque(maxlen=self.baseline_window)
        )
        self._cache: Dict[str, OnchainSnapshot] = {}
        self._stable_snap: Optional[OnchainSnapshot] = None
        self._mock_idx = 0
        self._running = False
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if not self.enabled:
            logger.info("ONCHAIN_GUARDS disabled")
            return
        if self._running:
            return
        self._running = True
        self._stop.clear()
        try:
            await self.refresh()
        except Exception as exc:
            logger.warning("ONCHAIN_GUARDS initial refresh failed (degrade): %s", exc)
        self._task = asyncio.create_task(self._poll_loop(), name="onchain_guards")
        logger.info(
            "ONCHAIN_GUARDS armed mock=%s poll=%.0fs spike_mult=%.2f mint_boost=%.1f "
            "cache=%.0fs flow_url=%s stable_url=%s",
            self.mock_mode,
            self.poll_seconds,
            self.inflow_spike_mult,
            self.mint_boost,
            self.cache_ttl_sec,
            bool(self.flow_url),
            bool(self.stable_url),
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self._running = False

    async def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
                break
            except asyncio.TimeoutError:
                pass
            try:
                await self.refresh()
            except Exception as exc:
                logger.warning("ONCHAIN_GUARDS poll failed (degrade): %s", exc)

    async def refresh(self) -> None:
        async with self._lock:
            if self.mock_mode:
                self._refresh_mock()
                return
            try:
                await self._refresh_http()
            except Exception as exc:
                logger.warning("ONCHAIN_GUARDS HTTP failed (degrade/skip): %s", exc)

    def _refresh_mock(self) -> None:
        now = time.time()
        for asset in FLOW_ASSETS:
            series = self._mock_inflows.get(asset)
            if series and len(series) > 0:
                val = float(series[self._mock_idx % len(series)])
            else:
                val = 100.0 + (10.0 if self._mock_idx % 7 else 0.0)
            self._history[asset].append(val)
            self._cache[asset] = OnchainSnapshot(
                asset=asset,
                net_inflow=val,
                fetched_at=now,
                source="mock",
            )
        if self._mock_stable_deltas:
            delta = float(
                self._mock_stable_deltas[self._mock_idx % len(self._mock_stable_deltas)]
            )
        else:
            delta = 1.0 if (self._mock_idx % 5 == 0) else 0.0
        supply = 1_000_000.0 + delta * (self._mock_idx + 1)
        self._stable_snap = OnchainSnapshot(
            asset="STABLE",
            stable_supply=supply,
            stable_supply_delta=delta,
            fetched_at=now,
            source="mock",
        )
        self._mock_idx += 1

    async def _refresh_http(self) -> None:
        headers: Dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            headers["X-API-KEY"] = self.api_key
        now = time.time()
        async with httpx.AsyncClient(timeout=20.0) as client:
            if self.flow_url:
                resp = await client.get(self.flow_url, headers=headers)
                resp.raise_for_status()
                parsed = self._parse_flow(resp.json())
                for asset, val in parsed.items():
                    self._history[asset].append(val)
                    self._cache[asset] = OnchainSnapshot(
                        asset=asset,
                        net_inflow=val,
                        fetched_at=now,
                        source="http",
                    )
            if self.stable_url:
                resp = await client.get(self.stable_url, headers=headers)
                resp.raise_for_status()
                supply, delta = self._parse_stable(resp.json())
                self._stable_snap = OnchainSnapshot(
                    asset="STABLE",
                    stable_supply=supply,
                    stable_supply_delta=delta,
                    fetched_at=now,
                    source="http",
                )

    @staticmethod
    def _parse_flow(data: Any) -> Dict[str, float]:
        out: Dict[str, float] = {}
        if not isinstance(data, dict):
            return out
        if "data" in data and isinstance(data["data"], list):
            for row in data["data"]:
                if not isinstance(row, dict):
                    continue
                sym = str(row.get("symbol") or row.get("asset") or "").upper()
                if sym in FLOW_ASSETS:
                    try:
                        out[sym] = float(
                            row.get("net_inflow")
                            or row.get("exchange_netflow")
                            or row.get("value")
                            or 0
                        )
                    except (TypeError, ValueError):
                        continue
            return out
        for asset in FLOW_ASSETS:
            node = data.get(asset) or data.get(asset.lower())
            if isinstance(node, dict):
                try:
                    out[asset] = float(
                        node.get("net_inflow")
                        or node.get("exchange_netflow")
                        or node.get("value")
                        or 0
                    )
                except (TypeError, ValueError):
                    continue
            elif isinstance(node, (int, float)):
                out[asset] = float(node)
        return out

    @staticmethod
    def _parse_stable(data: Any) -> Tuple[Optional[float], Optional[float]]:
        if not isinstance(data, dict):
            return None, None
        if "data" in data and isinstance(data["data"], dict):
            d = data["data"]
            try:
                supply = (
                    float(d["total_supply"])
                    if d.get("total_supply") is not None
                    else None
                )
                delta = (
                    float(d["total_delta"])
                    if d.get("total_delta") is not None
                    else None
                )
                return supply, delta
            except (TypeError, ValueError):
                pass
        total_supply = 0.0
        total_delta = 0.0
        found = False
        for s in STABLES:
            node = data.get(s) or data.get(s.lower())
            if isinstance(node, dict):
                found = True
                try:
                    total_supply += float(node.get("supply") or node.get("total") or 0)
                    total_delta += float(node.get("delta") or node.get("change") or 0)
                except (TypeError, ValueError):
                    continue
        if found:
            return total_supply, total_delta
        return None, None

    def _baseline(self, asset: str) -> float:
        hist = self._history.get(asset.upper())
        if not hist:
            return 0.0
        vals = list(hist)
        if len(vals) >= 2:
            vals = vals[:-1]
        if not vals:
            return 0.0
        return sum(vals) / len(vals)

    def clear_buffers(self) -> None:
        """Clear on-chain snapshot cache (baselines retained)."""
        self._cache.clear()
        logger.debug("OnchainGuards cache cleared")

    def evaluate(self, symbol: str, *, confidence: float = 0.0) -> OnchainEval:
        del confidence  # reserved
        if not self.enabled:
            return OnchainEval(allow_buy=True)
        try:
            return self._evaluate_inner(symbol)
        except Exception as exc:
            logger.warning("ONCHAIN_GUARDS evaluate failed (degrade): %s", exc)
            return OnchainEval(allow_buy=True)

    def _evaluate_inner(self, symbol: str) -> OnchainEval:
        now = time.time()
        base = (symbol or "").upper().split("-")[0]
        eval_out = OnchainEval(allow_buy=True)

        check_assets = [base] if base in FLOW_ASSETS else ["BTC"]
        for asset in check_assets:
            snap = self._cache.get(asset)
            if snap is None or (now - snap.fetched_at) > self.cache_ttl_sec * 2:
                continue
            cur = float(snap.net_inflow or 0)
            base_avg = self._baseline(asset)
            if inflow_spike(cur, base_avg, mult=self.inflow_spike_mult):
                eval_out.allow_buy = False
                eval_out.skip_reason = SKIP_EXCHANGE_INFLOW
                logger.info(
                    "ONCHAIN skip %s | %s asset=%s inflow=%.2f baseline=%.2f mult=%.2f",
                    symbol,
                    SKIP_EXCHANGE_INFLOW,
                    asset,
                    cur,
                    base_avg,
                    self.inflow_spike_mult,
                )
                return eval_out

        ss = self._stable_snap
        if ss is not None and (now - ss.fetched_at) <= self.cache_ttl_sec * 2:
            delta = float(ss.stable_supply_delta or 0)
            if stablecoin_mint_boost(delta, min_delta=0.0) and delta > 0:
                eval_out.confidence_delta = float(self.mint_boost)
                eval_out.boost_tag = BOOST_STABLECOIN_MINT
                logger.info(
                    "ONCHAIN boost %s | %s delta=%.4g conf+=%.1f",
                    symbol,
                    BOOST_STABLECOIN_MINT,
                    delta,
                    self.mint_boost,
                )
        return eval_out

    def inject_mock_inflow(self, asset: str, value: float) -> None:
        asset = asset.upper()
        self._history[asset].append(float(value))
        self._cache[asset] = OnchainSnapshot(
            asset=asset,
            net_inflow=float(value),
            fetched_at=time.time(),
            source="inject",
        )

    def inject_stable_delta(self, delta: float, supply: float = 1_000_000.0) -> None:
        self._stable_snap = OnchainSnapshot(
            asset="STABLE",
            stable_supply=float(supply),
            stable_supply_delta=float(delta),
            fetched_at=time.time(),
            source="inject",
        )
