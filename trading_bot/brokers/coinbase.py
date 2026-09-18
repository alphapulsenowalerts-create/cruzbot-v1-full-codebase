"""Coinbase Advanced Trade (spot) broker adapter.

Uses official ``coinbase-advanced-py`` when installed; otherwise falls back to
CDP JWT-signed REST against documented Advanced Trade endpoints (TODOs marked).

Coinbase has no true retail paper-trading portfolio. When ``PAPER_TRADING_MODE``
is True, this adapter **never** calls live create/cancel/liquidate endpoints —
it logs intended orders and returns simulated fills. Market-data reads may still
use the public/authenticated API when credentials are present.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
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

# Official package (optional at import time for test envs without the dep).
try:
    from coinbase.rest import RESTClient as _CoinbaseRESTClient

    _HAS_SDK = True
except ImportError:  # pragma: no cover - exercised when SDK absent
    _CoinbaseRESTClient = None  # type: ignore[assignment, misc]
    _HAS_SDK = False

_API_HOST = "api.coinbase.com"
_API_PREFIX = "/api/v3/brokerage"
# Static Advanced Trade sandbox (mocked responses, no auth) — not a fill simulator.
_SANDBOX_BASE = "https://api-sandbox.coinbase.com/api/v3/brokerage"

_TIMEFRAME_TO_GRANULARITY = {
    "1Min": "ONE_MINUTE",
    "1MIN": "ONE_MINUTE",
    "5Min": "FIVE_MINUTE",
    "5MIN": "FIVE_MINUTE",
    "15Min": "FIFTEEN_MINUTE",
    "15MIN": "FIFTEEN_MINUTE",
    "1Hour": "ONE_HOUR",
    "1H": "ONE_HOUR",
    "4Hour": "FOUR_HOUR",
    "4H": "FOUR_HOUR",
    "4Hr": "FOUR_HOUR",
    "1Day": "ONE_DAY",
    "1D": "ONE_DAY",
}

_STATUS_MAP = {
    "PENDING": OrderStatus.PENDING,
    "OPEN": OrderStatus.SUBMITTED,
    "FILLED": OrderStatus.FILLED,
    "CANCELLED": OrderStatus.CANCELLED,
    "EXPIRED": OrderStatus.EXPIRED,
    "FAILED": OrderStatus.REJECTED,
    "UNKNOWN_ORDER_STATUS": OrderStatus.PENDING,
}


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if value is None or value == "":
        return utcnow()
    if isinstance(value, (int, float)):
        # Coinbase candles use unix seconds
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    s = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        try:
            return datetime.fromtimestamp(float(s), tz=timezone.utc)
        except ValueError:
            return utcnow()


def _to_product_id(symbol: str) -> str:
    """Normalize symbols to Coinbase product ids (e.g. BTCUSD / BTC → BTC-USD)."""
    s = symbol.strip().upper().replace("/", "-").replace("_", "-")
    if "-" in s:
        return s
    # Common crypto base without quote → assume USD
    known = ("BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK", "MATIC", "DOT")
    if s in known:
        return f"{s}-USD"
    if s.endswith("USD") and len(s) > 3:
        return f"{s[:-3]}-USD"
    if s.endswith("USDT") and len(s) > 4:
        return f"{s[:-4]}-USDT"
    return s


def _obj_to_dict(obj: Any) -> Dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict"):
        try:
            return obj.to_dict()  # type: ignore[no-any-return]
        except Exception:
            pass
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return {}




def _apply_trade_fee(settings, notional: float, *, is_taker: bool = True) -> float:
    """Return fee dollars for notional using configured Intro/VIP rates."""
    rate = float(getattr(settings, "taker_fee_rate", 0.009 if is_taker else 0.005))
    if not is_taker:
        rate = float(getattr(settings, "maker_fee_rate", 0.005))
    return max(0.0, float(notional) * rate)

class CoinbaseBroker(BrokerAdapter):
    """Coinbase Advanced Trade spot adapter with hard paper-mode order gate."""

    name = "coinbase"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: Any = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._connected = False
        self._use_sdk = _HAS_SDK
        self._day_start_equity: Optional[float] = None
        # Paper-mode simulated book (never touches live order endpoints)
        self._paper_cash = float(settings.account_equity)
        self._paper_equity = float(settings.account_equity)
        self._paper_positions: Dict[str, Position] = {}
        self._paper_orders: Dict[str, OrderResult] = {}
        self._paper_prices: Dict[str, float] = {}
        self._paper_book_path = Path(
            getattr(settings, "paper_book_path", None)
            or (Path(__file__).resolve().parents[2] / "data" / "paper_book.json")
        )
        # Live bankroll cache (display-only; never mutates paper book / sizing)
        self._live_bankroll_cache: Optional[Dict[str, Any]] = None
        self._live_bankroll_cache_ts: float = 0.0
        self._live_bankroll_ttl_s: float = 60.0
        # Market-data heartbeat (REST and/or WS) — monotonic last success
        self._last_tick_mono: float = 0.0
        self._last_l2_mono: float = 0.0
        self._reconnect_backoff = ExponentialBackoff(base=1.0, factor=2.0, max_delay=60.0)
        self._reconnect_lock = asyncio.Lock()
        self._reconnect_in_progress = False
        self._load_paper_book()

    # ------------------------------------------------------------------ helpers

    def clear_stream_buffers(self) -> None:
        """Clear ephemeral market-data caches (never touches paper book / positions)."""
        self._live_bankroll_cache = None
        self._live_bankroll_cache_ts = 0.0
        # Drop stale paper price marks; book cash/positions untouched
        self._paper_prices.clear()
        logger.debug("CoinbaseBroker stream buffers cleared")


    def _require_credentials(self) -> None:
        if not self.settings.coinbase_api_key or not self.settings.coinbase_api_secret:
            raise RuntimeError(
                "Coinbase CDP credentials missing. Set COINBASE_API_KEY and "
                "COINBASE_API_SECRET (CDP key name / private key), or use "
                "--dry-run / BROKER=mock / PAPER_TRADING_MODE with simulated book."
            )

    def _normalize_secret(self) -> str:
        """CDP PEMs often arrive with literal \\n in env vars."""
        secret = self.settings.coinbase_api_secret
        if "\\n" in secret and "BEGIN" in secret:
            return secret.replace("\\n", "\n")
        return secret

    async def _run_sync(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run blocking SDK calls off the event loop."""
        return await asyncio.to_thread(fn, *args, **kwargs)


    # -------------------------------------------------------------- paper book I/O

    def _load_paper_book(self) -> None:
        """Restore paper cash/positions across restarts. Never invents live balances."""
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
                product = _to_product_id(str(sym))
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
                    self._paper_prices[product] = float(
                        pdata.get("last_price") or avg
                    )
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
            payload = {
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
        """Update bracket / trail / sweet-spot state on an open paper position."""
        product = _to_product_id(symbol)
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
        """Flat reset — use when orphan inventory cannot be reconstructed."""
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
        # L2 also counts as a tick heartbeat
        self._last_tick_mono = now

    @property
    def last_tick_age_seconds(self) -> Optional[float]:
        """Seconds since last successful ticker/quote/bars/L2 fetch; None if never."""
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
            # No successful fetch yet — not stale until we've connected and tried
            return False
        return age > self._stale_tick_limit()

    async def _close_api_surfaces(self) -> None:
        """Close SDK client / aiohttp session (does not wipe paper book)."""
        self._client = None
        if self._session is not None:
            try:
                if not self._session.closed:
                    await self._session.close()
            except Exception as exc:
                logger.debug("session close on reconnect: %s", exc)
            self._session = None

    async def _init_api_client(self) -> None:
        """(Re)create REST SDK or JWT session for market data. Preserves paper book."""
        has_creds = bool(
            self.settings.coinbase_api_key and self.settings.coinbase_api_secret
        )
        if not has_creds:
            logger.info("No Coinbase credentials — market data stays synthetic/offline")
            return
        api_key = self.settings.coinbase_api_key
        api_secret = self._normalize_secret()
        if self._use_sdk and _CoinbaseRESTClient is not None:
            self._client = _CoinbaseRESTClient(
                api_key=api_key,
                api_secret=api_secret,
                timeout=30,
            )
            logger.info("Coinbase REST client (re)initialized")
        else:
            timeout = aiohttp.ClientTimeout(total=30)
            self._session = aiohttp.ClientSession(timeout=timeout)
            logger.warning(
                "coinbase-advanced-py not installed — JWT REST session (re)initialized"
            )

    async def force_reconnect_market_data(self, *, reason: str = "stale") -> None:
        """Close socket/session and re-init with exponential backoff (cap 60s)."""
        async with self._reconnect_lock:
            if self._reconnect_in_progress:
                return
            self._reconnect_in_progress = True
            try:
                age = self.last_tick_age_seconds
                logger.warning(
                    "Market data %s (last_tick_age=%s) — closing client/session and reconnecting",
                    reason,
                    f"{age:.1f}s" if age is not None else "n/a",
                )
                await self._close_api_surfaces()
                delay = self._reconnect_backoff.next_delay()
                logger.info("Reconnect backoff sleep %.1fs", delay)
                await asyncio.sleep(delay)
                await self._init_api_client()
                # Probe with a cheap quote on first allowlisted symbol if possible
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
        """If tick/L2 heartbeat exceeded STALE_TICK_SECONDS, force refresh/reconnect.

        Returns True if data was considered fresh (or no prior tick yet).
        """
        if not self.is_market_data_stale():
            return True
        await self.force_reconnect_market_data(reason="stale_tick")
        return not self.is_market_data_stale()

    # ------------------------------------------------------------------ connect

    async def connect(self) -> None:
        paper = self.settings.paper_trading_mode
        has_creds = bool(
            self.settings.coinbase_api_key and self.settings.coinbase_api_secret
        )

        if paper and not has_creds:
            # Safe local path: paper book only, no network.
            self._connected = True
            self._day_start_equity = self._paper_equity
            logger.info(
                "Coinbase connected in PAPER mode without credentials "
                "(simulated account equity=%.2f). No live orders will be sent.",
                self._paper_equity,
            )
            return

        self._require_credentials()
        await self._init_api_client()
        if self._client is not None:
            logger.info("Coinbase Advanced Trade SDK client ready")

        if paper:
            self._connected = True
            self._day_start_equity = self._paper_equity
            logger.info(
                "Coinbase PAPER mode ON — live create/cancel/liquidate are gated; "
                "orders will be simulated. Market data may still hit the API."
            )
            # Paper sizing must stay on ACCOUNT_EQUITY ($200 book). Live balances
            # are display-only — syncing them into the paper book caused dust fills
            # and bogus day P&L alerts.
            logger.info(
                "PAPER book stays on ACCOUNT_EQUITY=%.2f (live balances not used for sizing)",
                self._paper_equity,
            )
            return

        logger.critical(
            "PAPER_TRADING_MODE is False — live Coinbase order submits are enabled. "
            "Proceed only if intentional."
        )
        acct = await self.get_account()
        self._day_start_equity = acct.equity
        self._connected = True
        logger.info("Coinbase LIVE connected (equity=%.2f)", acct.equity)

    async def disconnect(self) -> None:
        self._connected = False
        self._client = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    # ------------------------------------------------------------------ JWT REST fallback

    def _build_jwt(self, method: str, path: str) -> str:
        """Build a CDP REST JWT for Advanced Trade.

        TODO: Prefer installing ``coinbase-advanced-py`` which handles JWT
        automatically. Manual path uses ``coinbase.jwt_generator`` when the
        package is partially available, else PyJWT + cryptography.
        """
        try:
            from coinbase import jwt_generator

            uri = jwt_generator.format_jwt_uri(method, path)
            return jwt_generator.build_rest_jwt(
                uri,
                self.settings.coinbase_api_key,
                self._normalize_secret(),
            )
        except Exception as exc:
            # TODO: implement standalone JWT with jose/PyJWT:
            #   header: {"alg": "ES256"|"EdDSA", "kid": api_key, "nonce": ...}
            #   claims: {"sub": api_key, "iss": "cdp", "nbf", "exp", "uri": "METHOD hostPATH"}
            raise RuntimeError(
                "JWT generation failed. Install coinbase-advanced-py for CDP auth. "
                f"Detail: {exc}"
            ) from exc

    async def _rest_request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Authenticated REST against api.coinbase.com Advanced Trade.

        TODO: add WS market-data client (wss://advanced-trade-ws.coinbase.com)
        with JWT subscribe for candles/ticker when SDK is unavailable.
        """
        if not self._session:
            raise RuntimeError("CoinbaseBroker REST session not connected")
        url = f"https://{_API_HOST}{path}"
        token = self._build_jwt(method.upper(), path)

        async def _do() -> Any:
            assert self._session is not None
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            async with self._session.request(
                method, url, params=params, json=json_body, headers=headers
            ) as resp:
                text = await resp.text()
                if is_rate_limit_status(resp.status):
                    retry_after = resp.headers.get("Retry-After")
                    raise RateLimitError(
                        f"Coinbase 429: {text[:200]}",
                        retry_after=float(retry_after) if retry_after else None,
                    )
                if resp.status >= 500:
                    raise RetryableError(f"Coinbase {resp.status}: {text[:200]}")
                if resp.status >= 400:
                    raise RuntimeError(f"Coinbase {resp.status}: {text[:400]}")
                if not text:
                    return None
                return json.loads(text)

        return await retry_async(_do, max_attempts=5, backoff=ExponentialBackoff())

    # ------------------------------------------------------------------ account / positions

    async def _fetch_live_account(self) -> AccountState:
        accounts: List[Dict[str, Any]] = []
        if self._client is not None:
            raw = await self._run_sync(lambda: self._client.get_accounts(limit=250))
            data = _obj_to_dict(raw)
            accounts = list(data.get("accounts") or [])
            if not accounts and hasattr(raw, "accounts"):
                accounts = [_obj_to_dict(a) for a in (raw.accounts or [])]
        else:
            data = await self._rest_request("GET", f"{_API_PREFIX}/accounts")
            accounts = list((data or {}).get("accounts") or [])

        cash = 0.0
        equity = 0.0
        for a in accounts:
            currency = str(a.get("currency") or "").upper()
            avail = a.get("available_balance")
            hold = a.get("hold")
            bal = CoinbaseBroker._money_value(avail)
            hold_v = CoinbaseBroker._money_value(hold)
            total = bal + hold_v
            if currency in ("USD", "USDC", "USDT"):
                cash += bal
                equity += total
            else:
                # Non-USD balances counted at last known mark if we have one
                product = f"{currency}-USD"
                px = self._paper_prices.get(product)
                if px is None and self._client is not None:
                    try:
                        quote = await self.get_quote(product)
                        px = quote.mid
                        self._paper_prices[product] = px
                    except Exception:
                        px = 0.0
                equity += total * (px or 0.0)

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


    @staticmethod
    def _money_value(raw: Any) -> float:
        """Parse Coinbase available_balance / Amount dict or SDK object → float."""
        if raw is None:
            return 0.0
        if isinstance(raw, (int, float)):
            return float(raw)
        if isinstance(raw, str):
            try:
                return float(raw) if raw else 0.0
            except ValueError:
                return 0.0
        if isinstance(raw, dict):
            return float(raw.get("value") or 0)
        # SDK nested object
        val = getattr(raw, "value", None)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                return 0.0
        try:
            d = _obj_to_dict(raw)
            return float(d.get("value") or 0)
        except Exception:
            return 0.0

    async def get_live_bankroll(self, *, force: bool = False) -> Dict[str, float]:
        """Read-only live Coinbase cash + portfolio equity.

        Does **not** mutate ``paper_cash`` / paper positions. Used only for
        Telegram bankroll display. Cached ~60s to avoid API hammering.
        """
        now = time.monotonic()
        if (
            not force
            and self._live_bankroll_cache is not None
            and (now - self._live_bankroll_cache_ts) < self._live_bankroll_ttl_s
        ):
            return dict(self._live_bankroll_cache)

        cash_usd = 0.0
        cash_usdc = 0.0
        equity: Optional[float] = None

        has_creds = bool(
            self.settings.coinbase_api_key and self.settings.coinbase_api_secret
        )
        if not has_creds or (self._client is None and self._session is None):
            out = {
                "cash": 0.0,
                "cash_usd": 0.0,
                "cash_usdc": 0.0,
                "equity": 0.0,
            }
            self._live_bankroll_cache = out
            self._live_bankroll_cache_ts = now
            return dict(out)

        # --- USD / USDC available from accounts (available_balance.value) ---
        try:
            accounts: List[Dict[str, Any]] = []
            if self._client is not None:
                raw = await self._run_sync(lambda: self._client.get_accounts(limit=250))
                data = _obj_to_dict(raw)
                accounts = list(data.get("accounts") or [])
                if not accounts and hasattr(raw, "accounts"):
                    accounts = [_obj_to_dict(a) for a in (raw.accounts or [])]
            else:
                data = await self._rest_request("GET", f"{_API_PREFIX}/accounts")
                accounts = list((data or {}).get("accounts") or [])

            for a in accounts:
                currency = str(a.get("currency") or "").upper()
                if currency not in ("USD", "USDC"):
                    continue
                bal = self._money_value(a.get("available_balance"))
                if currency == "USD":
                    cash_usd += bal
                else:
                    cash_usdc += bal
        except Exception as exc:
            logger.warning("get_live_bankroll accounts failed: %s", exc)

        cash = cash_usd + cash_usdc

        # --- Portfolio total equity via breakdown (preferred) ---
        try:
            if self._client is not None and hasattr(self._client, "get_portfolios"):
                pr = await self._run_sync(self._client.get_portfolios)
                pd = _obj_to_dict(pr)
                ports = list(pd.get("portfolios") or [])
                if not ports and hasattr(pr, "portfolios"):
                    ports = [_obj_to_dict(p) for p in (pr.portfolios or [])]
                uid = None
                if ports:
                    p0 = ports[0] if isinstance(ports[0], dict) else _obj_to_dict(ports[0])
                    uid = p0.get("uuid")
                if uid and hasattr(self._client, "get_portfolio_breakdown"):
                    bd_raw = await self._run_sync(
                        lambda: self._client.get_portfolio_breakdown(str(uid))
                    )
                    bd = _obj_to_dict(bd_raw)
                    breakdown = bd.get("breakdown") or {}
                    if not breakdown and hasattr(bd_raw, "breakdown"):
                        breakdown = _obj_to_dict(bd_raw.breakdown)
                    bals = breakdown.get("portfolio_balances") or {}
                    if not isinstance(bals, dict):
                        bals = _obj_to_dict(bals)
                    total = bals.get("total_balance")
                    equity = self._money_value(total)
                    # Prefer cash-equivalent from breakdown if accounts path empty
                    if cash <= 0:
                        ce = bals.get("total_cash_equivalent_balance")
                        ce_v = self._money_value(ce)
                        if ce_v > 0:
                            cash = ce_v
                            cash_usd = ce_v
        except Exception as exc:
            logger.warning("get_live_bankroll portfolio breakdown failed: %s", exc)

        if equity is None or equity <= 0:
            # Fallback: live account mark-to-market (read-only; may quote non-USD)
            try:
                # Temporarily compute without touching paper book fields beyond price cache
                live = await self._fetch_live_account()
                equity = float(live.equity or 0)
                if cash <= 0 and live.cash > 0:
                    cash = float(live.cash)
                    cash_usd = cash
            except Exception as exc:
                logger.warning("get_live_bankroll equity fallback failed: %s", exc)
                equity = cash

        out = {
            "cash": float(cash),
            "cash_usd": float(cash_usd),
            "cash_usdc": float(cash_usdc),
            "equity": float(equity or cash),
        }
        self._live_bankroll_cache = out
        self._live_bankroll_cache_ts = now
        logger.debug(
            "live bankroll refreshed cash=%.2f equity=%.2f",
            out["cash"],
            out["equity"],
        )
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

        accounts: List[Dict[str, Any]] = []
        if self._client is not None:
            raw = await self._run_sync(lambda: self._client.get_accounts(limit=250))
            data = _obj_to_dict(raw)
            accounts = list(data.get("accounts") or [])
            if not accounts and hasattr(raw, "accounts"):
                accounts = [_obj_to_dict(a) for a in (raw.accounts or [])]
        else:
            data = await self._rest_request("GET", f"{_API_PREFIX}/accounts")
            accounts = list((data or {}).get("accounts") or [])

        out: List[Position] = []
        for a in accounts:
            currency = str(a.get("currency") or "").upper()
            if currency in ("USD", "USDC", "USDT"):
                continue
            avail = a.get("available_balance") or {}
            qty = float(avail.get("value") or 0) if isinstance(avail, dict) else float(avail or 0)
            if qty <= 0:
                continue
            product = f"{currency}-USD"
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
        product = _to_product_id(symbol)
        for p in await self.get_positions():
            if p.symbol == product or p.symbol == symbol.upper():
                return p
        return None

    # ------------------------------------------------------------------ market data

    async def get_bars(
        self,
        symbol: str,
        *,
        timeframe: str = "1Min",
        limit: int = 100,
    ) -> List[Bar]:
        product = _to_product_id(symbol)
        gran = _TIMEFRAME_TO_GRANULARITY.get(timeframe, "ONE_MINUTE")
        end = int(time.time())
        # span enough seconds for `limit` candles
        seconds = {
            "ONE_MINUTE": 60,
            "FIVE_MINUTE": 300,
            "FIFTEEN_MINUTE": 900,
            "THIRTY_MINUTE": 1800,
            "ONE_HOUR": 3600,
            "TWO_HOUR": 7200,
            "FOUR_HOUR": 14400,
            "SIX_HOUR": 21600,
            "ONE_DAY": 86400,
        }.get(gran, 60)
        start = end - seconds * max(limit, 1) - seconds

        # No credentials + paper → synthetic bars (unit-test / offline safe)
        if self.settings.paper_trading_mode and self._client is None and self._session is None:
            bars = self._synthetic_bars(product, limit=limit)
            self._touch_tick()
            return bars

        candles: List[Any] = []
        if self._client is not None:
            raw = await self._run_sync(
                self._client.get_candles,
                product,
                str(start),
                str(end),
                gran,
                limit=limit,
            )
            data = _obj_to_dict(raw)
            candles = list(data.get("candles") or [])
            if not candles and hasattr(raw, "candles"):
                candles = list(raw.candles or [])
        else:
            data = await self._rest_request(
                "GET",
                f"{_API_PREFIX}/products/{product}/candles",
                params={
                    "start": str(start),
                    "end": str(end),
                    "granularity": gran,
                    "limit": limit,
                },
            )
            candles = list((data or {}).get("candles") or [])

        bars: List[Bar] = []
        for c in candles:
            cd = _obj_to_dict(c) if not isinstance(c, dict) else c
            # Advanced Trade candle fields: start, open, high, low, close, volume
            close = float(cd.get("close") or 0)
            bars.append(
                Bar(
                    symbol=product,
                    timestamp=_parse_ts(cd.get("start") or cd.get("start_time")),
                    open=float(cd.get("open") or close),
                    high=float(cd.get("high") or close),
                    low=float(cd.get("low") or close),
                    close=close,
                    volume=float(cd.get("volume") or 0),
                )
            )
        bars.sort(key=lambda b: b.timestamp)
        if bars:
            self._paper_prices[product] = bars[-1].close
            self._touch_tick()
        return bars[-limit:] if limit else bars

    def _synthetic_bars(self, product: str, limit: int = 100) -> List[Bar]:
        """Offline paper bars when no network/credentials (deterministic-ish)."""
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
            l = min(o, c) * (1 - abs(rng.gauss(0, 0.001)))
            bars.append(
                Bar(
                    symbol=product,
                    timestamp=now - timedelta(minutes=i),
                    open=round(o, 6),
                    high=round(h, 6),
                    low=round(l, 6),
                    close=round(c, 6),
                    volume=float(rng.randint(10, 500)),
                    vwap=round((h + l + c) / 3, 6),
                )
            )
            price = c
        self._paper_prices[product] = price
        return bars

    async def get_quote(self, symbol: str) -> Quote:
        product = _to_product_id(symbol)

        if self.settings.paper_trading_mode and self._client is None and self._session is None:
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

        bid = ask = 0.0
        if self._client is not None:
            raw = await self._run_sync(self._client.get_best_bid_ask, [product])
            data = _obj_to_dict(raw)
            books = data.get("pricebooks") or data.get("priceBooks") or []
            if not books and hasattr(raw, "pricebooks"):
                books = [_obj_to_dict(b) for b in (raw.pricebooks or [])]
            if books:
                book = books[0] if isinstance(books[0], dict) else _obj_to_dict(books[0])
                bids = book.get("bids") or []
                asks = book.get("asks") or []
                if bids:
                    b0 = bids[0] if isinstance(bids[0], dict) else _obj_to_dict(bids[0])
                    bid = float(b0.get("price") or 0)
                if asks:
                    a0 = asks[0] if isinstance(asks[0], dict) else _obj_to_dict(asks[0])
                    ask = float(a0.get("price") or 0)
        else:
            data = await self._rest_request(
                "GET",
                f"{_API_PREFIX}/best_bid_ask",
                params={"product_ids": product},
            )
            books = (data or {}).get("pricebooks") or []
            if books:
                bids = books[0].get("bids") or []
                asks = books[0].get("asks") or []
                if bids:
                    bid = float(bids[0].get("price") or 0)
                if asks:
                    ask = float(asks[0].get("price") or 0)

        if bid <= 0 and ask <= 0:
            # fallback to last candle close
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
        """
        Fetch L2 product book (REST). Returns dict with bids/asks lists of
        {price, size}, plus mid. Empty bids/asks on failure (caller skips safely).
        """
        product = _to_product_id(symbol)
        empty: Dict[str, Any] = {"product_id": product, "bids": [], "asks": [], "mid": None}
        try:
            if self.settings.paper_trading_mode and self._client is None and self._session is None:
                # Synthetic thin book around last paper mid for offline tests
                mid = float(self._paper_prices.get(product, 100.0))
                spread = mid * 0.0004
                levels = []
                for i in range(1, min(limit, 10) + 1):
                    levels.append(i)
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

            raw_bids: List[Any] = []
            raw_asks: List[Any] = []
            if self._client is not None:
                # SDK: get_product_book(product_id, limit=...)
                fn = getattr(self._client, "get_product_book", None) or getattr(
                    self._client, "get_product_book_v3", None
                )
                if fn is None:
                    logger.debug("SDK has no get_product_book — trying REST")
                else:
                    raw = await self._run_sync(fn, product_id=product, limit=limit)
                    data = _obj_to_dict(raw)
                    # pricebook nested or top-level
                    book = data.get("pricebook") or data.get("priceBook") or data
                    if not isinstance(book, dict):
                        book = _obj_to_dict(book)
                    raw_bids = list(book.get("bids") or [])
                    raw_asks = list(book.get("asks") or [])
            if not raw_bids and not raw_asks:
                data = await self._rest_request(
                    "GET",
                    f"{_API_PREFIX}/product_book",
                    params={"product_id": product, "limit": limit},
                )
                book = (data or {}).get("pricebook") or (data or {})
                raw_bids = list(book.get("bids") or [])
                raw_asks = list(book.get("asks") or [])

            def _norm(levels: List[Any]) -> List[Dict[str, float]]:
                out: List[Dict[str, float]] = []
                for lvl in levels:
                    d = lvl if isinstance(lvl, dict) else _obj_to_dict(lvl)
                    try:
                        px = float(d.get("price") or 0)
                        sz = float(d.get("size") or d.get("qty") or 0)
                    except (TypeError, ValueError):
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
        """Convenience: L2 imbalance ratio within band of mid, or None."""
        from trading_bot.utils.indicators import l2_depth_imbalance

        book = await self.get_l2_book(symbol, limit=limit)
        return l2_depth_imbalance(
            book.get("bids") or [],
            book.get("asks") or [],
            mid=book.get("mid"),
            band_pct=band_pct,
        )

    # ------------------------------------------------------------------ orders (paper-gated)

    def _simulate_fill(self, order: OrderRequest) -> OrderResult:
        # Paper path also enforces maker-limit-only (no market fill shortcut)
        if bool(getattr(self.settings, "post_only", True)):
            if getattr(order, "order_type", None) != OrderType.LIMIT or order.limit_price is None:
                broker_id = f"cb-paper-reject-{uuid4().hex[:8]}"
                result = OrderResult(
                    client_order_id=order.client_order_id,
                    broker_order_id=broker_id,
                    status=OrderStatus.REJECTED,
                    symbol=_to_product_id(order.symbol),
                    side=order.side,
                    qty=order.qty,
                    message="market_orders_disabled",
                    paper=True,
                )
                self._paper_orders[broker_id] = result
                logger.warning(
                    "PAPER reject market/non-limit under POST_ONLY: %s %s type=%s",
                    order.side.value,
                    order.symbol,
                    getattr(getattr(order, "order_type", None), "value", order.order_type),
                )
                return result
        product = _to_product_id(order.symbol)
        px = order.limit_price or self._paper_prices.get(product, 100.0)
        cost = px * order.qty
        broker_id = f"cb-paper-{uuid4().hex[:12]}"

        is_taker = not (
            bool(getattr(order, "post_only", False))
            or bool(getattr(self.settings, "post_only", False))
        )
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
            self._paper_cash -= (cost + fee)
            existing = self._paper_positions.get(product)
            if existing:
                new_qty = existing.qty + order.qty
                existing.avg_entry_price = (
                    existing.avg_entry_price * existing.qty + cost
                ) / new_qty
                existing.qty = new_qty
                existing.market_value = new_qty * px
                # Refresh brackets on add (only if pyramiding ever enabled)
                if order.stop_loss is not None:
                    existing.stop_loss = order.stop_loss
                if order.take_profit is not None:
                    existing.take_profit = order.take_profit
                if existing.trail_high_water is None or px > existing.trail_high_water:
                    existing.trail_high_water = px
            else:
                trail_dist = None
                # trail distance may arrive via stop_loss/take_profit only on OrderRequest;
                # main/risk stores trail on position after fill via update_position_brackets
                sweet = bool(getattr(self.settings, "is_sweet_spot", False))
                self._paper_positions[product] = Position(
                    symbol=product,
                    qty=order.qty,
                    avg_entry_price=px,
                    market_value=cost,
                    side="long",
                    stop_loss=order.stop_loss,
                    take_profit=order.take_profit,
                    trail_distance=None if sweet else trail_dist,
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
            self._paper_cash += (proceeds - fee)
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
            message="paper simulated fill (no live Coinbase submit)",
            paper=True,
        )
        self._paper_orders[broker_id] = result
        self._save_paper_book()
        return result

    async def submit_order(self, order: OrderRequest) -> OrderResult:
        # HARD GATE: never place real orders in paper mode
        if self.settings.paper_trading_mode:
            logger.info(
                "PAPER gate: would submit Coinbase order %s %s qty=%s limit=%s "
                "(simulated fill only)",
                order.side.value,
                _to_product_id(order.symbol),
                order.qty,
                order.limit_price,
            )
            return self._simulate_fill(order)

        product = _to_product_id(order.symbol)
        side = order.side.value.upper()
        client_id = order.client_order_id
        base_size = str(order.qty)

        if self._client is not None:
            if order.order_type == OrderType.LIMIT and order.limit_price is not None:
                raw = await self._run_sync(
                    self._client.limit_order_gtc,
                    client_id,
                    product,
                    side,
                    base_size,
                    str(order.limit_price),
                )
            else:
                raw = await self._run_sync(
                    self._client.market_order,
                    client_id,
                    product,
                    side,
                    base_size=base_size,
                )
            data = _obj_to_dict(raw)
            success = data.get("success")
            if success is None and hasattr(raw, "success"):
                success = raw.success
            resp = data.get("success_response") or data.get("order_id")
            if isinstance(resp, dict):
                order_id = str(resp.get("order_id") or "")
            else:
                order_id = str(data.get("order_id") or getattr(raw, "order_id", "") or "")
            if not success and not order_id:
                err = data.get("error_response") or data.get("failure_reason") or data
                return OrderResult(
                    client_order_id=client_id,
                    broker_order_id=None,
                    status=OrderStatus.REJECTED,
                    symbol=product,
                    side=order.side,
                    qty=order.qty,
                    message=str(err)[:400],
                    paper=False,
                )
            return OrderResult(
                client_order_id=client_id,
                broker_order_id=order_id or None,
                status=OrderStatus.SUBMITTED,
                symbol=product,
                side=order.side,
                qty=order.qty,
                message="live submitted",
                paper=False,
            )

        # JWT REST fallback
        # TODO: map stop/bracket orders via order_configuration.stop_limit_gtc etc.
        if order.order_type == OrderType.LIMIT and order.limit_price is not None:
            config: Dict[str, Any] = {
                "limit_limit_gtc": {
                    "base_size": base_size,
                    "limit_price": str(order.limit_price),
                    "post_only": bool(
                        getattr(order, "post_only", False)
                        or getattr(self.settings, "post_only", True)
                    ),
                }
            }
        else:
            config = {"market_market_ioc": {"base_size": base_size}}
        body = {
            "client_order_id": client_id,
            "product_id": product,
            "side": side,
            "order_configuration": config,
        }
        data = await self._rest_request(
            "POST", f"{_API_PREFIX}/orders", json_body=body
        )
        success = (data or {}).get("success")
        order_id = ((data or {}).get("success_response") or {}).get("order_id")
        if not success:
            err = (data or {}).get("error_response") or data
            return OrderResult(
                client_order_id=client_id,
                broker_order_id=None,
                status=OrderStatus.REJECTED,
                symbol=product,
                side=order.side,
                qty=order.qty,
                message=str(err)[:400],
                paper=False,
            )
        return OrderResult(
            client_order_id=client_id,
            broker_order_id=str(order_id) if order_id else None,
            status=OrderStatus.SUBMITTED,
            symbol=product,
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

        if self._client is not None:
            raw = await self._run_sync(self._client.cancel_orders, [broker_order_id])
            data = _obj_to_dict(raw)
            results = data.get("results") or []
            if results:
                r0 = results[0] if isinstance(results[0], dict) else _obj_to_dict(results[0])
                return bool(r0.get("success"))
            return True

        data = await self._rest_request(
            "POST",
            f"{_API_PREFIX}/orders/batch_cancel",
            json_body={"order_ids": [broker_order_id]},
        )
        results = (data or {}).get("results") or []
        if results:
            return bool(results[0].get("success"))
        return False

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

        # List open then cancel
        open_ids: List[str] = []
        if self._client is not None:
            raw = await self._run_sync(
                self._client.list_orders, order_status=["OPEN"]
            )
            data = _obj_to_dict(raw)
            orders = data.get("orders") or []
            if not orders and hasattr(raw, "orders"):
                orders = [_obj_to_dict(o) for o in (raw.orders or [])]
            for o in orders:
                od = o if isinstance(o, dict) else _obj_to_dict(o)
                oid = od.get("order_id")
                if oid:
                    open_ids.append(str(oid))
        else:
            data = await self._rest_request(
                "GET",
                f"{_API_PREFIX}/orders/historical/batch",
                params={"order_status": "OPEN"},
            )
            for o in (data or {}).get("orders") or []:
                if o.get("order_id"):
                    open_ids.append(str(o["order_id"]))

        if not open_ids:
            return 0
        if self._client is not None:
            await self._run_sync(self._client.cancel_orders, open_ids)
        else:
            await self._rest_request(
                "POST",
                f"{_API_PREFIX}/orders/batch_cancel",
                json_body={"order_ids": open_ids},
            )
        return len(open_ids)

    async def get_order(self, broker_order_id: str) -> OrderResult:
        if self.settings.paper_trading_mode:
            if broker_order_id in self._paper_orders:
                return self._paper_orders[broker_order_id]
            raise KeyError(broker_order_id)

        if self._client is not None:
            raw = await self._run_sync(self._client.get_order, broker_order_id)
            data = _obj_to_dict(raw)
            order = data.get("order") or data
            if not isinstance(order, dict) and hasattr(raw, "order"):
                order = _obj_to_dict(raw.order)
        else:
            data = await self._rest_request(
                "GET", f"{_API_PREFIX}/orders/historical/{broker_order_id}"
            )
            order = (data or {}).get("order") or data or {}

        status_raw = str(order.get("status") or "PENDING").upper()
        side_raw = str(order.get("side") or "BUY").upper()
        return OrderResult(
            client_order_id=str(order.get("client_order_id") or ""),
            broker_order_id=str(order.get("order_id") or broker_order_id),
            status=_STATUS_MAP.get(status_raw, OrderStatus.PENDING),
            symbol=str(order.get("product_id") or "").upper(),
            side=OrderSide.BUY if side_raw == "BUY" else OrderSide.SELL,
            qty=float(order.get("order_configuration", {})
                      .get("limit_limit_gtc", {})
                      .get("base_size")
                      or order.get("filled_size")
                      or 0),
            filled_qty=float(order.get("filled_size") or 0),
            avg_fill_price=float(order["average_filled_price"])
            if order.get("average_filled_price")
            else None,
            message=status_raw,
            paper=False,
        )

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
                )
                results.append(await self.submit_order(req))
            logger.info("PAPER gate: liquidate_all simulated n=%s", len(results))
            return results

        results = []
        for pos in await self.get_positions():
            _sym = Settings.normalize_symbol(pos.symbol)
            if _sym not in HARD_SYMBOL_ALLOWLIST:
                logger.info("Skipping non-allowlist holding: %s", _sym)
                continue
            if self._client is not None:
                raw = await self._run_sync(
                    self._client.close_position,
                    f"liq-{uuid4().hex[:12]}",
                    pos.symbol,
                    size=str(pos.qty),
                )
                data = _obj_to_dict(raw)
                order_id = str(
                    data.get("order_id")
                    or (data.get("success_response") or {}).get("order_id")
                    or ""
                )
                results.append(
                    OrderResult(
                        client_order_id=f"liq-{pos.symbol}",
                        broker_order_id=order_id or None,
                        status=OrderStatus.SUBMITTED,
                        symbol=pos.symbol,
                        side=OrderSide.SELL,
                        qty=abs(pos.qty),
                        message="close_position submitted",
                        paper=False,
                    )
                )
            else:
                # TODO: market sell via POST /orders when SDK absent
                req = OrderRequest(
                    symbol=pos.symbol,
                    side=OrderSide.SELL,
                    qty=pos.qty,
                    order_type=OrderType.MARKET,
                    paper=False,
                )
                # Temporarily allow live path through submit (paper already False)
                results.append(await self.submit_order(req))
        return results

    async def stream_bars(
        self,
        symbols: List[str],
        on_bar: Callable[[Bar], None],
    ) -> None:
        """Optional WS bar stream.

        TODO: wire ``coinbase.websocket.WSClient.candles`` with reconnect/backoff
        when a persistent stream is required. Default path is REST polling via
        DataFeed.
        """
        products = [_to_product_id(s) for s in symbols]
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
                logger.warning("Coinbase bar poll error (%s); retry in %.1fs", exc, delay)
                await asyncio.sleep(delay)
