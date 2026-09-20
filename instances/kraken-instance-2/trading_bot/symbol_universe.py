"""Independent Kraken crypto and tokenized-equity (xStocks) universes."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import aiohttp

from trading_bot.config import (
    DEFAULT_SYMBOL_ALLOWLIST,
    HARD_SYMBOL_ALLOWLIST,
    PROJECT_ROOT,
    Settings,
    normalize_active_symbol,
    set_runtime_active_symbols,
    upsert_env_keys,
)

logger = logging.getLogger(__name__)
KRAKEN_ASSET_PAIRS_URL = "https://api.kraken.com/0/public/AssetPairs"
KRAKEN_TICKER_URL = "https://api.kraken.com/0/public/Ticker"
MIN_USD_VOLUME_24H = 500_000.0
MIN_STOCK_USD_VOLUME_24H = 50_000.0
MAX_SPREAD_PCT = 0.5
REFRESH_INTERVAL_SEC = 3600.0
TICKER_BATCH_SIZE = 40
TOKENIZED_ASSET_CLASS = "tokenized_asset"
# Never discover / subscribe / scan these fiat-backed & stable pairs.
STABLECOIN_BLACKLIST = frozenset({
    "EURC-USD",
    "USDT-USD",
    "USDC-USD",
    "DAI-USD",
    "PYUSD-USD",
    "EURG-USD",
    "USDG-USD",
    "PAXG-USD",
})

_FIAT_BASES = frozenset({"USD", "ZUSD", "EUR", "ZEUR", "GBP", "ZGBP", "AUD", "CAD", "JPY", "CHF", "USDT", "ZUSDT", "USDC", "DAI", "EURT"})
_ASSET_FROM_KRAKEN = {"XBT":"BTC", "XXBT":"BTC", "XETH":"ETH", "XXRP":"XRP", "XLTC":"LTC", "ZUSD":"USD", "ZUSDT":"USDT", "ZEUR":"EUR", "ZGBP":"GBP"}


def _from_kraken_asset(asset: str) -> str:
    a = (asset or "").strip().upper()
    if a in _ASSET_FROM_KRAKEN:
        return _ASSET_FROM_KRAKEN[a]
    if len(a) > 3 and a[0] in ("X", "Z") and a[1:] in _ASSET_FROM_KRAKEN:
        return _ASSET_FROM_KRAKEN[a[1:]]
    if a.startswith("X") and a[1:] in ("ETH", "XRP", "LTC", "BTC", "XBT"):
        return "BTC" if a[1:] in ("BTC", "XBT") else a[1:]
    if a.startswith("Z") and a[1:] in ("USD", "EUR", "GBP", "USDT"):
        return a[1:]
    return a


def _split_pair(raw: str) -> Tuple[str, str]:
    s = (raw or "").strip().replace(" ", "").replace("_", "/")
    if "/" in s:
        return tuple(s.split("/", 1))  # type: ignore[return-value]
    if "-" in s:
        return tuple(s.split("-", 1))  # type: ignore[return-value]
    for q in ("ZUSD", "ZUSDT", "ZEUR", "ZGBP", "USDT", "USD", "EUR", "GBP"):
        if s.upper().endswith(q) and len(s) > len(q):
            return s[:-len(q)], s[-len(q):]
    return s, "USD"


def _tokenized_standard(raw: str) -> Optional[str]:
    base, quote = _split_pair(raw)
    quote_iso = _from_kraken_asset(quote)
    if quote_iso != "USD" or not base or not base.lower().endswith("x"):
        return None
    # Kraken xStocks are deliberately rendered as AAPLx-USD, not AAPLX-USD.
    return f"{base[:-1].upper()}x-USD"


def normalize_to_base_usd(raw: str) -> Optional[str]:
    """Normalize a pair to BASE-USD, preserving tokenized ``x`` suffixes."""
    tokenized = _tokenized_standard(raw)
    if tokenized:
        return tokenized
    try:
        from trading_bot.brokers.kraken import to_standard_symbol
        standard = to_standard_symbol(raw)
    except Exception:
        standard = _fallback_normalize(raw)
    if not standard or "-" not in standard:
        return None
    base, quote = standard.split("-", 1)
    if quote != "USD" or not base or base in _FIAT_BASES:
        return None
    if base in ("XBT", "XXBT"):
        base = "BTC"
    return f"{base}-USD"



def is_stablecoin_excluded(symbol: str) -> bool:
    """True if symbol is on the hard stablecoin / fiat-backed exclude list."""
    s = normalize_active_symbol(symbol) if symbol else ""
    if not s:
        s = str(symbol or "").strip().upper()
    return s in STABLECOIN_BLACKLIST


def drop_stablecoins(symbols) -> List[str]:
    """Filter stables out; preserve order."""
    out: List[str] = []
    for sym in symbols or []:
        s = normalize_active_symbol(sym) if sym else ""
        if not s:
            continue
        if s in STABLECOIN_BLACKLIST:
            continue
        if s not in out:
            out.append(s)
    return out


def _fallback_normalize(raw: str) -> str:
    base, quote = _split_pair(raw)
    return f"{_from_kraken_asset(base)}-{_from_kraken_asset(quote)}"


def _asset_class(meta: Mapping[str, Any]) -> str:
    return str(meta.get("asset_class") or meta.get("aclass_base") or meta.get("asset_class_base") or "").strip().lower()


def pair_is_active_spot_usd(meta: Mapping[str, Any], pair_key: str = "") -> bool:
    """True for online, non-dark, ordinary spot USD quote pairs."""
    if not isinstance(meta, Mapping):
        return False
    quote = str(meta.get("quote") or "").upper()
    status = str(meta.get("status") or "online").strip().lower()
    if quote not in ("ZUSD", "USD") or status not in ("online", "enabled", ""):
        return False
    alt = str(meta.get("altname") or pair_key or "")
    if ".d" in alt.lower() or any(x in status for x in ("dark", "halt", "cancel")):
        return False
    return _from_kraken_asset(str(meta.get("base") or "").upper()) not in _FIAT_BASES


def pair_is_active_tokenized_usd(meta: Mapping[str, Any], pair_key: str = "") -> bool:
    """True for fully tradeable Kraken ``tokenized_asset`` USD xStock pairs.

    Only ``online`` (and legacy ``enabled``) — excludes ``post_only`` listings
    that are not freely tradeable 24h spot. Drops dark/halted/SPV duplicates.
    """
    if not isinstance(meta, Mapping) or _asset_class(meta) != TOKENIZED_ASSET_CLASS:
        return False
    if str(meta.get("quote") or "").upper() not in ("USD", "ZUSD"):
        return False
    status = str(meta.get("status") or "").strip().lower()
    # User wants 24h stocks they can actually trade — online book only.
    if status not in ("online", "enabled"):
        return False
    alt = str(meta.get("altname") or pair_key or "")
    if ".d" in alt.lower():
        return False
    if "SPV" in alt.upper() and not alt.upper().endswith("XUSD"):
        # Prefer AAPLxUSD over AAPLSPVUSD duplicates
        return False
    return True


def filter_by_volume_spread(ticker_row: Mapping[str, Any], *, min_usd_volume: float = MIN_USD_VOLUME_24H, max_spread_pct: float = MAX_SPREAD_PCT) -> Tuple[bool, float, float]:
    """Return (pass, USD 24h volume, spread percent) from a Kraken Ticker row."""
    try: vol_base = float((ticker_row.get("v") or [0, 0])[1] or 0)
    except (TypeError, ValueError, IndexError): vol_base = 0.0
    try: vwap = float((ticker_row.get("p") or [0, 0])[1] or 0)
    except (TypeError, ValueError, IndexError): vwap = 0.0
    if vwap <= 0:
        try: vwap = float((ticker_row.get("c") or [0])[0] or 0)
        except (TypeError, ValueError, IndexError): vwap = 0.0
    usd_vol = vol_base * vwap if vwap > 0 else 0.0
    try:
        ask = float((ticker_row.get("a") or [0])[0] or 0); bid = float((ticker_row.get("b") or [0])[0] or 0)
    except (TypeError, ValueError, IndexError): ask = bid = 0.0
    mid = (ask + bid) / 2.0 if ask > 0 and bid > 0 else 0.0
    spread = 100.0 * (ask - bid) / mid if mid > 0 and ask >= bid else 999.0
    return usd_vol >= float(min_usd_volume) and spread <= float(max_spread_pct), usd_vol, spread


def format_symbols_chunks(symbols: Sequence[str], *, per_line: int = 5) -> str:
    syms = [str(s) for s in symbols]
    if not syms: return "(none)"
    n = max(1, int(per_line)); return "\n".join(", ".join(syms[i:i+n]) for i in range(0, len(syms), n))


class SymbolUniverse:
    """Maintains independent crypto and xStocks lists and their merged runtime list."""
    def __init__(self, settings: Settings, *, base_url: str = "https://api.kraken.com", min_usd_volume: float = MIN_USD_VOLUME_24H, max_spread_pct: float = MAX_SPREAD_PCT, refresh_interval_sec: float = REFRESH_INTERVAL_SEC) -> None:
        self.settings = settings
        self.base_url = (base_url or "https://api.kraken.com").rstrip("/")
        self.min_usd_volume = float(min_usd_volume)
        self.max_spread_pct = float(max_spread_pct)
        self.refresh_interval_sec = float(refresh_interval_sec)
        self._lock = asyncio.Lock(); self._refresh_event = asyncio.Event()
        self._crypto_active: List[str] = []; self._stocks_active: List[str] = []
        self._pair_rest: Dict[str, str] = {}; self._stock_pair_rest: Dict[str, str] = {}
        self._last_refresh_ts = 0.0; self._last_error = ""
        mode = self._norm_mode(getattr(settings, "symbol_mode", "ALLOWLIST")); object.__setattr__(settings, "symbol_mode", mode)
        set_runtime_active_symbols(None)
        self._reset_crypto_seed(); self._rebuild_active()

    @staticmethod
    def _norm_mode(raw: Any) -> str:
        m = str(raw or "ALLOWLIST").strip().upper().replace("-", "_")
        if m in ("OFF", "NONE", "CRYPTO_OFF", "STOCKS_ONLY"): return "OFF"
        if m in ("DYNAMIC", "DYNAMIC_ALL", "ALL", "KRAKEN", "DISCOVERY"): return "DYNAMIC_ALL"
        return "ALLOWLIST"

    @property
    def mode(self) -> str: return self._norm_mode(getattr(self.settings, "symbol_mode", "ALLOWLIST"))
    @property
    def stocks_enabled(self) -> bool: return bool(getattr(self.settings, "universe_stocks", False))
    @property
    def active_symbols(self) -> List[str]: return list(self._active)
    @property
    def stock_symbols(self) -> List[str]: return list(self._stocks_active)
    @property
    def crypto_symbols(self) -> List[str]: return list(self._crypto_active)

    def _allowlist_crypto_symbols(self) -> List[str]:
        requested = [self.settings.normalize_symbol(s) for s in str(self.settings.symbols or "").split(",") if str(s).strip()]
        allowed = [s for s in requested if s in HARD_SYMBOL_ALLOWLIST]
        return drop_stablecoins(list(dict.fromkeys(allowed or DEFAULT_SYMBOL_ALLOWLIST)))

    def _reset_crypto_seed(self) -> None:
        self._crypto_active = [] if self.mode == "OFF" else list(DEFAULT_SYMBOL_ALLOWLIST if self.mode == "DYNAMIC_ALL" else self._allowlist_crypto_symbols())

    def _rebuild_active(self) -> None:
        merged: List[str] = []
        for sym in ([*self._crypto_active] if self.mode != "OFF" else []) + (self._stocks_active if self.stocks_enabled else []):
            sym = normalize_active_symbol(sym)
            if not sym or is_stablecoin_excluded(sym):
                continue
            if sym not in merged:
                merged.append(sym)
        self._active = merged
        # Runtime override is needed for xStocks and explicit OFF (including empty).
        if self.stocks_enabled or self.mode in ("DYNAMIC_ALL", "OFF"):
            set_runtime_active_symbols(merged)
        else:
            set_runtime_active_symbols(None)

    def set_mode(self, mode: str, *, persist: bool = True) -> str:
        m = self._norm_mode(mode); object.__setattr__(self.settings, "symbol_mode", m)
        self._reset_crypto_seed(); self._rebuild_active()
        if persist: upsert_env_keys(PROJECT_ROOT / ".env", {"SYMBOL_MODE": m})
        return m

    def set_stocks_enabled(self, enabled: bool, *, persist: bool = True) -> bool:
        val = bool(enabled); object.__setattr__(self.settings, "universe_stocks", val)
        if not val: self._stocks_active = []
        self._rebuild_active()
        if persist: upsert_env_keys(PROJECT_ROOT / ".env", {"UNIVERSE_STOCKS": "true" if val else "false"})
        return val

    def request_refresh(self) -> None: self._refresh_event.set()

    def status_counts(self) -> Dict[str, Any]:
        return {"mode": self.mode, "count": len(self._active), "crypto_count": len(self._crypto_active), "stocks_enabled": self.stocks_enabled, "stocks_count": len(self._stocks_active), "allowlist_count": len(HARD_SYMBOL_ALLOWLIST), "last_refresh_ts": self._last_refresh_ts, "last_error": self._last_error}

    async def _get_json(self, path: str, params: Optional[Mapping[str, str]] = None) -> Mapping[str, Any]:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{self.base_url}{path}", params=params, timeout=aiohttp.ClientTimeout(total=45)) as resp:
                resp.raise_for_status(); payload = await resp.json()
        if payload.get("error"): raise RuntimeError(f"Kraken API error: {payload.get('error')}")
        return payload

    async def _discover_usd_pairs(self) -> Dict[str, str]:
        payload = await self._get_json("/0/public/AssetPairs")
        out: Dict[str, str] = {}
        stables_hit = 0
        for key, meta in (payload.get("result") or {}).items():
            if not pair_is_active_spot_usd(meta, str(key)):
                continue
            rest = str(meta.get("altname") or key)
            standard = None
            for candidate in (meta.get("wsname"), meta.get("altname"), key):
                if candidate and normalize_to_base_usd(str(candidate)):
                    standard = normalize_to_base_usd(str(candidate))
                    break
            if not standard:
                continue
            if is_stablecoin_excluded(standard):
                stables_hit += 1
                continue
            if standard not in out or len(rest) < len(out[standard]):
                out[standard] = rest
        self._pair_rest = dict(out)
        logger.info(
            "Kraken discovery: %s USD spot pairs (excluded %s stablecoin hits, blacklist=%s)",
            len(out),
            stables_hit,
            len(STABLECOIN_BLACKLIST),
        )
        return out

    async def _discover_tokenized_pairs(self) -> Dict[str, str]:
        payload = await self._get_json("/0/public/AssetPairs", {"aclass_base": TOKENIZED_ASSET_CLASS})
        out: Dict[str, str] = {}
        for key, meta in (payload.get("result") or {}).items():
            if not pair_is_active_tokenized_usd(meta, str(key)): continue
            rest = str(meta.get("altname") or key); standard = None
            for candidate in (meta.get("wsname"), meta.get("altname"), meta.get("base"), key):
                if candidate and normalize_to_base_usd(str(candidate)) and str(normalize_to_base_usd(str(candidate)) or "").lower().endswith("x-usd"):
                    standard = normalize_to_base_usd(str(candidate)); break
            if standard and (standard not in out or len(rest) < len(out[standard])): out[standard] = rest
        self._stock_pair_rest = dict(out); return out

    async def _liquidity_filter(self, discovered: Mapping[str, str], *, min_usd_volume: Optional[float] = None, ticker_params: Optional[Mapping[str, str]] = None, require_asset_class: bool = False) -> List[str]:
        if not discovered: return []
        rest_to_std = {v:k for k,v in discovered.items()}; tokens = list(discovered.values()); tickers: Dict[str, Any] = {}
        async with aiohttp.ClientSession() as session:
            for i in range(0, len(tokens), TICKER_BATCH_SIZE):
                batch = tokens[i:i+TICKER_BATCH_SIZE]
                try:
                    async with session.get(f"{self.base_url}/0/public/Ticker", params={"pair": ",".join(batch), **dict(ticker_params or {})}, timeout=aiohttp.ClientTimeout(total=45)) as resp:
                        resp.raise_for_status(); payload = await resp.json()
                except Exception as exc:
                    logger.warning("Ticker batch failed (%s): %s", len(batch), exc); continue
                for key, row in (payload.get("result") or {}).items():
                    if require_asset_class and isinstance(row, Mapping):
                        klass = str(row.get("asset_class") or row.get("aclass_base") or "").lower()
                        if klass and klass != TOKENIZED_ASSET_CLASS: continue
                    tickers[str(key)] = row
        passed: List[str] = []
        for standard, rest in discovered.items():
            row = tickers.get(rest)
            if row is None:
                row = next((tr for tk,tr in tickers.items() if normalize_to_base_usd(tk) == standard), None)
            if row is not None and filter_by_volume_spread(row, min_usd_volume=min_usd_volume if min_usd_volume is not None else self.min_usd_volume, max_spread_pct=self.max_spread_pct)[0]: passed.append(standard)
        return passed

    async def refresh(self) -> List[str]:
        async with self._lock:
            errors: List[str] = []
            if self.mode == "DYNAMIC_ALL":
                try:
                    discovered = await self._discover_usd_pairs()
                    crypto = drop_stablecoins(await self._liquidity_filter(discovered))
                    if crypto:
                        self._crypto_active = sorted(set(crypto), key=lambda s: (DEFAULT_SYMBOL_ALLOWLIST.index(s) if s in DEFAULT_SYMBOL_ALLOWLIST else 10000, s))
                except Exception as exc:
                    errors.append(f"crypto: {exc}")
            elif self.mode == "OFF":
                self._crypto_active = []
            else:
                self._crypto_active = self._allowlist_crypto_symbols()
            if self.stocks_enabled:
                try:
                    stock_discovered = await self._discover_tokenized_pairs()
                    # Online-only xStocks (24h freely tradeable — not post_only dump)
                    if stock_discovered:
                        self._stocks_active = sorted(set(stock_discovered))
                    else:
                        self._stocks_active = []
                except Exception as exc:
                    errors.append(f"stocks: {exc}")
            else:
                self._stocks_active = []
            before_crypto = len(self._crypto_active)
            self._crypto_active = drop_stablecoins(self._crypto_active)
            stables_dropped = before_crypto - len(self._crypto_active)
            self._rebuild_active()
            self._last_error = "; ".join(errors)
            self._last_refresh_ts = time.time()
            if errors:
                logger.warning("symbol_universe refresh degraded: %s", self._last_error)
            logger.info(
                "active tradable universe: %s (crypto=%s stocks=%s online-only; stables_dropped=%s blacklist=%s)",
                len(self._active),
                len(self._crypto_active),
                len(self._stocks_active),
                stables_dropped,
                len(STABLECOIN_BLACKLIST),
            )
            if not errors:
                logger.info(
                    "symbol_universe refreshed mode=%s crypto=%s stocks=%s total=%s",
                    self.mode,
                    len(self._crypto_active),
                    len(self._stocks_active),
                    len(self._active),
                )
            return list(self._active)

    async def run_loop(self, stop_event: Any) -> None:
        logger.info("symbol_universe loop armed interval=%.0fs mode=%s stocks=%s", self.refresh_interval_sec, self.mode, self.stocks_enabled)
        if self.mode == "DYNAMIC_ALL" or self.stocks_enabled: await self.refresh()
        while not stop_event.is_set():
            self._refresh_event.clear()
            wait_refresh = asyncio.create_task(self._refresh_event.wait()); wait_stop = asyncio.create_task(stop_event.wait())
            try:
                _, pending = await asyncio.wait({wait_refresh, wait_stop}, timeout=self.refresh_interval_sec, return_when=asyncio.FIRST_COMPLETED)
                for task in pending: task.cancel()
                if stop_event.is_set(): break
                if self.mode == "DYNAMIC_ALL" or self.stocks_enabled or self._refresh_event.is_set(): await self.refresh()
            except Exception as exc:
                logger.warning("symbol_universe loop error: %s", exc)
                try: await asyncio.wait_for(stop_event.wait(), timeout=30.0)
                except asyncio.TimeoutError: pass
        logger.info("symbol_universe loop stopped")

_ENGINE_UNIVERSE: Optional[SymbolUniverse] = None
def bind_universe(universe: Optional[SymbolUniverse]) -> None:
    global _ENGINE_UNIVERSE; _ENGINE_UNIVERSE = universe
def get_bound_universe() -> Optional[SymbolUniverse]: return _ENGINE_UNIVERSE
