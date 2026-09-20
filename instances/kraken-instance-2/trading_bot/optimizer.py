"""Nightly walk-forward parameter optimizer (America/Chicago midnight).

Hard safety floors (never loosen past these):
  - min_tp_pct          >= 0.025
  - rvol_breakout_mult  >= 1.5
  - rsi_buy_cap         <= 72.0   (never raise chase ceiling)
  - pullback_vol_frac   <= 0.5
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import httpx
import numpy as np
import pandas as pd

from trading_bot.config import PROJECT_ROOT, HARD_SYMBOL_ALLOWLIST

logger = logging.getLogger(__name__)

CHICAGO = ZoneInfo("America/Chicago")

# --- Hard safety floors (documented) ---
FLOOR_MIN_TP_PCT = 0.025
FLOOR_RVOL_BREAKOUT_MULT = 1.5
CEILING_RSI_BUY_CAP = 72.0
CEILING_PULLBACK_VOL_FRAC = 0.5

DEFAULT_ACTIVE_PARAMS_PATH = PROJECT_ROOT / "data" / "active_params.json"

SPOT_TO_BINANCE = {
    "BTC-USD": "BTCUSDT",
    "ETH-USD": "ETHUSDT",
    "SOL-USD": "SOLUSDT",
    "XRP-USD": "XRPUSDT",
    "LINK-USD": "LINKUSDT",
    "AVAX-USD": "AVAXUSDT",
    "SUI-USD": "SUIUSDT",
    "ADA-USD": "ADAUSDT",
}


@dataclass
class OptimizedParams:
    rvol_breakout_mult: float
    rsi_buy_cap: float
    min_tp_pct: float
    regime: str = "unknown"  # optimizer vol split: high_vol|mid_vol|low_vol|no_data
    score: float = 0.0
    timestamp: str = ""
    lookback_days: int = 14
    # Phase 2 macro regime (ATR+ADX clustering) — distinct from `regime` vol split
    market_regime: str = "RANGING"  # TRENDING | RANGING | HIGH_VOLATILITY
    regime_updated_at: str = ""
    regime_features: Optional[Dict[str, Any]] = None

    def clamped(self) -> "OptimizedParams":
        return OptimizedParams(
            rvol_breakout_mult=max(FLOOR_RVOL_BREAKOUT_MULT, float(self.rvol_breakout_mult)),
            rsi_buy_cap=min(CEILING_RSI_BUY_CAP, float(self.rsi_buy_cap)),
            min_tp_pct=max(FLOOR_MIN_TP_PCT, float(self.min_tp_pct)),
            regime=self.regime,
            score=self.score,
            timestamp=self.timestamp,
            lookback_days=self.lookback_days,
            market_regime=self.market_regime or "RANGING",
            regime_updated_at=self.regime_updated_at,
            regime_features=self.regime_features,
        )


def clamp_params(
    *,
    rvol_breakout_mult: float,
    rsi_buy_cap: float,
    min_tp_pct: float,
) -> Dict[str, float]:
    """Apply hard safety floors/ceilings."""
    return {
        "rvol_breakout_mult": max(FLOOR_RVOL_BREAKOUT_MULT, float(rvol_breakout_mult)),
        "rsi_buy_cap": min(CEILING_RSI_BUY_CAP, float(rsi_buy_cap)),
        "min_tp_pct": max(FLOOR_MIN_TP_PCT, float(min_tp_pct)),
    }


def atr_pct_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(period, min_periods=max(2, period // 2)).mean()
    return (atr / close.replace(0, np.nan)).fillna(0.0)


def classify_vol_regime(atr_pct: float, hi_thresh: float, lo_thresh: float) -> str:
    if atr_pct >= hi_thresh:
        return "high_vol"
    if atr_pct <= lo_thresh:
        return "low_vol"
    return "mid_vol"


def _pure_kmeans(
    points: np.ndarray,
    k: int = 3,
    *,
    max_iter: int = 40,
    seed: int = 42,
) -> Tuple[np.ndarray, np.ndarray]:
    """Tiny deterministic k-means (no sklearn). Returns (centroids[k,d], labels[n])."""
    pts = np.asarray(points, dtype=float)
    if pts.ndim != 2 or pts.shape[0] == 0:
        raise ValueError("points must be non-empty 2d")
    n, d = pts.shape
    k = int(min(max(1, k), n))
    rng = np.random.default_rng(seed)
    # Deterministic init: pick seed index, then farthest-point sampling
    idxs = [int(rng.integers(0, n))]
    while len(idxs) < k:
        dists = np.min(
            np.linalg.norm(pts[:, None, :] - pts[np.array(idxs)][None, :, :], axis=2),
            axis=1,
        )
        idxs.append(int(np.argmax(dists)))
    centroids = pts[np.array(idxs)].copy()
    labels = np.zeros(n, dtype=int)
    for _ in range(max_iter):
        # Assign
        dist = np.linalg.norm(pts[:, None, :] - centroids[None, :, :], axis=2)
        new_labels = np.argmin(dist, axis=1)
        new_centroids = centroids.copy()
        for j in range(k):
            mask = new_labels == j
            if np.any(mask):
                new_centroids[j] = pts[mask].mean(axis=0)
            else:
                # Re-seed empty cluster to farthest point
                far = int(np.argmax(np.min(dist, axis=1)))
                new_centroids[j] = pts[far]
        if np.allclose(new_centroids, centroids) and np.array_equal(new_labels, labels):
            centroids = new_centroids
            labels = new_labels
            break
        centroids = new_centroids
        labels = new_labels
    return centroids, labels


def _label_atr_adx_centroids(centroids: np.ndarray) -> Dict[int, str]:
    """
    Deterministic cluster → regime mapping on feature dims [atr_ratio, adx]:
      - highest atr_ratio → HIGH_VOLATILITY
      - among remaining, highest adx → TRENDING
      - last → RANGING
    """
    cents = np.asarray(centroids, dtype=float)
    k = cents.shape[0]
    order_atr = list(np.argsort(-cents[:, 0]))  # high ATR first
    high_vol_i = int(order_atr[0])
    remaining = [i for i in range(k) if i != high_vol_i]
    if not remaining:
        return {high_vol_i: "HIGH_VOLATILITY"}
    # high ADX among remaining → TRENDING
    rem_adx = [(i, float(cents[i, 1])) for i in remaining]
    rem_adx.sort(key=lambda t: -t[1])
    trend_i = int(rem_adx[0][0])
    mapping = {high_vol_i: "HIGH_VOLATILITY", trend_i: "TRENDING"}
    for i in remaining:
        if i not in mapping:
            mapping[i] = "RANGING"
    return mapping


def _resample_ohlcv(df: pd.DataFrame, rule: str = "4h") -> pd.DataFrame:
    """Resample to macro bars when a DatetimeIndex is present; else return as-is."""
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    cols = {c.lower(): c for c in out.columns}
    need = ["open", "high", "low", "close", "volume"]
    if not all(k in cols for k in need):
        return pd.DataFrame()
    piece = out[[cols[k] for k in need]].copy()
    piece.columns = need
    if not isinstance(piece.index, pd.DatetimeIndex):
        return piece
    ohlc = piece.resample(rule).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["close"])
    return ohlc


def extract_regime_feature_matrix(
    frames: Dict[str, pd.DataFrame],
    *,
    atr_period: int = 14,
    adx_period: int = 14,
    baseline_windows: int = 40,
    max_points_per_symbol: int = 24,
) -> Tuple[np.ndarray, Dict[str, Any]]:
    """
    Build feature rows [atr_ratio, adx] from recent daily/4h-ish bars.

    Feature ideas (documented):
      - atr_ratio = rolling ATR% / long baseline ATR%  (elevated → HIGH_VOLATILITY)
      - adx_level = Wilder ADX                         (elevated + calm ATR → TRENDING;
                                                       low ADX → RANGING)
    """
    from trading_bot.utils.indicators import adx as adx_fn

    rows: List[List[float]] = []
    meta_syms: Dict[str, Any] = {}
    for sym, raw in (frames or {}).items():
        if raw is None or raw.empty:
            continue
        # Prefer 4h macro bars; fall back to raw (e.g. already coarse / synthetic)
        df4 = _resample_ohlcv(raw, "4h")
        if df4.empty or len(df4) < atr_period + 5:
            df4 = raw.copy()
            cols = {c.lower(): c for c in df4.columns}
            if not all(k in cols for k in ("open", "high", "low", "close")):
                continue
            keep = ["open", "high", "low", "close"]
            if "volume" in cols:
                keep.append("volume")
            df4 = df4[[cols[k] for k in keep]].copy()
            df4.columns = keep
            if "volume" not in df4.columns:
                df4["volume"] = 1.0
        if len(df4) < atr_period + 5:
            continue
        atr_p = atr_pct_series(df4, atr_period)
        baseline = atr_p.rolling(baseline_windows, min_periods=max(5, baseline_windows // 4)).mean()
        atr_ratio = (atr_p / baseline.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan)
        try:
            adx_s = adx_fn(df4, adx_period)
        except Exception:
            adx_s = pd.Series(dtype=float)
        # Align and collect recent valid points
        n = len(df4)
        start = max(0, n - max_points_per_symbol)
        last_ar = last_adx = None
        for i in range(start, n):
            try:
                ar = float(atr_ratio.iloc[i])
                ax = float(adx_s.iloc[i]) if i < len(adx_s) else float("nan")
            except Exception:
                continue
            if math.isnan(ar) or math.isnan(ax):
                continue
            rows.append([ar, ax])
            last_ar, last_adx = ar, ax
        if last_ar is not None:
            meta_syms[sym] = {"atr_ratio": last_ar, "adx": last_adx}
    if not rows:
        return np.zeros((0, 2)), {"symbols": meta_syms, "n_points": 0}
    return np.asarray(rows, dtype=float), {"symbols": meta_syms, "n_points": len(rows)}


def classify_market_regime_atr_adx(
    frames: Dict[str, pd.DataFrame],
    *,
    fallback: str = "RANGING",
) -> Tuple[str, Dict[str, Any]]:
    """
    Cluster ATR-ratio + ADX features into 3 regimes and label the current state.

    Soft-fail: empty/insufficient data → fallback (caller may pass previous regime).
    """
    features = {
        "method": "pure_kmeans_atr_adx",
        "fallback": fallback,
    }
    try:
        mat, meta = extract_regime_feature_matrix(frames)
        features.update(meta)
        if mat.shape[0] < 3:
            # Rule-based fallback on latest mean when too few points for k-means
            if mat.shape[0] == 0:
                features["reason"] = "no_feature_rows"
                return fallback, features
            mean_ar = float(mat[:, 0].mean())
            mean_adx = float(mat[:, 1].mean())
            features["current"] = {"atr_ratio": mean_ar, "adx": mean_adx}
            if mean_ar >= 1.35:
                label = "HIGH_VOLATILITY"
            elif mean_adx >= 25.0:
                label = "TRENDING"
            else:
                label = "RANGING"
            features["reason"] = "rule_fallback_lt3"
            return label, features
        centroids, labels = _pure_kmeans(mat, k=3, seed=42)
        mapping = _label_atr_adx_centroids(centroids)
        # Current state = mean of last few points → nearest centroid
        cur = mat[-min(8, len(mat)) :].mean(axis=0)
        dist = np.linalg.norm(centroids - cur[None, :], axis=1)
        nearest = int(np.argmin(dist))
        label = mapping.get(nearest, fallback)
        features["current"] = {"atr_ratio": float(cur[0]), "adx": float(cur[1])}
        features["centroids"] = {
            mapping[i]: {"atr_ratio": float(centroids[i, 0]), "adx": float(centroids[i, 1])}
            for i in mapping
        }
        features["cluster_counts"] = {
            mapping[i]: int(np.sum(labels == i)) for i in mapping
        }
        return label, features
    except Exception as exc:
        features["error"] = str(exc)
        logger.warning("classify_market_regime_atr_adx failed: %s", exc)
        return fallback, features


def _read_previous_market_regime(path: Path) -> str:
    try:
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            mr = raw.get("market_regime")
            if mr:
                return str(mr).strip().upper()
    except Exception:
        pass
    return "RANGING"


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def score_params_on_df(
    df: pd.DataFrame,
    *,
    rvol_breakout_mult: float,
    rsi_buy_cap: float,
    min_tp_pct: float,
    volume_sma: int = 20,
) -> float:
    """
    Lightweight walk-forward proxy score:
    Count RVOL breakout + subsequent M-bar move >= min_tp without immediate dump.
    Higher score is better; penalties for false breakouts.
    """
    if df is None or len(df) < volume_sma + 10:
        return -1e9
    close = df["close"].astype(float)
    open_ = df["open"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    vol = df["volume"].astype(float)
    vol_sma = vol.rolling(volume_sma).mean()
    rvol = vol / vol_sma.replace(0, np.nan)
    rsi = _rsi(close)
    score = 0.0
    hold = 8
    for i in range(volume_sma + 2, len(df) - hold):
        if not (rvol.iloc[i] >= rvol_breakout_mult and close.iloc[i] > open_.iloc[i]):
            continue
        # retest within next 3 bars: pullback vol < 0.5 * breakout
        retest = None
        for j in range(i + 1, min(i + 4, len(df) - hold)):
            if vol.iloc[j] < 0.5 * vol.iloc[i]:
                retest = j
                break
        if retest is None:
            score -= 0.15
            continue
        if rsi.iloc[retest] is not None and not math.isnan(rsi.iloc[retest]):
            if float(rsi.iloc[retest]) > rsi_buy_cap:
                score -= 0.05
                continue
        entry = float(close.iloc[retest])
        if entry <= 0:
            continue
        future_high = float(high.iloc[retest + 1 : retest + 1 + hold].max())
        future_low = float(low.iloc[retest + 1 : retest + 1 + hold].min())
        up = (future_high - entry) / entry
        dn = (entry - future_low) / entry
        if up >= min_tp_pct and up > dn:
            score += 1.0 + min(1.0, up / max(min_tp_pct, 1e-9))
        elif dn > up:
            score -= 0.6
        else:
            score -= 0.1
    return float(score)


def walk_forward_sweep(
    frames: Dict[str, pd.DataFrame],
    *,
    rvol_grid: Sequence[float] = (1.5, 1.75, 2.0, 2.25, 2.5),
    rsi_grid: Sequence[float] = (65.0, 68.0, 70.0, 72.0),
    min_tp_grid: Sequence[float] = (0.025, 0.028, 0.03, 0.035),
) -> OptimizedParams:
    """Sweep params under high-vol vs low-vol regime split; pick best clamped set."""
    # Aggregate ATR% to decide global regime label for the run
    atr_vals: List[float] = []
    for df in frames.values():
        if df is None or df.empty:
            continue
        s = atr_pct_series(df)
        if len(s):
            atr_vals.append(float(s.iloc[-1]))
    if atr_vals:
        arr = np.array(atr_vals)
        hi = float(np.percentile(arr, 70))
        lo = float(np.percentile(arr, 30))
        med = float(np.median(arr))
        regime = classify_vol_regime(med, hi, lo)
    else:
        regime = "unknown"
        hi = lo = 0.0

    # Bias grids slightly by regime (still clamped later)
    if regime == "high_vol":
        rvol_use = [x for x in rvol_grid if x >= 2.0] or list(rvol_grid)
        tp_use = [x for x in min_tp_grid if x >= 0.028] or list(min_tp_grid)
    elif regime == "low_vol":
        rvol_use = [x for x in rvol_grid if x <= 2.0] or list(rvol_grid)
        tp_use = [x for x in min_tp_grid if x <= 0.03] or list(min_tp_grid)
    else:
        rvol_use = list(rvol_grid)
        tp_use = list(min_tp_grid)

    best: Optional[OptimizedParams] = None
    for rvol in rvol_use:
        for rsi in rsi_grid:
            for mtp in tp_use:
                clamped = clamp_params(
                    rvol_breakout_mult=rvol, rsi_buy_cap=rsi, min_tp_pct=mtp
                )
                total = 0.0
                for df in frames.values():
                    total += score_params_on_df(
                        df,
                        rvol_breakout_mult=clamped["rvol_breakout_mult"],
                        rsi_buy_cap=clamped["rsi_buy_cap"],
                        min_tp_pct=clamped["min_tp_pct"],
                    )
                cand = OptimizedParams(
                    rvol_breakout_mult=clamped["rvol_breakout_mult"],
                    rsi_buy_cap=clamped["rsi_buy_cap"],
                    min_tp_pct=clamped["min_tp_pct"],
                    regime=regime,
                    score=total,
                )
                if best is None or cand.score > best.score:
                    best = cand
    if best is None:
        best = OptimizedParams(
            rvol_breakout_mult=FLOOR_RVOL_BREAKOUT_MULT,
            rsi_buy_cap=CEILING_RSI_BUY_CAP,
            min_tp_pct=FLOOR_MIN_TP_PCT,
            regime=regime,
            score=-1e9,
        )
    return best.clamped()


def fetch_binance_klines(
    perp: str,
    *,
    interval: str = "1m",
    days: int = 14,
    limit_per_call: int = 1000,
) -> pd.DataFrame:
    """Public klines — Binance futures first, Bybit linear fallback (no API key)."""
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - int(days * 86400 * 1000)
    rows: List[list] = []
    cursor = start_ms
    try:
        with httpx.Client(timeout=20.0) as client:
            while cursor < end_ms and len(rows) < days * 24 * 60 + 100:
                r = client.get(
                    "https://fapi.binance.com/fapi/v1/klines",
                    params={
                        "symbol": perp,
                        "interval": interval,
                        "startTime": cursor,
                        "endTime": end_ms,
                        "limit": limit_per_call,
                    },
                )
                r.raise_for_status()
                batch = r.json()
                if not batch:
                    break
                rows.extend(batch)
                cursor = int(batch[-1][0]) + 60_000
                if len(batch) < limit_per_call:
                    break
    except Exception as exc:
        logger.info("Binance klines unavailable (%s) — trying Bybit", exc)
        rows = []
        cursor = start_ms
        with httpx.Client(timeout=20.0) as client:
            while cursor < end_ms and len(rows) < days * 24 * 60 + 100:
                r = client.get(
                    "https://api.bybit.com/v5/market/kline",
                    params={
                        "category": "linear",
                        "symbol": perp,
                        "interval": "1",
                        "start": cursor,
                        "end": end_ms,
                        "limit": min(1000, limit_per_call),
                    },
                )
                r.raise_for_status()
                result = (r.json() or {}).get("result") or {}
                batch = result.get("list") or []
                if not batch:
                    break
                # Bybit returns newest-first
                batch = list(reversed(batch))
                for item in batch:
                    # [start, open, high, low, close, volume, turnover]
                    rows.append(
                        [
                            int(item[0]),
                            item[1],
                            item[2],
                            item[3],
                            item[4],
                            item[5],
                            0,
                            0,
                            0,
                            0,
                            0,
                            0,
                        ]
                    )
                cursor = int(batch[-1][0]) + 60_000
                if len(batch) < 200:
                    break
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(
        rows,
        columns=[
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time",
            "quote_volume",
            "trades",
            "taker_buy_base",
            "taker_buy_quote",
            "ignore",
        ],
    )
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    df["timestamp"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.drop_duplicates(subset=["open_time"]).set_index("timestamp")
    return df[["open", "high", "low", "close", "volume"]]


def load_frames_from_feed_cache(feed_frames: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
    out: Dict[str, pd.DataFrame] = {}
    for sym, df in (feed_frames or {}).items():
        if df is None or df.empty:
            continue
        cols = {c.lower(): c for c in df.columns}
        need = ["open", "high", "low", "close", "volume"]
        if not all(k in cols for k in need):
            continue
        piece = df[[cols[k] for k in need]].copy()
        piece.columns = need
        out[sym.upper()] = piece
    return out


def run_optimizer(
    *,
    symbols: Optional[Sequence[str]] = None,
    days: int = 14,
    out_path: Optional[Path] = None,
    feed_frames: Optional[Dict[str, pd.DataFrame]] = None,
    use_network: bool = True,
) -> OptimizedParams:
    symbols = [s.upper() for s in (symbols or list(HARD_SYMBOL_ALLOWLIST))]
    frames = load_frames_from_feed_cache(feed_frames or {})
    if use_network:
        for spot in symbols:
            if spot in frames and len(frames[spot]) >= 500:
                continue
            perp = SPOT_TO_BINANCE.get(spot)
            if not perp:
                continue
            try:
                df = fetch_binance_klines(perp, days=days)
                if not df.empty:
                    frames[spot] = df
                    logger.info("optimizer loaded %s bars=%d from Binance", spot, len(df))
            except Exception as exc:
                logger.warning("optimizer kline fetch %s failed: %s", spot, exc)
    path = Path(out_path) if out_path else DEFAULT_ACTIVE_PARAMS_PATH
    prev_market_regime = _read_previous_market_regime(path)

    if not frames:
        logger.warning("optimizer: no frames — writing conservative defaults (not loosened)")
        params = OptimizedParams(
            rvol_breakout_mult=2.0,  # production default; floor is 1.5
            rsi_buy_cap=CEILING_RSI_BUY_CAP,
            min_tp_pct=FLOOR_MIN_TP_PCT,
            regime="no_data",
            score=-1e9,
            lookback_days=days,
            market_regime=prev_market_regime or "RANGING",
            regime_updated_at=datetime.now(timezone.utc).isoformat(),
            regime_features={"reason": "no_frames", "kept_previous": True},
        )
    else:
        params = walk_forward_sweep(frames)
        params.lookback_days = days
        # Phase 2: ATR+ADX k-means macro regime (fail soft — never crash optimizer)
        try:
            mr, feats = classify_market_regime_atr_adx(
                frames, fallback=prev_market_regime or "RANGING"
            )
            params.market_regime = mr
            params.regime_features = feats
            params.regime_updated_at = datetime.now(timezone.utc).isoformat()
        except Exception as exc:
            logger.warning(
                "optimizer market_regime classify failed (%s) — keep %s",
                exc,
                prev_market_regime,
            )
            params.market_regime = prev_market_regime or "RANGING"
            params.regime_features = {"error": str(exc), "kept_previous": True}
            params.regime_updated_at = datetime.now(timezone.utc).isoformat()

    params.timestamp = datetime.now(timezone.utc).isoformat()
    params = params.clamped()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(params)
    # Expose sizing hint alongside regime for strategy/risk hot-reload
    from trading_bot.strategy import regime_trade_cap_mult as _cap_mult

    payload["regime_trade_cap_mult"] = float(_cap_mult(params.market_regime))
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    logger.info(
        "optimizer wrote %s regime=%s market_regime=%s rvol=%.2f rsi_cap=%.1f "
        "min_tp=%.3f score=%.2f",
        path,
        params.regime,
        params.market_regime,
        params.rvol_breakout_mult,
        params.rsi_buy_cap,
        params.min_tp_pct,
        params.score,
    )
    return params


def load_active_params(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    p = Path(path) if path else DEFAULT_ACTIVE_PARAMS_PATH
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        clamped = clamp_params(
            rvol_breakout_mult=float(raw.get("rvol_breakout_mult", FLOOR_RVOL_BREAKOUT_MULT)),
            rsi_buy_cap=float(raw.get("rsi_buy_cap", CEILING_RSI_BUY_CAP)),
            min_tp_pct=float(raw.get("min_tp_pct", FLOOR_MIN_TP_PCT)),
        )
        raw.update(clamped)
        # Normalize Phase 2 macro regime fields (fail-soft defaults)
        from trading_bot.strategy import (
            normalize_market_regime,
            regime_trade_cap_mult,
        )

        mr = normalize_market_regime(raw.get("market_regime"), default="RANGING")
        raw["market_regime"] = mr
        if "regime_trade_cap_mult" not in raw:
            raw["regime_trade_cap_mult"] = float(regime_trade_cap_mult(mr))
        return raw
    except Exception as exc:
        logger.warning("load_active_params failed: %s", exc)
        return None


def seconds_until_chicago_midnight(now: Optional[datetime] = None) -> float:
    now = now or datetime.now(CHICAGO)
    if now.tzinfo is None:
        now = now.replace(tzinfo=CHICAGO)
    else:
        now = now.astimezone(CHICAGO)
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    # If exactly midnight, schedule next day
    if now >= nxt:
        nxt = nxt + timedelta(days=1)
    # If before today's midnight and we want tonight: actually "midnight" means next 00:00
    today_mid = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if now > today_mid:
        target = today_mid + timedelta(days=1)
    else:
        target = today_mid
    return max(1.0, (target - now).total_seconds())


async def nightly_optimizer_loop(
    stop_event,
    *,
    symbols: Sequence[str],
    out_path: Path,
    get_feed_frames=None,
    on_complete=None,
) -> None:
    """asyncio task: sleep until America/Chicago midnight, run optimizer, hot-reload callback."""
    import asyncio

    logger.info(
        "OPTIMIZER nightly armed (America/Chicago midnight) out=%s",
        out_path,
    )
    while not stop_event.is_set():
        wait_s = seconds_until_chicago_midnight()
        logger.info("OPTIMIZER next run in %.0fs (Chicago midnight)", wait_s)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=wait_s)
            break  # stop requested
        except asyncio.TimeoutError:
            pass
        try:
            frames = get_feed_frames() if callable(get_feed_frames) else None
            params = run_optimizer(
                symbols=symbols,
                days=14,
                out_path=out_path,
                feed_frames=frames,
                use_network=True,
            )
            if callable(on_complete):
                on_complete(params)
        except Exception as exc:
            logger.warning("OPTIMIZER nightly run failed: %s", exc)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    run_optimizer(days=14, use_network=True)


if __name__ == "__main__":
    main()
