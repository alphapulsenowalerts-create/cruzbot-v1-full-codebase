"""Kraken spot broker adapter (REST + optional public WS subscribe).

Implements :class:`trading_bot.brokers.base.BrokerAdapter` so the executor /
factory wiring stays the same as Coinbase. Paper mode **never** hits live
AddOrder / Cancel endpoints — same hard gate as ``CoinbaseBroker``.

Symbol mapping
--------------
CruzBot keep symbols in Coinbase-style form (``BTC-USD``, ``ETH/USD``,
``BTCUSD``). Kraken uses three related names depending on the endpoint:

====================  =================  ========================================
Form                  Example            Used by
====================  =================  ========================================
REST ``pair``         ``XBTUSD``         AddOrder, OHLC, Ticker, Depth
WebSocket ``wsname``  ``XBT/USD``        public WS v1 subscribe
Altname / result key  ``XXBTZUSD``       OHLC/Ticker/Depth result dict keys
====================  =================  ========================================

**BTC is always XBT on Kraken.** Older assets also carry X/Z class prefixes
(``XXBT``, ``ZUSD``). Newer allowlist assets (SOL, LINK, AVAX, SUI, ADA)
typically use unprefixed REST pairs (``SOLUSD``).

Helpers (unit-tested):

* :func:`to_standard_symbol` — normalize any input → ``BTC-USD``
* :func:`to_kraken_pair` — REST ``pair=`` value (``XBTUSD``)
* :func:`to_kraken_wsname` — WS v1 pair (``XBT/USD``)
* :func:`to_kraken_altname` — result-key / altname (``XXBTZUSD``)
* :func:`from_kraken_pair` — Kraken name → ``BTC-USD``

POST_ONLY
---------
When ``POST_ONLY`` (or ``order.post_only``) is true:

* market / non-limit orders are rejected with ``market_orders_disabled``
* live limit orders send Kraken ``oflags=post`` (maker-only; rejected if it
  would take)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from uuid import uuid4

import aiohttp

from trading_bot.brokers.base import BrokerAdapter
from trading_bot.config import HARD_SYMBOL_ALLOWLIST, Settings
from trading_bot.models import (
    AccountState,
    Bar,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Quote,
    utcnow,
)
from trading_bot.utils.retry import (
    ExponentialBackoff,
    RateLimitError,
    RetryableError,
    is_rate_limit_status,
    retry_async,
)

logger = logging.getLogger(__name__)

_API_HOST_DEFAULT = "https://api.kraken.com"
_WS_URL_DEFAULT = "wss://ws.kraken.com"
_PUBLIC_PREFIX = "/0/public/"
_PRIVATE_PREFIX = "/0/private/"

_TIMEFRAME_TO_INTERVAL = {
    "1Min": 1,
    "1MIN": 1,
    "5Min": 5,
    "5MIN": 5,
    "15Min": 15,
    "15MIN": 15,
    "1Hour": 60,
    "1H": 60,
    "4Hour": 240,
    "4H": 240,
    "4Hr": 240,
    "1Day": 1440,
    "1D": 1440,
}

_STATUS_MAP = {
    "pending": OrderStatus.PENDING,
    "open": OrderStatus.SUBMITTED,
    "closed": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELLED,
    "cancelled": OrderStatus.CANCELLED,
    "expired": OrderStatus.EXPIRED,
}

# Classic X/Z-prefixed Kraken assets → ISO codes used by CruzBot.
_ASSET_FROM_KRAKEN = {
    "XBT": "BTC",
    "XXBT": "BTC",
    "XETH": "ETH",
    "XXRP": "XRP",
    "XLTC": "LTC",
    "ZUSD": "USD",
    "ZUSDT": "USDT",
    "ZEUR": "EUR",
    "ZGBP": "GBP",
}

# Allowlist + common aliases. rest / wsname / altname match Kraken AssetPairs.
# BTC is XBT everywhere on Kraken.
_PAIR_ROWS: Tuple[Tuple[str, str, str, str, str, str], ...] = (
    # standard, rest, wsname, altname, kraken_base, kraken_quote
    ("BTC-USD", "XBTUSD", "XBT/USD", "XXBTZUSD", "XXBT", "ZUSD"),
    ("ETH-USD", "ETHUSD", "ETH/USD", "XETHZUSD", "XETH", "ZUSD"),
    ("SOL-USD", "SOLUSD", "SOL/USD", "SOLUSD", "SOL", "ZUSD"),
    ("XRP-USD", "XRPUSD", "XRP/USD", "XXRPZUSD", "XXRP", "ZUSD"),
    ("LINK-USD", "LINKUSD", "LINK/USD", "LINKUSD", "LINK", "ZUSD"),
    ("AVAX-USD", "AVAXUSD", "AVAX/USD", "AVAXUSD", "AVAX", "ZUSD"),
    ("SUI-USD", "SUIUSD", "SUI/USD", "SUIUSD", "SUI", "ZUSD"),
    ("ADA-USD", "ADAUSD", "ADA/USD", "ADAUSD", "ADA", "ZUSD"),
)


@dataclass(frozen=True)
class KrakenPair:
    """Resolved Kraken names for one CruzBot standard pair (e.g. BTC-USD)."""

    standard: str
    rest: str
    wsname: str
    altname: str
    base: str
    quote: str


_CANONICAL: Dict[str, KrakenPair] = {}
_LOOKUP: Dict[str, KrakenPair] = {}


def _register_pair(row: Tuple[str, str, str, str, str, str]) -> KrakenPair:
    pair = KrakenPair(*row)
    _CANONICAL[pair.standard] = pair
    aliases = {
        pair.standard,
        pair.standard.replace("-", "/"),
        pair.standard.replace("-", ""),
        pair.rest,
        pair.wsname,
        pair.wsname.replace("/", "-"),
        pair.wsname.replace("/", ""),
        pair.altname,
        pair.base,  # XXBT → BTC-USD (USD default for allowlist)
    }
    # Extra BTC aliases (XBT is the Kraken name for Bitcoin)
    if pair.standard == "BTC-USD":
        aliases.update({"XBT", "XXBT", "XBT-USD", "XBT/USD", "XBTUSD", "BTCUSD"})
    for key in aliases:
        _LOOKUP[key.upper()] = pair
    return pair


for _row in _PAIR_ROWS:
    _register_pair(_row)


def _from_kraken_asset(asset: str) -> str:
    """Map a Kraken asset code (``XXBT``, ``ZUSD``, ``SOL``) to ISO (``BTC``)."""
    a = (asset or "").strip().upper()
    if not a:
        return a
    if a in _ASSET_FROM_KRAKEN:
        return _ASSET_FROM_KRAKEN[a]
    # Strip a single X/Z class prefix when the remainder is a known asset.
    if len(a) > 3 and a[0] in ("X", "Z") and a[1:] in _ASSET_FROM_KRAKEN:
        return _ASSET_FROM_KRAKEN[a[1:]]
    if a.startswith("X") and a[1:] in ("ETH", "XRP", "LTC", "BTC", "XBT"):
        return "BTC" if a[1:] in ("BTC", "XBT") else a[1:]
    if a.startswith("Z") and a[1:] in ("USD", "EUR", "GBP", "USDT"):
        return a[1:]
    return a


def _split_pair_token(raw: str) -> Tuple[str, str]:
    """Split ``XBT/USD``, ``XBT-USD``, ``XBTUSD``, ``XXBTZUSD`` → (base, quote)."""
    s = raw.strip().upper().replace("_", "/")
    if "/" in s:
        base, quote = s.split("/", 1)
        return base, quote
    if "-" in s:
        base, quote = s.split("-", 1)
        return base, quote
    for q in ("ZUSD", "ZUSDT", "ZEUR", "ZGBP", "USDT", "USD", "EUR", "GBP"):
        if s.endswith(q) and len(s) > len(q):
            return s[: -len(q)], q
    return s, "USD"


def to_standard_symbol(symbol: str) -> str:
    """Normalize Coinbase- or Kraken-style input to ``BTC-USD``.

    Accepts ``btc``, ``BTC-USD``, ``BTC/USD``, ``BTCUSD``, ``XBTUSD``,
    ``XBT/USD``, and ``XXBTZUSD``.
    """
    raw = (symbol or "").strip()
    if not raw:
        return raw
    key = raw.upper().replace(" ", "")
    if key in _LOOKUP:
        return _LOOKUP[key].standard
    # Bare bases (ETH, SOL, …)
    compact = key.replace("/", "-").replace("_", "-")
    if compact in _LOOKUP:
        return _LOOKUP[compact].standard
    known_bases = ("BTC", "XBT", "ETH", "SOL", "XRP", "LINK", "AVAX", "SUI", "ADA")
    if compact in known_bases or compact in ("XXBT", "XETH", "XXRP"):
        iso = _from_kraken_asset(compact)
        return f"{iso}-USD"
    base, quote = _split_pair_token(compact)
    iso_base = _from_kraken_asset(base)
    iso_quote = _from_kraken_asset(quote)
    standard = f"{iso_base}-{iso_quote}"
    if standard in _CANONICAL:
        return standard
    return standard


def resolve_kraken_pair(symbol: str) -> KrakenPair:
    """Return the :class:`KrakenPair` for any recognized symbol form."""
    standard = to_standard_symbol(symbol)
    if standard in _CANONICAL:
        return _CANONICAL[standard]
    base, quote = standard.split("-", 1) if "-" in standard else (standard, "USD")
    kbase = "XBT" if base == "BTC" else base
    kquote = "USD" if quote == "USD" else quote
    rest = f"{kbase}{kquote}"
    wsname = f"{kbase}/{kquote}"
    # Classic fiat/crypto class prefixes when we know them
    alt_base = {"BTC": "XXBT", "ETH": "XETH", "XRP": "XXRP"}.get(base, base)
    alt_quote = {"USD": "ZUSD", "EUR": "ZEUR"}.get(quote, quote)
    altname = f"{alt_base}{alt_quote}"
    return KrakenPair(
        standard=standard,
        rest=rest,
        wsname=wsname,
        altname=altname,
        base=alt_base,
        quote=alt_quote,
    )


def to_kraken_pair(symbol: str) -> str:
    """REST ``pair`` parameter, e.g. ``BTC-USD`` / ``BTC/USD`` → ``XBTUSD``."""
    return resolve_kraken_pair(symbol).rest


def to_kraken_wsname(symbol: str) -> str:
    """Public WS v1 pair name, e.g. ``BTC-USD`` → ``XBT/USD``."""
    return resolve_kraken_pair(symbol).wsname


def to_kraken_altname(symbol: str) -> str:
    """OHLC/Ticker result key / altname, e.g. ``BTC-USD`` → ``XXBTZUSD``."""
    return resolve_kraken_pair(symbol).altname


def from_kraken_pair(pair: str) -> str:
    """Inverse of :func:`to_kraken_pair` / altname / wsname → ``BTC-USD``."""
    return to_standard_symbol(pair)


def ws_subscribe_message(
    symbols: List[str],
    *,
    channel: str = "ticker",
) -> Dict[str, Any]:
    """Kraken public WS v1 subscribe payload using ``wsname`` pairs (``XBT/USD``)."""
    return {
        "event": "subscribe",
        "pair": [to_kraken_wsname(s) for s in symbols],
        "subscription": {"name": channel},
    }


def sign_kraken_request(urlpath: str, data: Dict[str, Any], secret_b64: str) -> str:
    """HMAC-SHA512 API-Sign for a private Kraken REST call.

    ``secret_b64`` is the base64-encoded API secret from Kraken (never a
    live credential in tests — use a dummy ``base64.b64encode(...)``).
    """
    postdata = urllib.parse.urlencode(data)
    encoded = (str(data["nonce"]) + postdata).encode()
    message = urlpath.encode() + hashlib.sha256(encoded).digest()
    secret = base64.b64decode(secret_b64)
    mac = hmac.new(secret, message, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


def _wants_post_only(order: OrderRequest, settings: Settings) -> bool:
    return bool(
        getattr(order, "post_only", False) or getattr(settings, "post_only", True)
    )


def build_add_order_payload(
    order: OrderRequest,
    *,
    post_only: bool,
) -> Dict[str, Any]:
    """Build Kraken ``AddOrder`` fields including ``oflags=post`` when maker-only."""
    payload: Dict[str, Any] = {
        "pair": to_kraken_pair(order.symbol),
        "type": order.side.value.lower(),
        "ordertype": "limit" if order.order_type == OrderType.LIMIT else "market",
        "volume": str(order.qty),
    }
    if order.order_type == OrderType.LIMIT and order.limit_price is not None:
        payload["price"] = str(order.limit_price)
    flags: List[str] = []
    if post_only:
        flags.append("post")
    if flags:
        payload["oflags"] = ",".join(flags)
    if order.client_order_id:
        payload["cl_ord_id"] = order.client_order_id
    return payload


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if value is None or value == "":
        return utcnow()
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    s = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        try:
            return datetime.fromtimestamp(float(s), tz=timezone.utc)
        except ValueError:
            return utcnow()


def _first_numeric_list(result: Dict[str, Any]) -> Any:
    """Return the first non-``last`` list/dict value from a Kraken public result."""
    for key, value in (result or {}).items():
        if key == "last":
            continue
        return value
    return None


def _apply_trade_fee(settings: Settings, notional: float, *, is_taker: bool = True) -> float:
    rate = float(getattr(settings, "taker_fee_rate", 0.009 if is_taker else 0.005))
    if not is_taker:
        rate = float(getattr(settings, "maker_fee_rate", 0.005))
    return max(0.0, float(notional) * rate)


class KrakenBroker(BrokerAdapter):
    """Kraken spot adapter with hard paper-mode order gate and POST_ONLY maker flags."""

    name = "kraken"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._session: Optional[aiohttp.ClientSession] = None
        self._connected = False
        self._day_start_equity: Optional[float] = None
        self._paper_cash = float(settings.account_equity)
        self._paper_equity = float(settings.account_equity)
        self._paper_positions: Dict[str, Position] = {}
        self._paper_orders: Dict[str, OrderResult] = {}
        self._paper_prices: Dict[str, float] = {}
        self._paper_book_path = Path(
            getattr(settings, "paper_book_path", None)
            or (Path(__file__).resolve().parents[2] / "data" / "paper_book.json")
        )
        self._live_bankroll_cache: Optional[Dict[str, Any]] = None
        self._live_bankroll_cache_ts: float = 0.0
        self._live_bankroll_ttl_s: float = 60.0
        self._last_tick_mono: float = 0.0
        self._last_l2_mono: float = 0.0
        self._reconnect_backoff = ExponentialBackoff(base=1.0, factor=2.0, max_delay=60.0)
        self._reconnect_lock = asyncio.Lock()
        self._reconnect_in_progress = False
        self._nonce_lock = asyncio.Lock()
        self._last_nonce = 0
        self._load_paper_book()

    # ------------------------------------------------------------------ helpers

    def clear_stream_buffers(self) -> None:
        self._live_bankroll_cache = None
        self._live_bankroll_cache_ts = 0.0
        self._paper_prices.clear()
        logger.debug("KrakenBroker stream buffers cleared")

    def _require_credentials(self) -> None:
        if not self.settings.kraken_api_key or not self.settings.kraken_api_secret:
            raise RuntimeError(
                "Kraken credentials missing. Set KRAKEN_API_KEY and "
                "KRAKEN_API_SECRET, or use --dry-run / BROKER=mock / "
                "PAPER_TRADING_MODE with simulated book."
            )

    def _base_url(self) -> str:
        return (self.settings.kraken_base_url or _API_HOST_DEFAULT).rstrip("/")

    def _ws_url(self) -> str:
        return getattr(self.settings, "kraken_ws_url", None) or _WS_URL_DEFAULT

    async def _next_nonce(self) -> str:
        async with self._nonce_lock:
            candidate = int(time.time() * 1_000_000)
            if candidate <= self._last_nonce:
                candidate = self._last_nonce + 1
            self._last_nonce = candidate
            return str(candidate)

    # -------------------------------------------------------------- paper book I/O

    def _load_paper_book(self) -> None:
        if not self.settings.paper_trading_mode:
            return
        path = self._paper_book_path
        if not path.exists():
            logger.info("No paper book at %s — starting flat cash=%.2f", path, self._paper_cash)
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("paper book load failed (%s) — starting flat", exc)
            return
        try:
            cash = float(raw.get("cash", self._paper_cash))
            self._paper_cash = cash
            self._paper_equity = float(raw.get("equity", cash))
            day_start = raw.get("day_start_equity")
            if day_start is not None:
                self._day_start_equity = float(day_start)
            positions: Dict[str, Position] = {}
            for sym, pdata in (raw.get("positions") or {}).items():
                product = to_standard_symbol(str(sym))
                opened = pdata.get("opened_at")
                opened_at = _parse_ts(opened) if opened else None
                qty = float(pdata.get("qty") or 0)
                if qty <= 1e-12:
                    continue
                avg = float(pdata.get("avg_entry_price") or 0)
                positions[product] = Position(
                    symbol=product,
                    qty=qty,
                    avg_entry_price=avg,
                    market_value=float(pdata.get("market_value") or qty * avg),
                    unrealized_pl=float(pdata.get("unrealized_pl") or 0),
                    side=str(pdata.get("side") or "long"),
                    stop_loss=pdata.get("stop_loss"),
                    take_profit=pdata.get("take_profit"),
                    take_profit_1=pdata.get("take_profit_1"),
                    trail_distance=pdata.get("trail_distance"),
                    trail_high_water=pdata.get("trail_high_water"),
                    opened_at=opened_at,
                    tp1_done=bool(pdata.get("tp1_done") or False),
                    initial_qty=pdata.get("initial_qty"),
                    entry_reason=pdata.get("entry_reason"),
                )
                if avg > 0:
                    self._paper_prices[product] = float(pdata.get("last_price") or avg)
            self._paper_positions = positions
            logger.info(
                "Loaded paper book %s cash=%.2f positions=%s",
                path,
                self._paper_cash,
                list(self._paper_positions.keys()),
            )
        except Exception as exc:
            logger.warning("paper book parse failed (%s) — keeping defaults", exc)

    def _save_paper_book(self) -> None:
        if not self.settings.paper_trading_mode:
            return
        path = self._paper_book_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            pos_value = sum(p.market_value for p in self._paper_positions.values())
            self._paper_equity = self._paper_cash + pos_value
            payload: Dict[str, Any] = {
                "cash": self._paper_cash,
                "equity": self._paper_equity,
                "day_start_equity": self._day_start_equity,
                "updated_at": utcnow().isoformat(),
                "positions": {},
            }
            for sym, pos in self._paper_positions.items():
                payload["positions"][sym] = {
                    "qty": pos.qty,
                    "avg_entry_price": pos.avg_entry_price,
                    "market_value": pos.market_value,
                    "unrealized_pl": pos.unrealized_pl,
                    "side": pos.side,
                    "stop_loss": pos.stop_loss,
                    "take_profit": pos.take_profit,
                    "take_profit_1": getattr(pos, "take_profit_1", None),
                    "trail_distance": pos.trail_distance,
                    "trail_high_water": pos.trail_high_water,
                    "opened_at": pos.opened_at.isoformat() if pos.opened_at else None,
                    "tp1_done": bool(getattr(pos, "tp1_done", False)),
                    "initial_qty": getattr(pos, "initial_qty", None),
                    "entry_reason": getattr(pos, "entry_reason", None),
                    "last_price": self._paper_prices.get(sym),
                }
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception as exc:
            logger.warning("paper book save failed: %s", exc)

    def update_position_brackets(
        self,
        symbol: str,
        *,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        take_profit_1: Optional[float] = None,
        trail_distance: Optional[float] = None,
        trail_high_water: Optional[float] = None,
        mark_price: Optional[float] = None,
        tp1_done: Optional[bool] = None,
        initial_qty: Optional[float] = None,
        entry_reason: Optional[str] = None,
        update_trail_hwm: bool = True,
    ) -> Optional[Position]:
        product = to_standard_symbol(symbol)
        pos = self._paper_positions.get(product)
        if not pos:
            return None
        if stop_loss is not None:
            pos.stop_loss = stop_loss
        if take_profit is not None:
            pos.take_profit = take_profit
        if take_profit_1 is not None:
            pos.take_profit_1 = take_profit_1
        sweet = bool(getattr(self.settings, "is_sweet_spot", False))
        if sweet:
            pos.trail_distance = None
            update_trail_hwm = False
        elif trail_distance is not None:
            pos.trail_distance = trail_distance
        if trail_high_water is not None:
            pos.trail_high_water = trail_high_water
        if tp1_done is not None:
            pos.tp1_done = bool(tp1_done)
        if initial_qty is not None:
            pos.initial_qty = float(initial_qty)
        if entry_reason is not None:
            pos.entry_reason = str(entry_reason)
        if mark_price is not None and mark_price > 0:
            self._paper_prices[product] = mark_price
            pos.market_value = pos.qty * mark_price
            pos.unrealized_pl = (mark_price - pos.avg_entry_price) * pos.qty
            if update_trail_hwm and not sweet:
                if pos.trail_high_water is None or mark_price > pos.trail_high_water:
                    pos.trail_high_water = mark_price
        self._save_paper_book()
        return pos

    def reset_paper_book(self, cash: Optional[float] = None) -> None:
        self._paper_cash = float(cash if cash is not None else self.settings.account_equity)
        self._paper_equity = self._paper_cash
        self._paper_positions.clear()
        self._day_start_equity = self._paper_cash
        self._save_paper_book()
        logger.warning("Paper book RESET to cash=%.2f flat", self._paper_cash)

    # ---------------------------------------------------------- market-data heartbeat

    def _stale_tick_limit(self) -> float:
        return float(getattr(self.settings, "stale_tick_seconds", 15.0) or 15.0)

    def _touch_tick(self) -> None:
        self._last_tick_mono = time.monotonic()

    def _touch_l2(self) -> None:
        now = time.monotonic()
        self._last_l2_mono = now
        self._last_tick_mono = now

    @property
    def last_tick_age_seconds(self) -> Optional[float]:
        if self._last_tick_mono <= 0:
            return None
        return max(0.0, time.monotonic() - self._last_tick_mono)

    @property
    def last_l2_age_seconds(self) -> Optional[float]:
        if self._last_l2_mono <= 0:
            return None
        return max(0.0, time.monotonic() - self._last_l2_mono)

    def is_market_data_stale(self) -> bool:
        age = self.last_tick_age_seconds
        if age is None:
            return False
        return age > self._stale_tick_limit()

    async def _close_api_surfaces(self) -> None:
        if self._session is not None:
            try:
                if not self._session.closed:
                    await self._session.close()
            except Exception as exc:
                logger.debug("session close on reconnect: %s", exc)
            self._session = None

    async def _init_api_client(self) -> None:
        has_creds = bool(self.settings.kraken_api_key and self.settings.kraken_api_secret)
        if not has_creds:
            logger.info("No Kraken credentials — market data stays synthetic/offline")
            return
        timeout = aiohttp.ClientTimeout(total=30)
        self._session = aiohttp.ClientSession(timeout=timeout)
        logger.info("Kraken REST session (re)initialized")

    async def force_reconnect_market_data(self, *, reason: str = "stale") -> None:
        async with self._reconnect_lock:
            if self._reconnect_in_progress:
                return
            self._reconnect_in_progress = True
            try:
                age = self.last_tick_age_seconds
                logger.warning(
                    "Market data %s (last_tick_age=%s) — closing session and reconnecting",
                    reason,
                    f"{age:.1f}s" if age is not None else "n/a",
                )
                await self._close_api_surfaces()
                delay = self._reconnect_backoff.next_delay()
                logger.info("Reconnect backoff sleep %.1fs", delay)
                await asyncio.sleep(delay)
                await self._init_api_client()
                try:
                    syms = list(getattr(self.settings, "symbol_list", None) or [])
                    if syms:
                        await self.get_quote(syms[0])
                    self._reconnect_backoff.reset()
                    logger.info(
                        "Market data reconnect OK (last_tick_age=%.1fs)",
                        self.last_tick_age_seconds or -1.0,
                    )
                except Exception as exc:
                    logger.warning("Reconnect probe failed: %s", exc)
            finally:
                self._reconnect_in_progress = False

    async def ensure_market_data_fresh(self) -> bool:
        if self.last_tick_age_seconds is None:
            return True
        if not self.is_market_data_stale():
            return True
        await self.force_reconnect_market_data(reason="stale_tick")
        return not self.is_market_data_stale()

    # ------------------------------------------------------------------ connect

    async def connect(self) -> None:
        paper = bool(self.settings.paper_trading_mode)
        has_creds = bool(self.settings.kraken_api_key and self.settings.kraken_api_secret)

        if paper and not has_creds:
            self._connected = True
            self._day_start_equity = self._paper_equity
            logger.info(
                "Kraken connected in PAPER mode without credentials "
                "(simulated account equity=%.2f). No live orders will be sent.",
                self._paper_equity,
            )
            return

        self._require_credentials()
        await self._init_api_client()

        if paper:
            self._connected = True
            self._day_start_equity = self._paper_equity
            logger.info(
                "Kraken PAPER mode ON — live AddOrder/Cancel are gated; "
                "orders will be simulated. Market data may still hit the API."
            )
            logger.info(
                "PAPER book stays on ACCOUNT_EQUITY=%.2f (live balances not used for sizing)",
                self._paper_equity,
            )
            return

        logger.critical(
            "PAPER_TRADING_MODE is False — live Kraken order submits are enabled. "
            "Proceed only if intentional."
        )
        acct = await self.get_account()
        self._day_start_equity = acct.equity
        self._connected = True
        logger.info("Kraken LIVE connected (equity=%.2f)", acct.equity)

    async def disconnect(self) -> None:
        self._connected = False
        await self._close_api_surfaces()

    # ------------------------------------------------------------------ REST

    def _raise_kraken_errors(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        errors = payload.get("error") or []
        if not errors:
            return
        msg = "; ".join(str(e) for e in errors)
        low = msg.lower()
        if "rate" in low or "exceeded" in low:
            raise RateLimitError(f"Kraken: {msg}")
        raise RuntimeError(f"Kraken: {msg}")

    async def _http(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        if not self._session:
            raise RuntimeError("KrakenBroker REST session not connected")

        async def _do() -> Any:
            assert self._session is not None
            async with self._session.request(
                method,
                url,
                params=params,
                data=data,
                headers=headers,
            ) as resp:
                text = await resp.text()
                if is_rate_limit_status(resp.status):
                    retry_after = resp.headers.get("Retry-After")
                    raise RateLimitError(
                        f"Kraken 429: {text[:200]}",
                        retry_after=float(retry_after) if retry_after else None,
                    )
                if resp.status >= 500:
                    raise RetryableError(f"Kraken {resp.status}: {text[:200]}")
                if resp.status >= 400:
                    raise RuntimeError(f"Kraken {resp.status}: {text[:400]}")
                if not text:
                    return None
                return json.loads(text)

        return await retry_async(_do, max_attempts=5, backoff=ExponentialBackoff())

    async def _public(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """GET ``/0/public/{endpoint}``. Overridable in tests."""
        url = f"{self._base_url()}{_PUBLIC_PREFIX}{endpoint}"
        payload = await self._http("GET", url, params=params)
        self._raise_kraken_errors(payload)
        return (payload or {}).get("result")

    async def _private(
        self,
        endpoint: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """POST ``/0/private/{endpoint}`` with HMAC API-Sign. Overridable in tests."""
        body = dict(data or {})
        body["nonce"] = await self._next_nonce()
        path = f"{_PRIVATE_PREFIX}{endpoint}"
        sign = sign_kraken_request(path, body, self.settings.kraken_api_secret)
        headers = {
            "API-Key": self.settings.kraken_api_key,
            "API-Sign": sign,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        url = f"{self._base_url()}{path}"
        payload = await self._http("POST", url, data=body, headers=headers)
        self._raise_kraken_errors(payload)
        return (payload or {}).get("result")

    # ------------------------------------------------------------------ account / positions

    async def _fetch_live_account(self) -> AccountState:
        balances = await self._private("Balance") or {}
        cash = 0.0
        equity = 0.0
        for asset, raw in balances.items():
            try:
                qty = float(raw or 0)
            except (TypeError, ValueError):
                qty = 0.0
            if qty == 0:
                continue
            iso = _from_kraken_asset(str(asset))
            if iso in ("USD", "USDT", "USDC"):
                cash += qty
                equity += qty
                continue
            product = f"{iso}-USD"
            px = self._paper_prices.get(product)
            if px is None:
                try:
                    quote = await self.get_quote(product)
                    px = quote.mid
                    self._paper_prices[product] = px
                except Exception:
                    px = 0.0
            equity += qty * (px or 0.0)
        if equity <= 0 and cash > 0:
            equity = cash
        return AccountState(
            equity=equity or cash,
            cash=cash,
            buying_power=cash,
            day_pl=0.0,
            day_pl_pct=0.0,
            paper=self.settings.paper_trading_mode,
        )

    async def get_live_bankroll(self, *, force: bool = False) -> Dict[str, float]:
        now = time.monotonic()
        if (
            not force
            and self._live_bankroll_cache is not None
            and (now - self._live_bankroll_cache_ts) < self._live_bankroll_ttl_s
        ):
            return dict(self._live_bankroll_cache)

        has_creds = bool(self.settings.kraken_api_key and self.settings.kraken_api_secret)
        if not has_creds or self._session is None:
            out = {"cash": 0.0, "cash_usd": 0.0, "cash_usdc": 0.0, "equity": 0.0}
            self._live_bankroll_cache = out
            self._live_bankroll_cache_ts = now
            return dict(out)

        try:
            live = await self._fetch_live_account()
            out = {
                "cash": float(live.cash),
                "cash_usd": float(live.cash),
                "cash_usdc": 0.0,
                "equity": float(live.equity or live.cash),
            }
        except Exception as exc:
            logger.warning("get_live_bankroll failed: %s", exc)
            out = {"cash": 0.0, "cash_usd": 0.0, "cash_usdc": 0.0, "equity": 0.0}

        self._live_bankroll_cache = out
        self._live_bankroll_cache_ts = now
        return dict(out)

    async def get_account(self) -> AccountState:
        if self.settings.paper_trading_mode:
            pos_value = sum(p.market_value for p in self._paper_positions.values())
            self._paper_equity = self._paper_cash + pos_value
            start = self._day_start_equity or self._paper_equity
            day_pl = self._paper_equity - start
            return AccountState(
                equity=self._paper_equity,
                cash=self._paper_cash,
                buying_power=self._paper_cash,
                day_pl=day_pl,
                day_pl_pct=(day_pl / start) if start else 0.0,
                paper=True,
            )
        acct = await self._fetch_live_account()
        if self._day_start_equity is None:
            self._day_start_equity = acct.equity
        day_pl = acct.equity - (self._day_start_equity or acct.equity)
        start = self._day_start_equity or acct.equity or 1.0
        return AccountState(
            equity=acct.equity,
            cash=acct.cash,
            buying_power=acct.buying_power,
            day_pl=day_pl,
            day_pl_pct=day_pl / start if start else 0.0,
            paper=False,
        )

    async def get_positions(self) -> List[Position]:
        if self.settings.paper_trading_mode:
            for sym, pos in list(self._paper_positions.items()):
                px = self._paper_prices.get(sym, pos.avg_entry_price)
                pos.market_value = pos.qty * px
                pos.unrealized_pl = (px - pos.avg_entry_price) * pos.qty
            return list(self._paper_positions.values())

        balances = await self._private("Balance") or {}
        out: List[Position] = []
        for asset, raw in balances.items():
            try:
                qty = float(raw or 0)
            except (TypeError, ValueError):
                qty = 0.0
            iso = _from_kraken_asset(str(asset))
            if iso in ("USD", "USDT", "USDC") or qty <= 0:
                continue
            product = f"{iso}-USD"
            try:
                q = await self.get_quote(product)
                px = q.mid
            except Exception:
                px = 0.0
            out.append(
                Position(
                    symbol=product,
                    qty=qty,
                    avg_entry_price=px,
                    market_value=qty * px,
                    unrealized_pl=0.0,
                    side="long",
                )
            )
        return out

    async def get_position(self, symbol: str) -> Optional[Position]:
        product = to_standard_symbol(symbol)
        for p in await self.get_positions():
            if p.symbol == product or p.symbol == symbol.upper():
                return p
        return None

    # ------------------------------------------------------------------ market data

    def _synthetic_bars(self, product: str, limit: int = 100) -> List[Bar]:
        import random

        rng = random.Random(hash(product) & 0xFFFFFFFF)
        price = self._paper_prices.get(product, 100.0)
        now = utcnow()
        bars: List[Bar] = []
        for i in range(limit, 0, -1):
            ret = rng.gauss(0, 0.002)
            o = price
            c = max(0.01, o * (1 + ret))
            h = max(o, c) * (1 + abs(rng.gauss(0, 0.001)))
            lo = min(o, c) * (1 - abs(rng.gauss(0, 0.001)))
            bars.append(
                Bar(
                    symbol=product,
                    timestamp=now - timedelta(minutes=i),
                    open=round(o, 6),
                    high=round(h, 6),
                    low=round(lo, 6),
                    close=round(c, 6),
                    volume=float(rng.randint(10, 500)),
                    vwap=round((h + lo + c) / 3, 6),
                )
            )
            price = c
        self._paper_prices[product] = price
        return bars

    async def get_bars(
        self,
        symbol: str,
        *,
        timeframe: str = "1Min",
        limit: int = 100,
    ) -> List[Bar]:
        product = to_standard_symbol(symbol)
        if self.settings.paper_trading_mode and self._session is None:
            bars = self._synthetic_bars(product, limit=limit)
            self._touch_tick()
            return bars

        pair = to_kraken_pair(product)
        interval = _TIMEFRAME_TO_INTERVAL.get(timeframe, 1)
        result = await self._public("OHLC", {"pair": pair, "interval": interval}) or {}
        raw_rows = _first_numeric_list(result) or []
        bars: List[Bar] = []
        for row in raw_rows:
            # [time, open, high, low, close, vwap, volume, count]
            if not isinstance(row, (list, tuple)) or len(row) < 7:
                continue
            close = float(row[4] or 0)
            bars.append(
                Bar(
                    symbol=product,
                    timestamp=_parse_ts(row[0]),
                    open=float(row[1] or close),
                    high=float(row[2] or close),
                    low=float(row[3] or close),
                    close=close,
                    volume=float(row[6] or 0),
                    vwap=float(row[5] or 0) or None,
                )
            )
        bars.sort(key=lambda b: b.timestamp)
        if bars:
            self._paper_prices[product] = bars[-1].close
            self._touch_tick()
        return bars[-limit:] if limit else bars

    async def get_quote(self, symbol: str) -> Quote:
        product = to_standard_symbol(symbol)
        if self.settings.paper_trading_mode and self._session is None:
            mid = self._paper_prices.get(product, 100.0)
            spread = mid * 0.0005
            self._touch_tick()
            return Quote(
                symbol=product,
                timestamp=utcnow(),
                bid=round(mid - spread / 2, 6),
                ask=round(mid + spread / 2, 6),
                bid_size=0.0,
                ask_size=0.0,
            )

        pair = to_kraken_pair(product)
        result = await self._public("Ticker", {"pair": pair}) or {}
        book = _first_numeric_list(result) or {}
        bid = ask = 0.0
        if isinstance(book, dict):
            bids = book.get("b") or []
            asks = book.get("a") or []
            if bids:
                bid = float(bids[0] or 0)
            if asks:
                ask = float(asks[0] or 0)
        if bid <= 0 and ask <= 0:
            bars = await self.get_bars(product, limit=1)
            if bars:
                mid = bars[-1].close
                bid = ask = mid
        mid = (bid + ask) / 2 if (bid and ask) else (bid or ask)
        if mid:
            self._paper_prices[product] = mid
        self._touch_tick()
        return Quote(
            symbol=product,
            timestamp=utcnow(),
            bid=bid or mid,
            ask=ask or mid,
            bid_size=0.0,
            ask_size=0.0,
        )

    async def get_l2_book(
        self,
        symbol: str,
        *,
        limit: int = 50,
    ) -> Dict[str, Any]:
        product = to_standard_symbol(symbol)
        empty: Dict[str, Any] = {"product_id": product, "bids": [], "asks": [], "mid": None}
        try:
            if self.settings.paper_trading_mode and self._session is None:
                mid = float(self._paper_prices.get(product, 100.0))
                spread = mid * 0.0004
                levels = list(range(1, min(limit, 10) + 1))
                bids = [
                    {"price": round(mid - spread * i, 8), "size": float(10 * (11 - i))}
                    for i in levels
                ]
                asks = [
                    {"price": round(mid + spread * i, 8), "size": float(8 * (11 - i))}
                    for i in levels
                ]
                self._touch_l2()
                return {"product_id": product, "bids": bids, "asks": asks, "mid": mid}

            pair = to_kraken_pair(product)
            result = await self._public("Depth", {"pair": pair, "count": limit}) or {}
            book = _first_numeric_list(result) or {}
            raw_bids = list((book or {}).get("bids") or [])
            raw_asks = list((book or {}).get("asks") or [])

            def _norm(levels: List[Any]) -> List[Dict[str, float]]:
                out: List[Dict[str, float]] = []
                for lvl in levels:
                    if isinstance(lvl, dict):
                        try:
                            px = float(lvl.get("price") or 0)
                            sz = float(lvl.get("size") or lvl.get("qty") or 0)
                        except (TypeError, ValueError):
                            continue
                    elif isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
                        try:
                            px = float(lvl[0])
                            sz = float(lvl[1])
                        except (TypeError, ValueError):
                            continue
                    else:
                        continue
                    if px > 0 and sz > 0:
                        out.append({"price": px, "size": sz})
                return out

            bids = _norm(raw_bids)
            asks = _norm(raw_asks)
            mid = None
            if bids and asks:
                mid = (bids[0]["price"] + asks[0]["price"]) / 2.0
                self._paper_prices[product] = mid
            if bids or asks:
                self._touch_l2()
            return {"product_id": product, "bids": bids, "asks": asks, "mid": mid}
        except Exception as exc:
            logger.debug("get_l2_book(%s) failed: %s", product, exc)
            return empty

    async def get_l2_imbalance(
        self,
        symbol: str,
        *,
        band_pct: float = 0.005,
        limit: int = 50,
    ) -> Optional[float]:
        from trading_bot.utils.indicators import l2_depth_imbalance

        book = await self.get_l2_book(symbol, limit=limit)
        return l2_depth_imbalance(
            book.get("bids") or [],
            book.get("asks") or [],
            mid=book.get("mid"),
            band_pct=band_pct,
        )

    # ------------------------------------------------------------------ orders (paper-gated)

    def _reject_market(self, order: OrderRequest, *, paper: bool, prefix: str) -> OrderResult:
        broker_id = f"{prefix}{uuid4().hex[:8]}"
        result = OrderResult(
            client_order_id=order.client_order_id,
            broker_order_id=broker_id,
            status=OrderStatus.REJECTED,
            symbol=to_standard_symbol(order.symbol),
            side=order.side,
            qty=order.qty,
            message="market_orders_disabled",
            paper=paper,
        )
        if paper:
            self._paper_orders[broker_id] = result
        logger.warning(
            "Reject market/non-limit under POST_ONLY: %s %s type=%s",
            order.side.value,
            order.symbol,
            getattr(getattr(order, "order_type", None), "value", order.order_type),
        )
        return result

    def _simulate_fill(self, order: OrderRequest) -> OrderResult:
        if _wants_post_only(order, self.settings):
            if getattr(order, "order_type", None) != OrderType.LIMIT or order.limit_price is None:
                return self._reject_market(order, paper=True, prefix="kr-paper-reject-")
        product = to_standard_symbol(order.symbol)
        px = order.limit_price or self._paper_prices.get(product, 100.0)
        cost = px * order.qty
        broker_id = f"kr-paper-{uuid4().hex[:12]}"

        is_taker = not _wants_post_only(order, self.settings)
        if order.side == OrderSide.BUY:
            fee = _apply_trade_fee(self.settings, cost, is_taker=is_taker)
            if cost + fee > self._paper_cash + 1e-6:
                result = OrderResult(
                    client_order_id=order.client_order_id,
                    broker_order_id=broker_id,
                    status=OrderStatus.REJECTED,
                    symbol=product,
                    side=order.side,
                    qty=order.qty,
                    message="paper: insufficient cash",
                    paper=True,
                )
                self._paper_orders[broker_id] = result
                return result
            self._paper_cash -= cost + fee
            existing = self._paper_positions.get(product)
            if existing:
                new_qty = existing.qty + order.qty
                existing.avg_entry_price = (
                    existing.avg_entry_price * existing.qty + cost
                ) / new_qty
                existing.qty = new_qty
                existing.market_value = new_qty * px
                if order.stop_loss is not None:
                    existing.stop_loss = order.stop_loss
                if order.take_profit is not None:
                    existing.take_profit = order.take_profit
                if existing.trail_high_water is None or px > existing.trail_high_water:
                    existing.trail_high_water = px
            else:
                sweet = bool(getattr(self.settings, "is_sweet_spot", False))
                self._paper_positions[product] = Position(
                    symbol=product,
                    qty=order.qty,
                    avg_entry_price=px,
                    market_value=cost,
                    side="long",
                    stop_loss=order.stop_loss,
                    take_profit=order.take_profit,
                    trail_distance=None if sweet else None,
                    trail_high_water=None if sweet else px,
                    opened_at=utcnow(),
                    tp1_done=False,
                    initial_qty=order.qty,
                )
        else:
            existing = self._paper_positions.get(product)
            if not existing or existing.qty < order.qty - 1e-9:
                result = OrderResult(
                    client_order_id=order.client_order_id,
                    broker_order_id=broker_id,
                    status=OrderStatus.REJECTED,
                    symbol=product,
                    side=order.side,
                    qty=order.qty,
                    message="paper: insufficient position",
                    paper=True,
                )
                self._paper_orders[broker_id] = result
                return result
            proceeds = cost
            fee = _apply_trade_fee(self.settings, proceeds, is_taker=is_taker)
            self._paper_cash += proceeds - fee
            existing.qty -= order.qty
            if existing.qty <= 1e-9:
                del self._paper_positions[product]
            else:
                existing.market_value = existing.qty * px

        self._paper_prices[product] = px
        result = OrderResult(
            client_order_id=order.client_order_id,
            broker_order_id=broker_id,
            status=OrderStatus.FILLED,
            symbol=product,
            side=order.side,
            qty=order.qty,
            filled_qty=order.qty,
            avg_fill_price=px,
            message="paper simulated fill (no live Kraken submit)",
            paper=True,
        )
        self._paper_orders[broker_id] = result
        self._save_paper_book()
        return result

    async def submit_order(self, order: OrderRequest) -> OrderResult:
        if _wants_post_only(order, self.settings):
            if getattr(order, "order_type", None) != OrderType.LIMIT or order.limit_price is None:
                paper = bool(self.settings.paper_trading_mode)
                prefix = "kr-paper-reject-" if paper else "kr-reject-"
                return self._reject_market(order, paper=paper, prefix=prefix)

        if self.settings.paper_trading_mode:
            logger.info(
                "PAPER gate: would submit Kraken order %s %s qty=%s limit=%s pair=%s "
                "(simulated fill only; oflags=post if POST_ONLY)",
                order.side.value,
                to_standard_symbol(order.symbol),
                order.qty,
                order.limit_price,
                to_kraken_pair(order.symbol),
            )
            return self._simulate_fill(order)

        payload = build_add_order_payload(order, post_only=_wants_post_only(order, self.settings))
        result = await self._private("AddOrder", payload) or {}
        txids = result.get("txid") or []
        order_id = str(txids[0]) if txids else None
        if not order_id:
            return OrderResult(
                client_order_id=order.client_order_id,
                broker_order_id=None,
                status=OrderStatus.REJECTED,
                symbol=to_standard_symbol(order.symbol),
                side=order.side,
                qty=order.qty,
                message=str(result)[:400],
                paper=False,
            )
        return OrderResult(
            client_order_id=order.client_order_id,
            broker_order_id=order_id,
            status=OrderStatus.SUBMITTED,
            symbol=to_standard_symbol(order.symbol),
            side=order.side,
            qty=order.qty,
            message="live submitted",
            paper=False,
        )

    async def cancel_order(self, broker_order_id: str) -> bool:
        if self.settings.paper_trading_mode:
            order = self._paper_orders.get(broker_order_id)
            if not order:
                logger.info("PAPER gate: cancel %s (unknown / no-op)", broker_order_id)
                return False
            if order.status in (OrderStatus.FILLED, OrderStatus.CANCELLED):
                return False
            order.status = OrderStatus.CANCELLED
            logger.info("PAPER gate: cancelled simulated order %s", broker_order_id)
            return True

        result = await self._private("CancelOrder", {"txid": broker_order_id}) or {}
        return int(result.get("count") or 0) > 0

    async def cancel_all_orders(self) -> int:
        if self.settings.paper_trading_mode:
            n = 0
            for o in self._paper_orders.values():
                if o.status in (
                    OrderStatus.SUBMITTED,
                    OrderStatus.PENDING,
                    OrderStatus.PARTIAL,
                ):
                    o.status = OrderStatus.CANCELLED
                    n += 1
            logger.info("PAPER gate: cancel_all simulated open orders n=%s", n)
            return n

        result = await self._private("CancelAll") or {}
        return int(result.get("count") or 0)

    def _order_from_kraken(self, broker_order_id: str, raw: Dict[str, Any]) -> OrderResult:
        descr = raw.get("descr") or {}
        status_raw = str(raw.get("status") or "pending").lower()
        side_raw = str(descr.get("type") or "buy").upper()
        pair = str(descr.get("pair") or raw.get("pair") or "")
        symbol = from_kraken_pair(pair) if pair else ""
        qty = float(raw.get("vol") or 0)
        filled = float(raw.get("vol_exec") or 0)
        avg = raw.get("price")
        try:
            avg_px = float(avg) if avg not in (None, "", "0", 0) else None
        except (TypeError, ValueError):
            avg_px = None
        return OrderResult(
            client_order_id=str(raw.get("cl_ord_id") or raw.get("userref") or ""),
            broker_order_id=str(broker_order_id),
            status=_STATUS_MAP.get(status_raw, OrderStatus.PENDING),
            symbol=symbol,
            side=OrderSide.BUY if side_raw == "BUY" else OrderSide.SELL,
            qty=qty,
            filled_qty=filled,
            avg_fill_price=avg_px,
            message=status_raw,
            paper=False,
        )

    async def get_order(self, broker_order_id: str) -> OrderResult:
        if self.settings.paper_trading_mode:
            if broker_order_id in self._paper_orders:
                return self._paper_orders[broker_order_id]
            raise KeyError(broker_order_id)

        result = await self._private("QueryOrders", {"txid": broker_order_id}) or {}
        raw = result.get(broker_order_id)
        if raw is None and result:
            raw = next(iter(result.values()))
        if not isinstance(raw, dict):
            raise KeyError(broker_order_id)
        return self._order_from_kraken(broker_order_id, raw)

    async def liquidate_all(self) -> List[OrderResult]:
        if self.settings.paper_trading_mode:
            results: List[OrderResult] = []
            for pos in list(await self.get_positions()):
                _sym = Settings.normalize_symbol(pos.symbol)
                if _sym not in HARD_SYMBOL_ALLOWLIST:
                    logger.info("Skipping non-allowlist holding: %s", _sym)
                    continue
                req = OrderRequest(
                    symbol=pos.symbol,
                    side=OrderSide.SELL,
                    qty=pos.qty,
                    order_type=OrderType.LIMIT,
                    limit_price=self._paper_prices.get(pos.symbol, pos.avg_entry_price),
                    paper=True,
                    post_only=True,
                )
                results.append(await self.submit_order(req))
            logger.info("PAPER gate: liquidate_all simulated n=%s", len(results))
            return results

        results: List[OrderResult] = []
        for pos in await self.get_positions():
            _sym = Settings.normalize_symbol(pos.symbol)
            if _sym not in HARD_SYMBOL_ALLOWLIST:
                logger.info("Skipping non-allowlist holding: %s", _sym)
                continue
            px = self._paper_prices.get(pos.symbol) or pos.avg_entry_price
            req = OrderRequest(
                symbol=pos.symbol,
                side=OrderSide.SELL,
                qty=pos.qty,
                order_type=OrderType.LIMIT,
                limit_price=px,
                paper=False,
                post_only=bool(getattr(self.settings, "post_only", True)),
            )
            results.append(await self.submit_order(req))
        return results

    async def stream_bars(
        self,
        symbols: List[str],
        on_bar: Callable[[Bar], None],
    ) -> None:
        """REST-polled bars (same default as Coinbase). Public WS is optional."""
        products = [to_standard_symbol(s) for s in symbols]
        backoff = ExponentialBackoff(base=1.0, max_delay=60.0)
        while self._connected:
            try:
                for product in products:
                    bars = await self.get_bars(product, limit=1)
                    if bars:
                        on_bar(bars[-1])
                backoff.reset()
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                delay = backoff.next_delay()
                logger.warning("Kraken bar poll error (%s); retry in %.1fs", exc, delay)
                await asyncio.sleep(delay)

    async def stream_quotes(
        self,
        symbols: List[str],
        on_quote: Callable[[Quote], None],
    ) -> None:
        """Public Kraken WS v1 ticker using ``XBT/USD`` wsnames.

        Falls back to REST quote polling if ``websockets`` is unavailable.
        """
        try:
            import websockets
        except ImportError:  # pragma: no cover - optional
            backoff = ExponentialBackoff(base=1.0, max_delay=60.0)
            while self._connected:
                try:
                    for symbol in symbols:
                        on_quote(await self.get_quote(symbol))
                    backoff.reset()
                    await asyncio.sleep(2.0)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    delay = backoff.next_delay()
                    logger.warning("Kraken quote poll error (%s); retry in %.1fs", exc, delay)
                    await asyncio.sleep(delay)
            return

        payload = ws_subscribe_message(symbols, channel="ticker")
        backoff = ExponentialBackoff(base=1.0, max_delay=60.0)
        while self._connected:
            try:
                async with websockets.connect(self._ws_url()) as ws:
                    await ws.send(json.dumps(payload))
                    backoff.reset()
                    async for raw in ws:
                        if not self._connected:
                            break
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(msg, list) or len(msg) < 4:
                            continue
                        # [channelID, {c/a/b/...}, "ticker", "XBT/USD"]
                        data = msg[1] if isinstance(msg[1], dict) else {}
                        pair = str(msg[-1])
                        bid_raw = data.get("b") or [0]
                        ask_raw = data.get("a") or [0]
                        bid = float(bid_raw[0] if isinstance(bid_raw, list) else bid_raw or 0)
                        ask = float(ask_raw[0] if isinstance(ask_raw, list) else ask_raw or 0)
                        if bid <= 0 and ask <= 0:
                            continue
                        product = from_kraken_pair(pair)
                        mid = (bid + ask) / 2 if (bid and ask) else (bid or ask)
                        self._paper_prices[product] = mid
                        self._touch_tick()
                        on_quote(
                            Quote(
                                symbol=product,
                                timestamp=utcnow(),
                                bid=bid or mid,
                                ask=ask or mid,
                            )
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                delay = backoff.next_delay()
                logger.warning("Kraken WS quote error (%s); retry in %.1fs", exc, delay)
                await asyncio.sleep(delay)
