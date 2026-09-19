#!/usr/bin/env python3
"""Unified entrypoint for the Agentic Day Trading Bot."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

from trading_bot.agent_core import AgentCore, SetupPreFilter, VwapMomentumScalpEngine
from trading_bot.strategy_volume_sweet_spot import VolumeSweetSpotEngine
from trading_bot.structural_guardrails import (
    check_buy_dedupe,
    check_spread,
    guardrails_startup_message,
)
from trading_bot.brokers.alpaca import AlpacaBroker
from trading_bot.brokers.base import BrokerAdapter
from trading_bot.brokers.coinbase import CoinbaseBroker
from trading_bot.brokers.ib_stub import IBBrokerStub
from trading_bot.brokers.mock import MockBroker
from trading_bot.config import (
    PROJECT_ROOT,
    SET_LIMIT_ENV_MAX_BOOK,
    SET_LIMIT_ENV_TRADE_CAP,
    Settings,
    apply_set_limit_to_settings,
    get_settings,
    reload_settings,
    upsert_env_vars,
)
from trading_bot.data_feed import DataFeed
from trading_bot.executor import Executor
from trading_bot.logger import TradeLogger, setup_logging
from trading_bot.macro_calendar import build_macro_guard
from trading_bot.models import (
    Action,
    AgentObservation,
    Decision,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    utcnow,
)
from trading_bot.notifier import Notifier, build_notifier
from trading_bot.risk_manager import RiskManager
from trading_bot.state_store import BehavioralStateStore
from trading_bot.derivatives_feed import FundingOIFilter, PerpLeadLagEngine
from trading_bot.onchain_guards import OnchainGuards
from trading_bot.order_reslicer import OrderReslicer
from trading_bot.cointegration import CointegrationEngine, parse_pairs
from trading_bot.sweep_fade import SweepFadeEngine
from trading_bot.failure_postmortem import (
    build_failure_snapshot,
    check_failure_blacklist,
    classify_failure,
)
from trading_bot.optimizer import (
    load_active_params,
    nightly_optimizer_loop,
    run_optimizer,
)
from trading_bot.telegram_commands import (
    ALERT_LIVE_ACTIVATED,
    ALERT_PAPER_RESTORED,
    LIVE_CONFIRM_TTL_SECONDS,
    OpsControlState,
    REPLY_CONFIRM_EXPIRED,
    REPLY_MODE_LIVE_PENDING,
    TelegramCommandListener,
    current_pid,
    day_trades_from_ledger,
    format_mode_reply,
    format_status_reply,
)
from trading_bot.backup import (
    backup_sqlite_dbs,
    default_db_sources,
    seconds_until_chicago_midnight,
)
logger = logging.getLogger(__name__)


def format_crash_alert(exc: BaseException) -> str:
    """Short CRITICAL crash text — exception type + message; never secrets."""
    etype = type(exc).__name__
    msg = str(exc).strip().replace("\n", " ")
    # Scrub common secret-ish substrings defensively
    for bad in ("BEGIN PRIVATE", "api_secret", "BOT_TOKEN", "password=", "secret="):
        if bad.lower() in msg.lower():
            msg = "[redacted]"
            break
    if len(msg) > 180:
        msg = msg[:177] + "..."
    if msg:
        return f"CRITICAL: Bot crashed due to {etype}: {msg}"
    return f"CRITICAL: Bot crashed due to {etype}"


async def notify_critical_crash(notifier: Optional[Notifier], exc: BaseException) -> None:
    """Urgent Telegram/Discord crash ping. No-op if notifier unset."""
    text = format_crash_alert(exc)
    logger.critical(text)
    if notifier is None:
        return
    try:
        await notifier.send(text, high_priority=True)
    except Exception as notify_exc:
        logger.warning("Crash Telegram notify failed: %s", notify_exc)


def _fmt_pnl_brief(amount: float) -> str:
    a = float(amount)
    return f"-${abs(a):.2f}" if a < 0 else f"+${a:.2f}"


def build_broker(settings: Settings) -> BrokerAdapter:
    name = settings.effective_broker
    if name == "mock":
        return MockBroker(equity=settings.account_equity, symbols=settings.symbol_list)
    if name == "ib":
        return IBBrokerStub()
    if name == "coinbase":
        return CoinbaseBroker(settings)
    return AlpacaBroker(settings)


class TradingApp:
    def __init__(self, settings: Settings, once: bool = False) -> None:
        self.settings = settings
        self.once = once
        self.broker = build_broker(settings)
        self.trade_logger = TradeLogger(settings.sqlite_path, settings.postgres_dsn)
        self.feed = DataFeed(self.broker, settings)
        self.state = BehavioralStateStore(
            settings.sqlite_path,
            revenge_stop_count=settings.revenge_stop_count,
            revenge_lockout_minutes=settings.revenge_lockout_minutes,
            memory_size=settings.trade_memory_size,
            post_stop_cooldown_min=settings.post_stop_cooldown_min,
        )
        self.notifier: Notifier = build_notifier(
            discord_webhook_url=settings.discord_webhook_url,
            telegram_bot_token=settings.telegram_bot_token,
            telegram_chat_id=settings.telegram_chat_id,
            quiet=bool(settings.quiet_notifier),
        )
        self.macro = build_macro_guard(
            enabled=settings.macro_pause_enabled,
            pause_minutes=settings.macro_pause_minutes,
            calendar_url=settings.macro_calendar_url,
            api_key=settings.macro_calendar_api_key,
        )
        if settings.is_sweet_spot:
            signal_engine = VolumeSweetSpotEngine(
                rvol_breakout_mult=settings.rvol_breakout_mult,
                pullback_vol_frac=settings.pullback_vol_frac,
                min_tp_pct=settings.min_tp_pct,
                min_clear_to_resistance_pct=settings.min_clear_to_resistance_pct,
                swing_sl_buffer_pct=settings.swing_sl_buffer_pct,
                maker_fee_rate=settings.maker_fee_rate,
                fee_to_target_mult=settings.fee_to_target_mult,
                tp1_rr=settings.tp1_rr,
                tp2_rr=settings.tp2_rr,
                volume_sma_period=settings.volume_sma_period,
                min_confidence=settings.min_confidence,
                l2_imbalance_enabled=bool(settings.l2_imbalance_enabled),
                l2_imbalance_min_ratio=float(settings.l2_imbalance_min_ratio),
                regime_filter_enabled=bool(settings.regime_filter_enabled),
                adx_min=float(settings.adx_min),
                chop_max=float(settings.chop_max),
                mtf_align_enabled=bool(settings.mtf_align_enabled),
                cvd_divergence_enabled=bool(
                    getattr(settings, "cvd_divergence_enabled", True)
                ),
                liq_sweep_required=bool(getattr(settings, "liq_sweep_required", True)),
                liq_sweep_short_usd=float(
                    getattr(settings, "liq_sweep_short_usd", 50_000.0) or 50_000.0
                ),
                cvd_warmup_fail_closed=bool(
                    getattr(settings, "cvd_warmup_fail_closed", True)
                ),
            )
            # Engine owns entry gates — skip legacy VWAP-spike prefilter
            require_prefilter = False
            logger.info(
                "Strategy mode=volume_sweet_spot (no ATR trail; post_only=%s; max_hold=%sm)",
                settings.post_only,
                settings.max_hold_minutes,
            )
        else:
            signal_engine = VwapMomentumScalpEngine(
                min_confidence=settings.min_confidence,
                stop_loss_atr_mult=settings.stop_loss_atr_mult,
                take_profit_atr_mult=settings.take_profit_atr_mult,
                taker_fee_rate=settings.taker_fee_rate,
                fee_clear_mult=settings.fee_clear_mult,
                rsi_buy_cap=settings.rsi_buy_cap,
            )
            require_prefilter = True
        self.agent = AgentCore(
            use_llm=settings.use_llm,
            signal_engine=signal_engine,
            prefilter=SetupPreFilter(
                vwap_boundary_pct=settings.prefilter_vwap_boundary_pct,
                volume_spike_mult=settings.prefilter_volume_spike_mult,
            ),
            require_prefilter=require_prefilter,
            vwap_boundary_pct=settings.prefilter_vwap_boundary_pct,
            volume_spike_mult=settings.prefilter_volume_spike_mult,
        )
        self.risk = RiskManager(settings)
        self.executor = Executor(self.broker, settings, self.trade_logger, notifier=self.notifier)
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._cb_notified = False
        # Dedupe rejected / flat SELLs so agent spam every poll doesn't flood logs
        self._sell_cooldown_until: Dict[str, float] = {}
        self.ops = OpsControlState()
        self._tg_listener: Optional[TelegramCommandListener] = None
        # Intel v2: lead-lag priority + funding/OI + failure blacklist cache
        self._priority_symbols: list[str] = []
        self._priority_event = asyncio.Event()
        self._last_obs: Dict[str, AgentObservation] = {}
        self._active_params: dict = {}
        self.leadlag = PerpLeadLagEngine(
            enabled=bool(getattr(settings, "perp_leadlag_enabled", True)),
            venues=str(getattr(settings, "perp_leadlag_venues", "binance,bybit") or "binance,bybit"),
            symbols=("BTC-USD", "ETH-USD"),
            sweep_mult=float(getattr(settings, "perp_sweep_mult", 3.0) or 3.0),
            sweep_window_sec=float(getattr(settings, "perp_sweep_window_sec", 5.0) or 5.0),
            liq_window_sec=float(getattr(settings, "perp_liq_window_sec", 10.0) or 10.0),
            liq_min_cluster=int(getattr(settings, "perp_liq_min_cluster", 5) or 5),
            signal_ttl_sec=float(getattr(settings, "perp_signal_ttl_sec", 30.0) or 30.0),
            on_signal=self._on_leadlag_signal,
        )
        # Strategy reads CVD/liq snapshots only (non-blocking); WS updates stay in leadlag
        try:
            eng = getattr(self.agent, "signal_engine", None)
            if eng is not None and hasattr(eng, "set_leadlag"):
                eng.set_leadlag(self.leadlag)
        except Exception as exc:
            logger.debug("wire leadlag→sweet_spot: %s", exc)
        self.funding_oi = FundingOIFilter(
            enabled=bool(getattr(settings, "funding_oi_enabled", True)),
            symbols=settings.symbol_list,
            poll_seconds=float(getattr(settings, "funding_oi_poll_seconds", 60.0) or 60.0),
            funding_block_threshold=float(
                getattr(settings, "funding_block_threshold", 0.0003) or 0.0003
            ),
            funding_boost_threshold=float(
                getattr(settings, "funding_boost_threshold", -0.0001) or -0.0001
            ),
            oi_surge_pct=float(getattr(settings, "funding_oi_surge_pct", 0.02) or 0.02),
            stagnant_bars=int(getattr(settings, "funding_stagnant_bars", 3) or 3),
        )
        # Load walk-forward params (hot) without killing positions
        
        # Intel v3: on-chain / reslice / cointegration / sweep-fade
        self.onchain = OnchainGuards(
            enabled=bool(getattr(settings, "onchain_guards_enabled", True)),
            poll_seconds=float(getattr(settings, "onchain_poll_seconds", 120.0) or 120.0),
            cache_ttl_sec=float(getattr(settings, "onchain_cache_ttl_sec", 180.0) or 180.0),
            inflow_spike_mult=float(
                getattr(settings, "onchain_inflow_spike_mult", 2.5) or 2.5
            ),
            mint_boost=float(getattr(settings, "stablecoin_mint_boost", 3.0) or 3.0),
            flow_url=str(getattr(settings, "onchain_flow_url", "") or ""),
            stable_url=str(getattr(settings, "onchain_stable_url", "") or ""),
            api_key=str(getattr(settings, "onchain_api_key", "") or ""),
            mock_mode=bool(getattr(settings, "onchain_mock_mode", False)),
        )
        self.reslicer = OrderReslicer(
            enabled=bool(getattr(settings, "order_reslice_enabled", True)),
            stall_sec=float(getattr(settings, "order_reslice_stall_sec", 10.0) or 10.0),
            max_reslices=int(getattr(settings, "order_reslice_max_times", 3) or 3),
        )
        self.cointegration = CointegrationEngine(
            enabled=bool(getattr(settings, "cointegration_enabled", True)),
            pairs=parse_pairs(str(getattr(settings, "coint_pairs", "") or "")),
            z_entry=float(getattr(settings, "coint_z_entry", 2.0) or 2.0),
            window=int(getattr(settings, "coint_window", 96) or 96),
            min_corr=float(getattr(settings, "coint_min_corr", 0.5) or 0.5),
        )
        self.sweep_fade = SweepFadeEngine(
            enabled=bool(getattr(settings, "sweep_fade_enabled", True)),
            lookback=int(getattr(settings, "sweep_fade_lookback", 50) or 50),
            absorption_ratio=float(
                getattr(settings, "sweep_fade_absorption_ratio", 1.5) or 1.5
            ),
        )

        self._apply_active_params(load_active_params(Path(settings.active_params_path)))

    def request_stop(self) -> None:
        self._stop.set()

    @property
    def ops_paused(self) -> bool:
        return bool(self.ops.paused)

    async def _build_status_text(self) -> str:
        try:
            acct = await self.broker.get_account()
            cash = float(acct.cash)
            equity = float(acct.equity)
        except Exception:
            cash = equity = float(self.settings.account_equity)
        positions = []
        try:
            for pos in await self.broker.get_positions():
                sym = getattr(pos, "symbol", "")
                if not self.settings.is_allowlisted(sym):
                    continue
                qty = float(getattr(pos, "qty", 0) or 0)
                if abs(qty) <= 1e-12:
                    continue
                positions.append(
                    {
                        "symbol": sym,
                        "qty": qty,
                        "avg_entry_price": getattr(pos, "avg_entry_price", None),
                        "market_value": getattr(pos, "market_value", None),
                    }
                )
        except Exception as exc:
            logger.debug("status positions: %s", exc)
        age = None
        try:
            age = self.feed.last_tick_age_seconds
        except Exception:
            age = getattr(self.broker, "last_tick_age_seconds", None)

        entry_proximity = None
        proximity_symbol = None
        proximity_price = None
        try:
            eng = getattr(self.agent, "signal_engine", None)
            if eng is not None and hasattr(eng, "get_entry_proximity"):
                best_score = -1.0
                best_prox = None
                best_sym = None
                best_px = None
                # Prefer highest proximity; BTC-USD wins ties
                items = list((self._last_obs or {}).items())
                items.sort(
                    key=lambda kv: (
                        0 if str(kv[0]).upper() in ("BTC-USD", "BTC/USD") else 1,
                        str(kv[0]),
                    )
                )
                for sym, obs in items:
                    try:
                        prox = eng.get_entry_proximity(obs)
                        sc = float((prox or {}).get("score") or 0.0)
                        # Strict > keeps earlier (BTC-first) on ties
                        if sc > best_score:
                            best_score = sc
                            best_prox = prox
                            best_sym = sym
                            try:
                                best_px = float(obs.indicators.close or 0) or None
                            except Exception:
                                best_px = None
                    except Exception:
                        continue
                if best_prox is not None:
                    entry_proximity = best_prox
                    proximity_symbol = best_sym
                    proximity_price = best_px
        except Exception as exc:
            logger.debug("status entry proximity: %s", exc)

        return format_status_reply(
            paper_cash=cash,
            paper_equity=equity,
            positions=positions,
            paused=self.ops.paused,
            strategy_mode=str(self.settings.strategy_mode or ""),
            last_tick_age_seconds=age,
            paper=bool(self.settings.paper_trading_mode),
            entry_proximity=entry_proximity,
            proximity_symbol=proximity_symbol,
            proximity_price=proximity_price,
            allowlist_symbols=list(self.settings.symbol_list),
            starting_equity=float(self.settings.account_equity),
            trade_cap=float(self.settings.max_notional_per_trade_usd),
            max_exposure=float(self.settings.max_total_exposure_usd),
        )

    async def _cmd_status(self, _cmd: str, _args: list[str]) -> str:
        return await self._build_status_text()

    async def _cmd_pause(self, _cmd: str, _args: list[str]) -> str:
        self.ops.set_pause(True)
        logger.warning("OPS PAUSE — new buys skipped (exits/brackets still active)")
        return "Paused: new buys skipped; exits/brackets still managed."

    async def _cmd_resume(self, _cmd: str, _args: list[str]) -> str:
        self.ops.set_pause(False)
        logger.info("OPS RESUME — new buys enabled")
        return "Resumed: new buys enabled."

    async def _cmd_pnl(self, _cmd: str, _args: list[str]) -> str:
        try:
            acct = await self.broker.get_account()
            cash = float(acct.cash)
            equity = float(acct.equity)
        except Exception:
            cash = equity = float(self.settings.account_equity)
        trades = day_trades_from_ledger()
        # Send via notifier template (user-initiated)
        try:
            await self.notifier.performance_report(
                trades=trades,
                paper_cash=cash,
                paper_equity=equity,
            )
        except Exception as exc:
            logger.warning("performance_report send failed: %s", exc)
            return f"/pnl send failed: {exc}"
        day_pnl = sum(float(t.get("pnl") or 0) for t in trades)
        return (
            f"Day P&L report sent ({len(trades)} closed). "
            f"Net {_fmt_pnl_brief(day_pnl)} | cash=${cash:.2f} equity=${equity:.2f}"
        )

    async def _cmd_kill(self, _cmd: str, _args: list[str]) -> str:
        logger.critical("TELEGRAM /kill — cancel, flatten allowlist, stop loop")
        self.ops.request_kill(liquidate=True)
        try:
            await self.executor.cancel_all()
        except Exception as exc:
            logger.warning("kill cancel_all: %s", exc)
        try:
            results = await self.executor.liquidate_all()
            logger.info("kill liquidate n=%s", len(results))
        except Exception as exc:
            logger.warning("kill liquidate_all: %s", exc)
        self.request_stop()
        return "KILL: orders cancelled, allowlist flattened, stopping loop."

    def _set_paper_trading_mode(self, paper: bool) -> None:
        """Mutate runtime paper flag shared by settings / broker / executor / risk.

        Does NOT rewrite .env — restart reloads PAPER_TRADING_MODE from env (safe default).
        Risk caps, allowlist, and post_only are unchanged.
        """
        paper_b = bool(paper)
        object.__setattr__(self.settings, "paper_trading_mode", paper_b)
        # Broker / executor / risk hold the same Settings instance — they read the flag live.
        broker = getattr(self, "broker", None)
        if broker is not None and hasattr(broker, "settings"):
            try:
                object.__setattr__(broker.settings, "paper_trading_mode", paper_b)
            except Exception:
                pass
            # Optional mirror if a broker caches a local bool
            if hasattr(broker, "paper"):
                try:
                    setattr(broker, "paper", paper_b)
                except Exception:
                    pass
            if hasattr(broker, "paper_trading_mode"):
                try:
                    setattr(broker, "paper_trading_mode", paper_b)
                except Exception:
                    pass

    async def _cmd_mode(self, _cmd: str, args: list[str]) -> str:
        """ /mode | /mode live | /mode paper — runtime PAPER/LIVE switch (confirm for live). """
        sub = (args[0].strip().lower() if args else "")
        try:
            acct = await self.broker.get_account()
            cash = float(acct.cash)
            equity = float(acct.equity)
        except Exception:
            cash = equity = float(self.settings.account_equity)

        if not sub:
            return format_mode_reply(
                paper=bool(self.settings.paper_trading_mode),
                cash=cash,
                equity=equity,
            )

        if sub == "live":
            if not bool(self.settings.paper_trading_mode):
                return (
                    format_mode_reply(paper=False, cash=cash, equity=equity)
                    + "\n(already LIVE)"
                )
            ttl = self.ops.arm_live_confirm(LIVE_CONFIRM_TTL_SECONDS)
            logger.warning(
                "OPS /mode live — pending confirm (ttl=%.0fs); still PAPER until /confirm_live",
                ttl,
            )
            return REPLY_MODE_LIVE_PENDING

        if sub == "paper":
            self.ops.clear_live_confirm()
            was_live = not bool(self.settings.paper_trading_mode)
            if was_live:
                self._set_paper_trading_mode(True)
                logger.warning("OPS MODE → PAPER (runtime); .env unchanged; restart returns to .env default")
                try:
                    self.trade_logger.log_event(
                        "mode_change",
                        {"to": "PAPER", "source": "telegram_/mode_paper", "persist_env": False},
                    )
                except Exception as exc:
                    logger.debug("mode_change log: %s", exc)
                try:
                    await self.notifier.mode_change_alert(ALERT_PAPER_RESTORED)
                except Exception as exc:
                    logger.warning("PAPER mode alert failed: %s", exc)
                return (
                    format_mode_reply(paper=True, cash=cash, equity=equity)
                    + f"\n{ALERT_PAPER_RESTORED}"
                )
            return (
                format_mode_reply(paper=True, cash=cash, equity=equity)
                + "\n(already PAPER; pending live confirm cleared)"
            )

        return "Usage: /mode | /mode live | /mode paper"

    async def _cmd_confirm_live(self, _cmd: str, _args: list[str]) -> str:
        """Second step after /mode live — flips runtime paper_trading_mode=False."""
        if not self.ops.has_pending_live_confirm():
            return REPLY_CONFIRM_EXPIRED
        self.ops.clear_live_confirm()
        if not bool(self.settings.paper_trading_mode):
            try:
                acct = await self.broker.get_account()
                cash, equity = float(acct.cash), float(acct.equity)
            except Exception:
                cash = equity = float(self.settings.account_equity)
            return (
                format_mode_reply(paper=False, cash=cash, equity=equity)
                + "\n(already LIVE)"
            )
        self._set_paper_trading_mode(False)
        logger.critical(
            "OPS MODE → LIVE (runtime) — real capital; caps/allowlist/post_only still enforced; "
            ".env NOT rewritten (restart returns to PAPER from .env)"
        )
        try:
            self.trade_logger.log_event(
                "mode_change",
                {
                    "to": "LIVE",
                    "source": "telegram_/confirm_live",
                    "persist_env": False,
                    "caps_intact": True,
                },
            )
        except Exception as exc:
            logger.debug("mode_change log: %s", exc)
        try:
            await self.notifier.mode_change_alert(ALERT_LIVE_ACTIVATED)
        except Exception as exc:
            logger.warning("LIVE mode alert failed: %s", exc)
        try:
            acct = await self.broker.get_account()
            cash, equity = float(acct.cash), float(acct.equity)
        except Exception:
            cash = equity = float(self.settings.account_equity)
        return (
            format_mode_reply(paper=False, cash=cash, equity=equity)
            + f"\n{ALERT_LIVE_ACTIVATED}"
        )


    async def _cmd_set_limit(self, _cmd: str, args: list[str]) -> str:
        """Hot-apply trade_cap / max_book to settings + .env (no process restart)."""
        from trading_bot.telegram_commands import (
            parse_set_limit_args,
            format_set_limit_reply,
            REPLY_SET_LIMIT_USAGE,
        )
        from trading_bot.config import (
            SET_LIMIT_ENV_TRADE_CAP,
            SET_LIMIT_ENV_MAX_BOOK,
            apply_set_limit_to_settings,
            upsert_env_vars,
            PROJECT_ROOT,
        )

        trade_cap, max_book, err = parse_set_limit_args(args)
        if err is not None:
            return err
        apply_set_limit_to_settings(self.settings, float(trade_cap), float(max_book))
        # Keep shared settings objects in sync when risk/executor hold same ref or copies
        for obj in (getattr(self, "risk", None), getattr(self, "executor", None)):
            if obj is not None and getattr(obj, "settings", None) is not None:
                apply_set_limit_to_settings(obj.settings, float(trade_cap), float(max_book))
        upsert_env_vars(
            {SET_LIMIT_ENV_TRADE_CAP: float(trade_cap), SET_LIMIT_ENV_MAX_BOOK: float(max_book)},
            path=PROJECT_ROOT / ".env",
        )
        logger.warning(
            "OPS SET_LIMIT trade_cap=$%.2f max_book=$%.2f (persisted to .env)",
            float(trade_cap),
            float(max_book),
        )
        return format_set_limit_reply(float(trade_cap), float(max_book))

    def _wire_telegram_commands(self) -> None:
        if not bool(getattr(self.settings, "telegram_commands_enabled", True)):
            logger.info("TELEGRAM_COMMANDS_ENABLED=false — command listener off")
            return
        token = self.settings.telegram_bot_token
        chat = self.settings.telegram_chat_id
        if not (token and chat):
            logger.info("Telegram commands skipped (token/chat unset)")
            return
        self._tg_listener = TelegramCommandListener(
            bot_token=token,
            chat_id=chat,
            enabled=True,
            handlers={
                "status": self._cmd_status,
                "pause": self._cmd_pause,
                "resume": self._cmd_resume,
                "pnl": self._cmd_pnl,
                "kill": self._cmd_kill,
                "mode": self._cmd_mode,
                "confirm_live": self._cmd_confirm_live,
                "set_limit": self._cmd_set_limit,
            },
        )

    async def _heartbeat_monitor(self) -> None:
        """15s tick/L2 stale monitor — reconnect with exponential backoff."""
        interval = min(5.0, max(2.0, float(getattr(self.settings, "stale_tick_seconds", 15) or 15) / 3.0))
        logger.info(
            "Stale-tick heartbeat armed (STALE_TICK_SECONDS=%.0f, check every %.1fs)",
            float(getattr(self.settings, "stale_tick_seconds", 15) or 15),
            interval,
        )
        while not self._stop.is_set():
            try:
                ensure = getattr(self.broker, "ensure_market_data_fresh", None)
                if callable(ensure):
                    await ensure()
            except Exception as exc:
                logger.warning("heartbeat monitor: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass


    def _on_leadlag_signal(self, snap) -> None:
        """Elevate BTC/ETH (+ correlated allowlisted) for faster POST_ONLY BUY eval."""
        try:
            elevated = snap.elevate_symbols(self.settings.symbol_list)
            if not elevated:
                return
            self._priority_symbols = elevated
            self._priority_event.set()
            logger.info(
                "PERP_LEADLAG priority elevate %s sweep=%s liq=%s",
                elevated,
                snap.recent_buy_sweep,
                snap.recent_liq_cascade,
            )
        except Exception as exc:
            logger.debug("leadlag signal handler: %s", exc)

    def _apply_active_params(self, params: Optional[dict]) -> None:
        """Hot-reload optimizer params into settings + sweet-spot engine (positions untouched)."""
        if not params:
            return
        try:
            # Ignore no_data / failed runs so we never loosen live gates accidentally
            if str(params.get("regime") or "") == "no_data":
                logger.info(
                    "ACTIVE_PARAMS skip apply (regime=no_data) — keep live rvol=%.2f min_tp=%.3f",
                    float(self.settings.rvol_breakout_mult),
                    float(self.settings.min_tp_pct),
                )
                self._active_params = dict(params)
                return
            rvol = float(params.get("rvol_breakout_mult", self.settings.rvol_breakout_mult))
            rsi_cap = float(params.get("rsi_buy_cap", self.settings.rsi_buy_cap))
            min_tp = float(params.get("min_tp_pct", self.settings.min_tp_pct))
            # floors / ceilings — never loosen past hard safety floors
            rvol = max(1.5, rvol)
            rsi_cap = min(72.0, rsi_cap)
            min_tp = max(0.025, min_tp)
            object.__setattr__(self.settings, "rvol_breakout_mult", rvol)
            object.__setattr__(self.settings, "rsi_buy_cap", rsi_cap)
            object.__setattr__(self.settings, "min_tp_pct", min_tp)
            eng = getattr(self.agent, "signal_engine", None)
            if eng is not None:
                if hasattr(eng, "rvol_breakout_mult"):
                    eng.rvol_breakout_mult = rvol
                if hasattr(eng, "min_tp_pct"):
                    eng.min_tp_pct = min_tp
            self._active_params = dict(params)
            logger.info(
                "ACTIVE_PARAMS applied regime=%s rvol=%.2f rsi_cap=%.1f min_tp=%.3f ts=%s",
                params.get("regime"),
                rvol,
                rsi_cap,
                min_tp,
                params.get("timestamp"),
            )
        except Exception as exc:
            logger.warning("apply_active_params failed: %s", exc)

    def _feed_frames_snapshot(self) -> dict:
        out = {}
        try:
            frames = getattr(self.feed, "_frames", None) or getattr(self.feed, "frames", None)
            if isinstance(frames, dict):
                for k, v in frames.items():
                    out[k] = v
        except Exception:
            pass
        return out

    async def startup(self) -> None:
        await self.broker.connect()
        logger.info(
            "Started broker=%s paper=%s equity_default=%.2f symbols=%s",
            self.broker.name,
            self.settings.paper_trading_mode,
            self.settings.account_equity,
            self.settings.symbol_list,
        )
        # Split concepts: live Coinbase bankroll (display) vs paper risk book (sizing)
        try:
            paper_acct = await self.broker.get_account()
            live_cash = live_eq = None
            getter = getattr(self.broker, "get_live_bankroll", None)
            if callable(getter):
                br = await getter(force=True)
                live_cash = float(br.get("cash") or 0)
                live_eq = float(br.get("equity") or 0)
            logger.info(
                "live bankroll cash=%.2f equity=%.2f | paper_book cash=%.2f equity=%.2f "
                "(caps $%.0f/trade $%.0f exposure)",
                live_cash if live_cash is not None else -1.0,
                live_eq if live_eq is not None else -1.0,
                float(paper_acct.cash),
                float(paper_acct.equity),
                float(self.settings.max_notional_per_trade_usd),
                float(self.settings.max_total_exposure_usd),
            )
        except Exception as exc:
            logger.warning("startup bankroll log skipped: %s", exc)

        logger.info(
            guardrails_startup_message(
                buy_dedupe_seconds=float(self.settings.buy_dedupe_seconds),
                post_only=bool(self.settings.post_only),
                max_spread_pct=float(self.settings.max_spread_pct),
                min_tp_pct=float(self.settings.min_tp_pct),
                maker_fee_rate=float(self.settings.maker_fee_rate),
                fee_to_target_mult=float(self.settings.fee_to_target_mult),
                allow_pyramiding=bool(self.settings.allow_pyramiding),
            )
        )
        logger.info(
            "INTELLIGENCE UPGRADES ARMED | "
            "1) L2 imbalance: enabled=%s band=%.3f%% min_ratio=%.2f | "
            "2) Regime: enabled=%s ADX_min=%.0f CHOP_max=%.0f (period=%d) | "
            "3) MTF align: enabled=%s EMA%d on 1h+4h (cache=%.0fs) | "
            "4) ATR sizing: enabled=%s period=%d ref_pct=%.2f%% "
            "formula=notional*base*(price*ATR_REF_PCT/ATR) clip[$%.0f,$%.0f] "
            "hard_caps=$%.0f/trade $%.0f exposure",
            bool(self.settings.l2_imbalance_enabled),
            float(self.settings.l2_imbalance_band_pct) * 100.0,
            float(self.settings.l2_imbalance_min_ratio),
            bool(self.settings.regime_filter_enabled),
            float(self.settings.adx_min),
            float(self.settings.chop_max),
            int(self.settings.adx_period),
            bool(self.settings.mtf_align_enabled),
            int(self.settings.htf_ema_period),
            float(self.settings.htf_cache_seconds),
            bool(self.settings.atr_sizing_enabled),
            int(self.settings.atr_sizing_period),
            float(self.settings.atr_ref_pct) * 100.0,
            float(self.settings.min_notional_usd),
            float(self.settings.max_notional_per_trade_usd),
            float(self.settings.max_notional_per_trade_usd),
            float(self.settings.max_total_exposure_usd),
        )

        logger.info(
            "INTEL V2 ARMED | "
            "1) PerpLeadLag: enabled=%s venues=%s sweep_mult=%.1f window=%.0fs | "
            "2) FundingOI: enabled=%s block>%.4f boost<%.4f oi_surge>%.1f%% poll=%.0fs | "
            "3) Optimizer: enabled=%s path=%s lookback=%dd | "
            "4) FailureBlacklist: enabled=%s lookback=%.0fh block=%.0fm",
            bool(self.settings.perp_leadlag_enabled),
            str(self.settings.perp_leadlag_venues),
            float(self.settings.perp_sweep_mult),
            float(self.settings.perp_sweep_window_sec),
            bool(self.settings.funding_oi_enabled),
            float(self.settings.funding_block_threshold),
            float(self.settings.funding_boost_threshold),
            float(self.settings.funding_oi_surge_pct) * 100.0,
            float(self.settings.funding_oi_poll_seconds),
            bool(self.settings.optimizer_enabled),
            str(self.settings.active_params_path),
            int(self.settings.optimizer_lookback_days),
            bool(self.settings.failure_blacklist_enabled),
            float(self.settings.failure_lookback_hours),
            float(self.settings.failure_block_minutes),
        )

        logger.info(
            "INTEL V3 ARMED | "
            "1) OnchainGuards: enabled=%s spike_mult=%.2f mint_boost=%.1f poll=%.0fs mock=%s | "
            "2) OrderReslicer: enabled=%s stall=%.0fs max=%d | "
            "3) Cointegration: enabled=%s z_entry=%.1f pairs=%s | "
            "4) SweepFade: enabled=%s lookback=%d abs_ratio=%.2f",
            bool(self.settings.onchain_guards_enabled),
            float(self.settings.onchain_inflow_spike_mult),
            float(self.settings.stablecoin_mint_boost),
            float(self.settings.onchain_poll_seconds),
            bool(self.settings.onchain_mock_mode) or (
                not str(self.settings.onchain_flow_url or "")
                and not str(self.settings.onchain_stable_url or "")
            ),
            bool(self.settings.order_reslice_enabled),
            float(self.settings.order_reslice_stall_sec),
            int(self.settings.order_reslice_max_times),
            bool(self.settings.cointegration_enabled),
            float(self.settings.coint_z_entry),
            str(self.settings.coint_pairs),
            bool(self.settings.sweep_fade_enabled),
            int(self.settings.sweep_fade_lookback),
            float(self.settings.sweep_fade_absorption_ratio),
        )

        # Ops controls: Telegram commands + stale-tick heartbeat (continuous loop only)
        if self.once:
            logger.info(
                "OPS CONTROLS | once-mode — skip telegram poll / heartbeat task "
                "(STALE_TICK_SECONDS=%.0f still enforced on refresh)",
                float(getattr(self.settings, "stale_tick_seconds", 15) or 15),
            )
        else:
            self._wire_telegram_commands()
            if self._tg_listener and self._tg_listener.configured:
                self._tasks.append(
                    asyncio.create_task(
                        self._tg_listener.run(self._stop), name="telegram_commands"
                    )
                )
                logger.info(
                    "OPS CONTROLS ARMED | telegram_commands=ON heartbeat_stale=%.0fs",
                    float(getattr(self.settings, "stale_tick_seconds", 15) or 15),
                )
            else:
                logger.info(
                    "OPS CONTROLS | telegram_commands=OFF heartbeat_stale=%.0fs",
                    float(getattr(self.settings, "stale_tick_seconds", 15) or 15),
                )
            self._tasks.append(
                asyncio.create_task(
                    self._heartbeat_monitor(), name="stale_tick_heartbeat"
                )
            )
            # Intel v2 background tasks (no unsolicited Telegram on deploy)
            try:
                await self.leadlag.start()
            except Exception as exc:
                logger.warning("PERP_LEADLAG start failed (degrade): %s", exc)
            try:
                await self.funding_oi.start()
            except Exception as exc:
                logger.warning("FUNDING_OI start failed (degrade): %s", exc)
            try:
                await self.onchain.start()
            except Exception as exc:
                logger.warning("ONCHAIN_GUARDS start failed (degrade): %s", exc)
            # Optional: paper/live reslice monitor for working POST_ONLY bids
            if bool(getattr(self.settings, "order_reslice_enabled", True)):
                self._tasks.append(
                    asyncio.create_task(self._reslice_monitor(), name="order_reslicer")
                )
            if bool(getattr(self.settings, "optimizer_enabled", True)):
                self._tasks.append(
                    asyncio.create_task(
                        nightly_optimizer_loop(
                            self._stop,
                            symbols=self.settings.symbol_list,
                            out_path=Path(self.settings.active_params_path),
                            get_feed_frames=self._feed_frames_snapshot,
                            on_complete=lambda p: self._apply_active_params(
                                {
                                    "rvol_breakout_mult": p.rvol_breakout_mult,
                                    "rsi_buy_cap": p.rsi_buy_cap,
                                    "min_tp_pct": p.min_tp_pct,
                                    "regime": p.regime,
                                    "timestamp": p.timestamp,
                                    "score": p.score,
                                }
                            ),
                        ),
                        name="nightly_optimizer",
                    )
                )
            # Stabilization: memory/stream maintenance + daily SQLite backup
            self._tasks.append(
                asyncio.create_task(self._maintenance_loop(), name="maintenance_gc")
            )
            self._tasks.append(
                asyncio.create_task(self._sqlite_backup_loop(), name="sqlite_backup")
            )
            logger.info(
                "STABILIZATION ARMED | maintenance=%.1fh sqlite_backup_keep=%s crash_alert=ON",
                float(getattr(self.settings, "maintenance_interval_hours", 6) or 6),
                int(getattr(self.settings, "sqlite_backup_keep", 7) or 7),
            )


    async def _run_maintenance_once(self) -> None:
        """gc.collect + clear/trim WS / market-data buffers."""
        import gc

        try:
            if hasattr(self.feed, "clear_stream_buffers"):
                self.feed.clear_stream_buffers()
        except Exception as exc:
            logger.warning("maintenance feed clear: %s", exc)
        try:
            broker = self.broker
            if hasattr(broker, "clear_stream_buffers"):
                broker.clear_stream_buffers()
        except Exception as exc:
            logger.warning("maintenance broker clear: %s", exc)
        try:
            if hasattr(self.leadlag, "clear_buffers"):
                self.leadlag.clear_buffers()
        except Exception as exc:
            logger.warning("maintenance leadlag clear: %s", exc)
        try:
            if hasattr(self.funding_oi, "clear_buffers"):
                self.funding_oi.clear_buffers()
        except Exception as exc:
            logger.warning("maintenance funding clear: %s", exc)
        try:
            if hasattr(self.onchain, "clear_buffers"):
                self.onchain.clear_buffers()
        except Exception as exc:
            logger.warning("maintenance onchain clear: %s", exc)
        collected = gc.collect()
        logger.info("MAINTENANCE: gc + stream buffer clear (gc=%s)", collected)

    async def _maintenance_loop(self) -> None:
        hours = float(getattr(self.settings, "maintenance_interval_hours", 6.0) or 6.0)
        interval = max(60.0, hours * 3600.0)
        logger.info(
            "MAINTENANCE armed interval=%.1fh (%.0fs)", hours, interval
        )
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                break
            try:
                await self._run_maintenance_once()
            except Exception:
                logger.exception("maintenance cycle failed")

    async def _run_sqlite_backup_once(self) -> None:
        keep = int(getattr(self.settings, "sqlite_backup_keep", 7) or 7)
        backup_dir = Path(
            getattr(self.settings, "sqlite_backup_dir", None)
            or (PROJECT_ROOT / "data" / "backups")
        )
        sources = default_db_sources(PROJECT_ROOT, self.settings.sqlite_path)
        written = await asyncio.to_thread(
            backup_sqlite_dbs, sources, backup_dir, keep=keep
        )
        if written:
            logger.info("SQLITE_BACKUP complete paths=%s", ",".join(str(p) for p in written))
        else:
            logger.warning("SQLITE_BACKUP produced no files (sources missing?)")

    async def _sqlite_backup_loop(self) -> None:
        """Daily SQLite backup at America/Chicago midnight (then every 24h)."""
        first = seconds_until_chicago_midnight()
        logger.info(
            "SQLITE_BACKUP armed next_chicago_midnight_in=%.0fs keep=%s dir=%s",
            first,
            getattr(self.settings, "sqlite_backup_keep", 7),
            getattr(self.settings, "sqlite_backup_dir", "data/backups"),
        )
        delay = first
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                break
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                break
            try:
                await self._run_sqlite_backup_once()
            except Exception:
                logger.exception("sqlite backup cycle failed")
            delay = 24 * 3600.0

    async def shutdown(self, liquidate: bool = False) -> None:
        logger.info("Shutting down (liquidate=%s)...", liquidate)
        self._stop.set()
        # Do not Telegram "KILL SWITCH" on normal paper restarts (LPM: fills/exits only)
        if not (self.settings.paper_trading_mode and getattr(self.notifier, "quiet", True)):
            try:
                await self.notifier.kill_switch(
                    f"Kill-switch / shutdown (liquidate={liquidate})"
                )
            except Exception as exc:
                logger.debug("kill-switch notify: %s", exc)
        else:
            logger.info("Paper quiet shutdown — skip kill-switch Telegram (liquidate=%s)", liquidate)
        try:
            await self.executor.cancel_all()
        except Exception as exc:
            logger.warning("cancel_all failed: %s", exc)
        if liquidate:
            try:
                results = await self.executor.liquidate_all()
                logger.info("Liquidation results: %s", [r.status.value for r in results])
            except Exception as exc:
                logger.warning("liquidate_all failed: %s", exc)
        try:
            await self.leadlag.stop()
        except Exception:
            pass
        try:
            await self.funding_oi.stop()
        except Exception:
            pass
        try:
            await self.onchain.stop()
        except Exception:
            pass
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        try:
            await self.broker.disconnect()
        except Exception as exc:
            logger.warning("broker disconnect: %s", exc)

        # Quiet daily P&L digest
        try:
            account = await self.broker.get_account()
            await self.notifier.daily_pnl_digest(
                day_pl=float(getattr(account, "day_pl", 0) or 0),
                day_pl_pct=float(getattr(account, "day_pl_pct", 0) or 0),
                equity=float(account.equity),
                fills=len(getattr(self.executor, "_results", {})),
            )
        except Exception as exc:
            logger.debug("daily pnl digest skipped: %s", exc)
        self.trade_logger.close()
        logger.info("Shutdown complete")


    def _sell_on_cooldown(self, symbol: str) -> bool:
        until = self._sell_cooldown_until.get(symbol, 0.0)
        return time.monotonic() < until

    def _arm_sell_cooldown(self, symbol: str) -> None:
        secs = float(getattr(self.settings, "sell_reject_cooldown_seconds", 60.0) or 60.0)
        self._sell_cooldown_until[symbol] = time.monotonic() + max(5.0, secs)

    def _check_hard_brackets(
        self,
        symbol: str,
        position,
        mark: float,
        *,
        rvol: Optional[float] = None,
    ) -> Optional[Decision]:
        """Force exit: structural SL / TP1 / TP2 / exhaustion / time-stop.

        volume_sweet_spot: ATR trail path is dead — never call trail updates.
        """
        if position is None or float(getattr(position, "qty", 0) or 0) <= 0:
            return None
        if mark <= 0:
            return None

        sweet = bool(getattr(self.settings, "is_sweet_spot", False))
        updater = getattr(self.broker, "update_position_brackets", None)

        if not sweet:
            # Legacy: update trailing high-water
            if callable(updater):
                try:
                    position = updater(symbol, mark_price=mark) or position
                except Exception as exc:
                    logger.debug("trail update skipped: %s", exc)
        # sweet-spot: deliberately do NOT call trail updates

        sl = getattr(position, "stop_loss", None)
        tp2 = getattr(position, "take_profit", None)
        tp1 = getattr(position, "take_profit_1", None)
        tp1_done = bool(getattr(position, "tp1_done", False))
        trail_dist = getattr(position, "trail_distance", None)
        hwm = getattr(position, "trail_high_water", None)
        opened_at = getattr(position, "opened_at", None)
        entry = float(getattr(position, "avg_entry_price", 0) or 0)
        qty = float(getattr(position, "qty", 0) or 0)
        initial_qty = float(getattr(position, "initial_qty", 0) or 0) or qty

        reason = None
        sell_qty = None

        if sweet:
            if sl is not None and mark <= float(sl):
                reason = "SL"
            else:
                # Compute TP1 from 1R if not stored
                if tp1 is None and entry > 0 and sl is not None:
                    risk = entry - float(sl)
                    if risk > 0:
                        tp1 = entry + float(self.settings.tp1_rr) * risk
                # Exhaustion near resistance / TP2 zone
                exh_mult = float(getattr(self.settings, "rvol_exhaustion_mult", 3.0) or 3.0)
                near_tp2 = tp2 is not None and mark >= float(tp2) * 0.998
                exhausted = (
                    isinstance(rvol, (int, float))
                    and float(rvol) >= exh_mult
                    and (near_tp2 or (tp2 is not None and mark >= float(tp2) * 0.99))
                )
                if not tp1_done and tp1 is not None and mark >= float(tp1):
                    reason = "TP1"
                    frac = float(getattr(self.settings, "tp1_fraction", 0.5) or 0.5)
                    sell_qty = initial_qty * frac
                elif (tp2 is not None and mark >= float(tp2)) or exhausted:
                    reason = "TP2" if not exhausted else "TP2"
                    if exhausted and not (tp2 is not None and mark >= float(tp2)):
                        reason = "TP2"
                    sell_qty = qty  # remainder
        else:
            tp = tp2
            if sl is not None and mark <= float(sl):
                reason = "ATR stop"
            elif tp is not None and mark >= float(tp):
                reason = "ATR take-profit"
            elif trail_dist is not None and hwm is not None:
                trail_stop = float(hwm) - float(trail_dist)
                if mark <= trail_stop:
                    reason = "trail"

        if reason is None and opened_at is not None:
            default_hold = 30 if sweet else 45
            max_hold = int(getattr(self.settings, "max_hold_minutes", default_hold) or default_hold)
            try:
                oa = opened_at
                if oa.tzinfo is None:
                    oa = oa.replace(tzinfo=timezone.utc)
                age_min = (utcnow() - oa).total_seconds() / 60.0
                if age_min >= max_hold:
                    reason = "time-stop"
            except Exception as exc:
                logger.debug("time-stop check failed: %s", exc)

        if reason is None:
            return None

        return Decision(
            action=Action.SELL,
            symbol=symbol,
            confidence=100.0,
            stop_loss=float(sl) if sl is not None else None,
            take_profit=float(tp2) if tp2 is not None else None,
            quantity=float(sell_qty) if sell_qty is not None else None,
            reasoning=reason,
        )

    async def _execute_decision(
        self,
        symbol: str,
        decision: Decision,
        *,
        account,
        entry: float,
        atr,
        position,
        open_exposure: float,
        open_position_count: int = 0,
    ) -> Decision:
        """Risk-size + submit + notify. Shared by agent path and hard brackets."""
        if self._sell_on_cooldown(symbol) and decision.action == Action.SELL:
            logger.info("SELL cooldown active for %s — skip", symbol)
            return Decision.hold(symbol, "sell cooldown")

        verdict = self.risk.evaluate(
            decision,
            account,
            entry_price=entry,
            atr=atr,
            open_exposure_usd=open_exposure,
            open_position=position,
            open_position_count=open_position_count,
        )
        logger.info(
            "RISK %s approved=%s qty=%.8f risk_pct=%.2f%% reason=%s",
            symbol,
            verdict.approved,
            verdict.sized_qty,
            verdict.risk_pct * 100,
            verdict.reason,
        )
        if not verdict.approved:
            if decision.action == Action.SELL:
                self._arm_sell_cooldown(symbol)
            if verdict.circuit_breaker_active and not self._cb_notified:
                self._cb_notified = True
                await self.notifier.circuit_breaker(verdict.reason)
            return decision

        order = self.risk.to_order_request(
            decision,
            verdict,
            limit_price=entry,
            paper=self.settings.paper_trading_mode,
        )
        if order is None:
            return decision

        # Forced exits prefer marketable limit at mark (already set)
        if decision.action == Action.SELL and decision.reasoning in (
            "ATR stop",
            "ATR take-profit",
            "trail",
            "time stop",
            "time-stop",
            "SL",
            "TP1",
            "TP2",
        ):
            # Always post-only limit in strategy path (never market/taker)
            upd = {"order_type": OrderType.LIMIT}
            if bool(getattr(self.settings, "post_only", True)):
                upd["post_only"] = True
            order = order.model_copy(update=upd)

        if decision.action == Action.BUY:
            try:
                self.state.record_buy_attempt(symbol)
            except Exception as exc:
                logger.debug("buy_dedupe stamp failed: %s", exc)

        result = await self.executor.submit(order)
        logger.info(
            "ORDER %s status=%s filled=%.4f avg=%s msg=%s",
            symbol,
            result.status.value,
            result.filled_qty,
            result.avg_fill_price,
            (result.message or "")[:80],
        )

        if result.status == OrderStatus.REJECTED:
            if decision.action == Action.SELL:
                self._arm_sell_cooldown(symbol)
            return decision

        if result.status in (OrderStatus.FILLED, OrderStatus.PARTIAL) and result.filled_qty > 0:
            # Clear cooldown on successful exit
            self._sell_cooldown_until.pop(symbol, None)
            fill_px = float(result.avg_fill_price or 0)
            fill_notional = float(result.filled_qty) * fill_px
            if fill_notional < 1.0:
                logger.info("Skip dust fill notify %s notional=%.6f", symbol, fill_notional)
                return decision

            paper = bool(getattr(result, "paper", self.settings.paper_trading_mode))
            if bool(getattr(self.settings, "post_only", True)) or bool(
                getattr(order, "post_only", False)
            ):
                fee_rate = float(getattr(self.settings, "maker_fee_rate", 0.005) or 0.005)
            else:
                fee_rate = float(getattr(self.settings, "taker_fee_rate", 0.009) or 0.009)

            # Paper book (simulated) + live Coinbase bankroll (display-only)
            cash_after = equity_after = None
            live_cash = live_equity = None
            try:
                acct_after = await self.broker.get_account()
                cash_after = float(getattr(acct_after, "cash", 0) or 0)
                equity_after = float(getattr(acct_after, "equity", 0) or 0)
            except Exception as exc:
                logger.debug("post-fill paper account read skipped: %s", exc)
            getter = getattr(self.broker, "get_live_bankroll", None)
            if callable(getter):
                try:
                    br = await getter()
                    live_cash = float(br.get("cash") or 0)
                    live_equity = float(br.get("equity") or 0)
                except Exception as exc:
                    logger.debug("post-fill live bankroll skipped: %s", exc)
            paper_cap = float(
                getattr(self.settings, "max_total_exposure_usd", 1000.0) or 1000.0
            )

            if order.side == OrderSide.BUY:
                buy_fee = fill_notional * fee_rate
                rt_fee = fill_notional * fee_rate * 2.0  # est. round-trip maker
                await self.notifier.trade_entry(
                    symbol,
                    "BUY",
                    result.filled_qty,
                    fill_px,
                    paper=paper,
                    fee=buy_fee,
                    cash_after=cash_after,
                    equity_after=equity_after,
                    live_cash=live_cash,
                    live_equity=live_equity,
                    paper_cap=paper_cap,
                    fee_rate=fee_rate,
                    stop_loss=verdict.stop_loss or decision.stop_loss,
                    take_profit=verdict.take_profit or decision.take_profit,
                    entry_reason=decision.reasoning,
                    est_round_trip_fee=rt_fee,
                    post_only=bool(getattr(order, "post_only", True)),
                )
                updater = getattr(self.broker, "update_position_brackets", None)
                if callable(updater):
                    try:
                        # Sweet-spot: store structural SL/TP1/TP2; NEVER set trail_distance
                        kw = {
                            "stop_loss": verdict.stop_loss or decision.stop_loss,
                            "take_profit": verdict.take_profit or decision.take_profit,
                            "mark_price": fill_px,
                        }
                        if self.settings.is_sweet_spot:
                            entry = fill_px
                            sl = kw["stop_loss"]
                            if entry and sl and entry > float(sl):
                                risk = entry - float(sl)
                                kw["take_profit_1"] = entry + float(self.settings.tp1_rr) * risk
                            kw["trail_distance"] = None  # explicit dead trail
                            kw["tp1_done"] = False
                            kw["initial_qty"] = float(result.filled_qty)
                            kw["entry_reason"] = decision.reasoning
                        elif verdict.trailing_stop_distance:
                            kw["trail_distance"] = verdict.trailing_stop_distance
                            kw["trail_high_water"] = fill_px
                        updater(symbol, **kw)
                    except Exception as exc:
                        logger.debug("post-buy bracket store: %s", exc)
                self.state.add_trade_memory(
                    symbol=symbol,
                    action="BUY",
                    outcome="entry",
                    reason=decision.reasoning,
                    pnl=None,
                )
            else:
                entry_px = float(getattr(position, "avg_entry_price", 0) or 0) if position else 0.0
                qty_f = float(result.filled_qty)
                entry_notional = (entry_px * qty_f) if entry_px > 0 else 0.0
                entry_fee = entry_notional * fee_rate
                buy_cost_with_fees = entry_notional + entry_fee if entry_px > 0 else None
                exit_fee = fill_notional * fee_rate
                pnl = None
                if buy_cost_with_fees is not None:
                    # Full round-trip $ P&L: net cash in - buy cost w/ entry fee
                    pnl = (fill_notional - exit_fee) - buy_cost_with_fees
                outcome = "exit"
                is_stop = False
                reason_l = (decision.reasoning or "").lower()
                if (
                    "stop" in reason_l
                    or reason_l in ("atr stop", "trail", "sl", "time-stop", "time stop")
                ):
                    is_stop = True
                if decision.stop_loss is not None and fill_px > 0:
                    if fill_px <= float(decision.stop_loss) * 1.002:
                        is_stop = True
                if is_stop:
                    outcome = "stop_loss"
                    locked = self.state.record_stop_loss(symbol)
                    self.state.record_post_stop_cooldown(symbol)
                    # Post-mortem snapshot → trade_failures + temporary blacklist
                    try:
                        prev = self._last_obs.get(symbol)
                        snap = build_failure_snapshot(
                            symbol=symbol,
                            indicators=getattr(prev, "indicators", None) if prev else None,
                            htf=getattr(prev, "htf", None) if prev else None,
                            quote=getattr(prev, "quote", None) if prev else None,
                            recent_bars=list(getattr(prev, "recent_bars", []) or []) if prev else [],
                        )
                        tag = classify_failure(snap)
                        self.state.record_trade_failure(
                            symbol=symbol,
                            signature_tag=tag,
                            snapshot=snap,
                            block_minutes=float(
                                getattr(self.settings, "failure_block_minutes", 45.0) or 45.0
                            ),
                        )
                        logger.info("POSTMORTEM %s tag=%s", symbol, tag)
                    except Exception as pm_exc:
                        logger.debug("postmortem skipped: %s", pm_exc)
                    # Quiet mode: Why folded into trade_exit only (no separate STOP LOSS ping)
                    if not getattr(self.notifier, "quiet", True):
                        await self.notifier.stop_loss(
                            symbol,
                            f"Stop-loss exit {symbol} qty={result.filled_qty}"
                            + ("; revenge lockout armed" if locked else ""),
                        )
                else:
                    self.state.record_non_stop_outcome(symbol)
                    if "take" in reason_l or "tp" in reason_l or reason_l == "atr take-profit":
                        if not getattr(self.notifier, "quiet", True):
                            await self.notifier.take_profit(
                                symbol,
                                f"TP exit {symbol} qty={result.filled_qty}",
                            )
                await self.notifier.trade_exit(
                    symbol,
                    "SELL",
                    result.filled_qty,
                    reason=decision.reasoning,
                    price=fill_px,
                    paper=paper,
                    fee=exit_fee,
                    entry_price=entry_px or None,
                    buy_cost_with_fees=buy_cost_with_fees,
                    pnl=pnl,
                    cash_after=cash_after,
                    equity_after=equity_after,
                    live_cash=live_cash,
                    live_equity=live_equity,
                    paper_cap=paper_cap,
                    fee_rate=fee_rate,
                    post_only=bool(getattr(order, "post_only", True)),
                )
                # After TP1: mark done + move stop to breakeven + round-trip maker cushion
                if (
                    self.settings.is_sweet_spot
                    and (decision.reasoning or "") == "TP1"
                ):
                    updater = getattr(self.broker, "update_position_brackets", None)
                    if callable(updater) and entry_px > 0:
                        try:
                            fee_cushion = entry_px * float(self.settings.maker_fee_rate) * 2.0
                            be_stop = entry_px + fee_cushion
                            updater(
                                symbol,
                                stop_loss=be_stop,
                                tp1_done=True,
                                trail_distance=None,
                            )
                        except Exception as exc:
                            logger.debug("post-TP1 bracket update: %s", exc)
                if pnl is not None:
                    try:
                        from scripts.paper_ledger import update_last_fill_pnl
                        update_last_fill_pnl(symbol, "SELL", float(pnl))
                    except Exception as ledger_exc:
                        logger.debug("ledger pnl update skipped: %s", ledger_exc)
                self.state.add_trade_memory(
                    symbol=symbol,
                    action="SELL",
                    outcome=outcome,
                    reason=decision.reasoning,
                    pnl=pnl,
                )
        return decision


    async def _reslice_monitor(self) -> None:
        """Poll L2 for working POST_ONLY bids; re-slice closer on queue stall."""
        logger.info(
            "ORDER_RESLICER monitor armed stall=%.0fs max=%d",
            float(getattr(self.settings, "order_reslice_stall_sec", 10) or 10),
            int(getattr(self.settings, "order_reslice_max_times", 3) or 3),
        )
        while not self._stop.is_set():
            try:
                for wo in list(self.reslicer.open_orders()):
                    try:
                        book = await self.broker.get_l2_book(wo.symbol)
                        action = self.reslicer.on_book(
                            wo.order_id,
                            book.get("bids") or [],
                            book.get("asks") or [],
                        )
                        if action.action == "reslice" and action.new_price is not None:
                            try:
                                await self.broker.cancel_order(wo.order_id)
                            except Exception as exc:
                                logger.debug("reslice cancel %s: %s", wo.order_id, exc)
                            try:
                                self.state.record_reslice_event(
                                    symbol=wo.symbol,
                                    order_id=wo.order_id,
                                    old_price=float(wo.limit_price),
                                    new_price=float(action.new_price),
                                    depth_ahead=float(action.depth_ahead),
                                    reason=action.reason,
                                )
                            except Exception:
                                pass
                            logger.info(
                                "RESLICE %s order=%s -> %.8f (%s)",
                                wo.symbol,
                                wo.order_id,
                                action.new_price,
                                action.reason,
                            )
                            # Re-place is handled by strategy on next cycle in paper;
                            # register updated working price already applied in reslicer.
                        elif action.action == "fill":
                            logger.info(
                                "RESLICE queue cleared %s order=%s",
                                wo.symbol,
                                wo.order_id,
                            )
                    except Exception as exc:
                        logger.debug("reslice tick %s: %s", wo.order_id, exc)
            except Exception as exc:
                logger.warning("ORDER_RESLICER loop error (degrade): %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=2.0)
                break
            except asyncio.TimeoutError:
                pass

    async def cycle_symbol(self, symbol: str) -> Optional[Decision]:
        account = await self.broker.get_account()
        tripped = self.risk.update_drawdown(account)
        if tripped and self.risk.circuit_breaker_active and not self._cb_notified:
            self._cb_notified = True
            await self.notifier.circuit_breaker(
                self.risk._circuit_reason or "circuit breaker active"
            )

        # Macro pause — HOLD and skip new entries
        paused, ev = await self.macro.check_pause()
        if paused and ev is not None:
            msg = (
                f"MACRO_PAUSE ±{self.settings.macro_pause_minutes}m around "
                f"'{ev.title}' @ {ev.timestamp.isoformat()}"
            )
            logger.warning("%s — HOLD %s", msg, symbol)
            self.trade_logger.log_event("macro_pause", {"detail": msg, "event": ev.title}, symbol)
            if self.macro.should_notify(ev):
                await self.notifier.macro_pause(ev.title, msg)
            return Decision.hold(symbol, msg)

        # Revenge lockout
        if self.state.is_locked_out(symbol):
            rem = self.state.lockout_remaining_seconds(symbol)
            reason = f"REVENGE_LOCKOUT active ({rem:.0f}s remaining)"
            logger.warning("%s — HOLD %s", reason, symbol)
            self.trade_logger.log_event("revenge_lockout", {"remaining_s": rem}, symbol)
            return Decision.hold(symbol, reason)

        indicators = await self.feed.ensure_fresh(symbol)
        try:
            htf = await self.feed.ensure_htf(symbol)
        except Exception as exc:
            logger.debug("HTF refresh skipped for %s: %s", symbol, exc)
            htf = self.feed.get_htf(symbol)
        try:
            await self.feed.refresh_entry_5m(symbol)
        except Exception:
            pass

        quote = self.feed.get_quote(symbol)
        if quote is None:
            try:
                quote = await self.broker.get_quote(symbol)
            except Exception:
                quote = None

        # L2 depth imbalance → indicators.extras (BUY gate in sweet-spot engine)
        if indicators is not None and bool(getattr(self.settings, "l2_imbalance_enabled", False)):
            try:
                ratio = await self.feed.fetch_l2_imbalance(symbol)
                extras = dict(indicators.extras or {})
                extras["l2_imbalance_ratio"] = ratio
                indicators = indicators.model_copy(update={"extras": extras})
            except Exception as exc:
                logger.debug("L2 attach skipped for %s: %s", symbol, exc)

        # Phase 1: attach CVD / liq notional snapshots (non-blocking reads)
        if indicators is not None and (
            bool(getattr(self.settings, "cvd_divergence_enabled", False))
            or bool(getattr(self.settings, "liq_sweep_required", False))
        ):
            try:
                extras = dict(indicators.extras or {})
                if hasattr(self.leadlag, "get_cvd_snapshot"):
                    extras["cvd_snapshot"] = self.leadlag.get_cvd_snapshot(symbol)
                if hasattr(self.leadlag, "get_liq_snapshot"):
                    extras["liq_snapshot"] = self.leadlag.get_liq_snapshot(symbol)
                # Prefer 5m entry bar open/close for CVD divergence when available
                try:
                    frame5 = getattr(self.feed, "get_entry_5m_frame", None)
                    df5 = frame5(symbol) if callable(frame5) else None
                    if df5 is None:
                        df5 = getattr(self.feed, "_entry_5m", {}).get(symbol)
                    if df5 is not None and hasattr(df5, "empty") and not df5.empty:
                        row = df5.iloc[-1]
                        extras["setup_bar_open"] = float(row["open"])
                        extras["setup_bar_close"] = float(row["close"])
                except Exception:
                    pass
                indicators = indicators.model_copy(update={"extras": extras})
            except Exception as exc:
                logger.debug("CVD/liq attach skipped for %s: %s", symbol, exc)

        position = await self.broker.get_position(symbol)
        memory = self.state.get_trade_memory(self.settings.trade_memory_size)

        mark = (
            quote.mid
            if quote
            else (indicators.close if indicators and indicators.close else 0.0)
        )

        open_exposure = 0.0
        open_position_count = 0
        try:
            for pos in await self.broker.get_positions():
                sym = getattr(pos, "symbol", "")
                if not self.settings.is_allowlisted(sym):
                    continue
                qty = float(getattr(pos, "qty", 0) or 0)
                if abs(qty) > 1e-12:
                    open_position_count += 1
                open_exposure += abs(float(getattr(pos, "market_value", 0) or 0))
        except Exception as exc:
            logger.debug("exposure scan skipped: %s", exc)

        # --- Hard bracket / time-stop BEFORE agent decide ---
        rvol = None
        try:
            vr = (indicators.extras or {}).get("volume_ratio") if indicators else None
            if isinstance(vr, (int, float)):
                rvol = float(vr)
        except Exception:
            rvol = None
        forced = self._check_hard_brackets(
            symbol, position, float(mark or 0), rvol=rvol
        )
        if forced is not None:
            self.trade_logger.log_decision(forced)
            entry = (
                quote.bid
                if quote and quote.bid > 0
                else float(mark or indicators.close or 0)
            )
            if entry > 0:
                # Exits always allowed even if circuit breaker tripped
                return await self._execute_decision(
                    symbol,
                    forced,
                    account=account,
                    entry=entry,
                    atr=indicators.atr,
                    position=position,
                    open_exposure=open_exposure,
                    open_position_count=open_position_count,
                )
            return forced

        recent_bars = []
        try:
            frame = self.feed.get_frame(symbol)
            if frame is not None and not frame.empty:
                from trading_bot.models import Bar as _Bar

                tail = frame.tail(40)
                for ts, row in tail.iterrows():
                    ts_dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else utcnow()
                    if getattr(ts_dt, "tzinfo", None) is None:
                        ts_dt = ts_dt.replace(tzinfo=timezone.utc)
                    recent_bars.append(
                        _Bar(
                            symbol=symbol,
                            timestamp=ts_dt,
                            open=float(row["open"]),
                            high=float(row["high"]),
                            low=float(row["low"]),
                            close=float(row["close"]),
                            volume=float(row["volume"]),
                            vwap=float(row["vwap"]) if "vwap" in row and row["vwap"] == row["vwap"] else None,
                        )
                    )
        except Exception as exc:
            logger.debug("recent_bars build skipped: %s", exc)

        obs = AgentObservation(
            symbol=symbol,
            indicators=indicators,
            quote=quote,
            position=position,
            account=account,
            recent_bars=recent_bars,
            entry_timeframe=self.settings.bar_timeframe,
            htf=htf,
            trade_memory=memory,
            macro_paused=False,
            revenge_locked=False,
        )
        self._last_obs[symbol] = obs
        self.trade_logger.log_signal(
            symbol,
            {
                "indicators": indicators.model_dump(mode="json"),
                "htf": htf.model_dump(mode="json") if htf else None,
                "equity": account.equity,
            },
        )

        decision = await self.agent.decide(obs)
        self.trade_logger.log_decision(decision)

        if decision.action == Action.HOLD:
            return decision

        # Flat + agent SELL → suppress (no shorting); cooldown to avoid spam
        if decision.action == Action.SELL and (
            position is None or float(getattr(position, "qty", 0) or 0) <= 0
        ):
            if not self._sell_on_cooldown(symbol):
                logger.info("Agent SELL while flat on %s — suppress", symbol)
                self._arm_sell_cooldown(symbol)
            return Decision.hold(symbol, "flat — suppress SELL")

        # Already long + agent BUY → suppress pyramid early
        if (
            decision.action == Action.BUY
            and position is not None
            and float(getattr(position, "qty", 0) or 0) > 0
            and not bool(getattr(self.settings, "allow_pyramiding", False))
        ):
            logger.info("Already long %s — skip pyramid BUY", symbol)
            return Decision.hold(symbol, "already long — no pyramid")


        # Ops pause — skip NEW buys; exits/brackets already handled above
        if decision.action == Action.BUY and self.ops.paused:
            logger.info("OPS PAUSED — skip BUY %s", symbol)
            return Decision.hold(symbol, "ops paused — skip new buys")

        # Structural guardrail: buy dedupe window (same symbol)
        if decision.action == Action.BUY:
            window = float(getattr(self.settings, "buy_dedupe_seconds", 300) or 300)
            elapsed = self.state.seconds_since_last_buy(symbol)
            ok_dd, dd_reason = check_buy_dedupe(elapsed, window_seconds=window)
            if not ok_dd:
                logger.info("SKIP BUY %s | %s", symbol, dd_reason)
                return Decision.hold(symbol, dd_reason)

        # Structural guardrail: spread filter (need live bid/ask)
        if decision.action == Action.BUY:
            bid = ask = None
            if quote is not None:
                bid = getattr(quote, "bid", None)
                ask = getattr(quote, "ask", None)
            ok_sp, sp_reason = check_spread(
                bid,
                ask,
                max_spread_pct=float(getattr(self.settings, "max_spread_pct", 0.001) or 0.001),
            )
            if not ok_sp:
                logger.info("SKIP BUY %s | %s", symbol, sp_reason)
                return Decision.hold(symbol, sp_reason)

        # Post-stop / trail cooldown — block new BUY on this symbol
        if decision.action == Action.BUY and self.state.in_post_stop_cooldown(symbol):
            rem = self.state.post_stop_cooldown_remaining(symbol)
            reason = f"post_stop_cooldown ({rem:.0f}s remaining)"
            logger.info("SKIP BUY %s | %s", symbol, reason)
            return Decision.hold(symbol, reason)

        # Funding/OI positioning filter
        if decision.action == Action.BUY and bool(
            getattr(self.settings, "funding_oi_enabled", True)
        ):
            allow, conf_delta, ftag = self.funding_oi.evaluate(
                symbol, recent_bars, confidence=float(decision.confidence or 0)
            )
            if not allow:
                logger.info("SKIP BUY %s | %s", symbol, ftag or "funding_long_trap")
                return Decision.hold(symbol, ftag or "funding_long_trap")
            if conf_delta and ftag:
                decision = decision.model_copy(
                    update={
                        "confidence": min(100.0, float(decision.confidence or 0) + conf_delta),
                        "reasoning": (decision.reasoning or "") + f"; {ftag}",
                    }
                )
                logger.info("BOOST BUY %s | %s conf+=%.1f", symbol, ftag, conf_delta)

        # On-chain exchange inflow / stablecoin mint guards
        if decision.action == Action.BUY and bool(
            getattr(self.settings, "onchain_guards_enabled", True)
        ):
            try:
                oc = self.onchain.evaluate(
                    symbol, confidence=float(decision.confidence or 0)
                )
                if not oc.allow_buy:
                    try:
                        self.state.record_onchain_event(
                            event_type="skip",
                            symbol=symbol,
                            detail={"reason": oc.skip_reason},
                        )
                    except Exception:
                        pass
                    logger.info("SKIP BUY %s | %s", symbol, oc.skip_reason or "onchain: exchange_inflow_spike")
                    return Decision.hold(symbol, oc.skip_reason or "onchain: exchange_inflow_spike")
                if oc.confidence_delta and oc.boost_tag:
                    decision = decision.model_copy(
                        update={
                            "confidence": min(
                                100.0,
                                float(decision.confidence or 0) + float(oc.confidence_delta),
                            ),
                            "reasoning": (decision.reasoning or "") + f"; {oc.boost_tag}",
                        }
                    )
                    try:
                        self.state.record_onchain_event(
                            event_type="boost",
                            symbol=symbol,
                            detail={"tag": oc.boost_tag, "delta": oc.confidence_delta},
                        )
                    except Exception:
                        pass
                    logger.info(
                        "BOOST BUY %s | %s conf+=%.1f",
                        symbol,
                        oc.boost_tag,
                        oc.confidence_delta,
                    )
            except Exception as exc:
                logger.warning("ONCHAIN evaluate failed (degrade): %s", exc)

        # Update cointegration prices; optional alt BUY candidate when flat HOLD
        try:
            px = float(
                getattr(quote, "mid", None)
                or getattr(quote, "last", None)
                or (indicators.close if indicators else 0)
                or 0
            )
            if px > 0:
                self.cointegration.update_price(symbol, px)
        except Exception as exc:
            logger.debug("coint price update: %s", exc)

        if (
            decision.action == Action.HOLD
            and bool(getattr(self.settings, "cointegration_enabled", True))
            and (position is None or float(getattr(position, "qty", 0) or 0) <= 0)
        ):
            try:
                for sig in self.cointegration.scan():
                    if sig.symbol != symbol:
                        continue
                    decision = Decision(
                        action=Action.BUY,
                        symbol=symbol,
                        confidence=float(sig.confidence),
                        stop_loss=None,
                        take_profit=None,
                        reasoning=sig.reason,
                    )
                    try:
                        self.state.record_coint_signal(
                            symbol=symbol,
                            pair=f"{sig.pair[0]}/{sig.pair[1]}",
                            zscore=float(sig.zscore),
                            correlation=float(sig.correlation),
                            confidence=float(sig.confidence),
                            reason=sig.reason,
                        )
                    except Exception:
                        pass
                    logger.info(
                        "ENTRY BUY %s | %s z=%.2f conf=%.1f",
                        symbol,
                        sig.reason,
                        sig.zscore,
                        sig.confidence,
                    )
                    break
            except Exception as exc:
                logger.warning("COINT scan failed (degrade): %s", exc)

        # Sweep-fade alt BUY: sell-side liquidity sweep + bid absorption
        if (
            decision.action == Action.HOLD
            and bool(getattr(self.settings, "sweep_fade_enabled", True))
            and (position is None or float(getattr(position, "qty", 0) or 0) <= 0)
        ):
            try:
                bars = None
                try:
                    bars = self.feed.get_bars_df(symbol)
                except Exception:
                    bars = None
                if bars is None:
                    try:
                        raw = await self.broker.get_bars(symbol, limit=80)
                        import pandas as pd
                        bars = pd.DataFrame(
                            [
                                {
                                    "open": float(b.open),
                                    "high": float(b.high),
                                    "low": float(b.low),
                                    "close": float(b.close),
                                    "volume": float(getattr(b, "volume", 0) or 0),
                                }
                                for b in (raw or [])
                            ]
                        )
                    except Exception:
                        bars = None
                bids = asks = []
                mid = None
                try:
                    book = await self.broker.get_l2_book(symbol)
                    bids = book.get("bids") or []
                    asks = book.get("asks") or []
                    mid = book.get("mid")
                except Exception:
                    pass
                if bars is not None and not getattr(bars, "empty", True):
                    sf = self.sweep_fade.evaluate(
                        symbol, bars, bids=bids, asks=asks, mid=mid
                    )
                    if sf.entry:
                        decision = Decision(
                            action=Action.BUY,
                            symbol=symbol,
                            confidence=float(sf.confidence),
                            stop_loss=float(sf.swing_low) * 0.997 if sf.swing_low else None,
                            take_profit=None,
                            reasoning=sf.reason,
                        )
                        logger.info(
                            "ENTRY BUY %s | %s abs=%.2f conf=%.1f",
                            symbol,
                            sf.reason,
                            sf.absorption_ratio,
                            sf.confidence,
                        )
                    else:
                        logger.debug("sweep_fade %s | %s", symbol, sf.reason)
            except Exception as exc:
                logger.warning("SWEEP_FADE evaluate failed (degrade): %s", exc)

        # Failure-pattern blacklist (post-mortem signatures)
        if decision.action == Action.BUY and bool(
            getattr(self.settings, "failure_blacklist_enabled", True)
        ):
            live_snap = build_failure_snapshot(
                symbol=symbol,
                indicators=indicators,
                htf=htf,
                quote=quote,
                recent_bars=recent_bars,
            )
            recent_fails = self.state.get_active_failure_blacklist(
                lookback_hours=float(
                    getattr(self.settings, "failure_lookback_hours", 72.0) or 72.0
                ),
                symbol=symbol,
            )
            blocked, bl_reason = check_failure_blacklist(live_snap, recent_fails)
            if blocked:
                logger.info("SKIP BUY %s | %s", symbol, bl_reason)
                return Decision.hold(symbol, bl_reason)

        # Soft concurrent cap before sizing (risk also enforces)
        if decision.action == Action.BUY:
            max_c = int(getattr(self.settings, "max_concurrent_positions", 2) or 2)
            pos_qty = float(getattr(position, "qty", 0) or 0) if position else 0.0
            if pos_qty <= 0 and open_position_count >= max_c:
                reason = (
                    f"max concurrent positions {max_c} reached "
                    f"(open={open_position_count})"
                )
                logger.info("SKIP BUY %s | %s", symbol, reason)
                return Decision.hold(symbol, reason)

        # Circuit breaker blocks new entries (exits still allowed above)
        if self.risk.circuit_breaker_active and decision.action == Action.BUY:
            logger.warning("Circuit breaker — skip entry %s", symbol)
            return Decision.hold(symbol, "circuit breaker — skip entry")

        entry = (
            quote.ask
            if decision.action == Action.BUY and quote
            else quote.bid
            if decision.action == Action.SELL and quote
            else indicators.close
        )
        if not entry or entry <= 0:
            logger.warning("No valid entry price for %s — skip", symbol)
            return decision

        return await self._execute_decision(
            symbol,
            decision,
            account=account,
            entry=entry,
            atr=indicators.atr,
            position=position,
            open_exposure=open_exposure,
            open_position_count=open_position_count,
        )

    async def run_loop(self) -> None:
        await self.startup()
        try:
            while not self._stop.is_set():
                # Drain lead-lag priority symbols first (faster POST_ONLY BUY eval)
                if self._priority_event.is_set() or self._priority_symbols:
                    self._priority_event.clear()
                    pri = list(self._priority_symbols)
                    self._priority_symbols = []
                    for symbol in pri:
                        if self._stop.is_set():
                            break
                        try:
                            logger.info(
                                "PERP_LEADLAG fast-cycle %s (pre-poll elevate)", symbol
                            )
                            await self.cycle_symbol(symbol)
                        except Exception:
                            logger.exception("priority cycle error for %s", symbol)
                for symbol in self.settings.symbol_list:
                    if self._stop.is_set():
                        break
                    try:
                        await self.cycle_symbol(symbol)
                    except Exception:
                        logger.exception("cycle error for %s", symbol)
                if self.once:
                    logger.info("--once complete")
                    break
                # Interruptible poll: wake early on lead-lag sweep
                try:
                    poll = float(self.settings.agent_poll_seconds)
                    done, pending = await asyncio.wait(
                        [
                            asyncio.create_task(self._stop.wait()),
                            asyncio.create_task(self._priority_event.wait()),
                        ],
                        timeout=poll,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for t in pending:
                        t.cancel()
                except Exception:
                    try:
                        await asyncio.wait_for(
                            self._stop.wait(),
                            timeout=self.settings.agent_poll_seconds,
                        )
                    except asyncio.TimeoutError:
                        pass
        finally:
            # shutdown is invoked by main() with liquidate flag
            pass


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Agentic Day Trading Bot (paper-first)")
    p.add_argument("--dry-run", action="store_true", help="Use Mock broker (no credentials)")
    p.add_argument(
        "--live",
        action="store_true",
        help="Explicit live opt-in (sets PAPER_TRADING_MODE=false). Default remains paper.",
    )
    p.add_argument("--once", action="store_true", help="Run a single cycle then exit")
    p.add_argument(
        "--liquidate-on-kill",
        action="store_true",
        help="Liquidate positions on SIGINT/SIGTERM",
    )
    p.add_argument("--symbols", type=str, default=None, help="Comma-separated symbols")
    p.add_argument("--log-level", type=str, default=None)
    return p.parse_args(argv)


async def async_main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    # Apply CLI overrides via env-style mutation before settings load
    import os

    if args.dry_run:
        os.environ["DRY_RUN"] = "true"
        os.environ["BROKER"] = "mock"
    if args.liquidate_on_kill:
        os.environ["LIQUIDATE_ON_KILL"] = "true"
    if args.symbols:
        os.environ["SYMBOLS"] = args.symbols
    if args.log_level:
        os.environ["LOG_LEVEL"] = args.log_level

    if args.dry_run:
        os.environ["DRY_RUN"] = "true"
        os.environ["BROKER"] = "mock"
    if getattr(args, "live", False):
        # Explicit live opt-in only — never the default
        os.environ["PAPER_TRADING_MODE"] = "false"
        logger.critical("LIVE OPT-IN: --live set PAPER_TRADING_MODE=false")

    settings = reload_settings()
    # Paper remains the safe default. Live only when PAPER_TRADING_MODE=false via env or --live.
    if settings.paper_trading_mode:
        logger.info("Paper trading mode ON (safe default)")
    else:
        logger.critical(
            "PAPER_TRADING_MODE=false — LIVE trading enabled (explicit opt-in). "
            "Allowlist + $100/$1000 caps still enforced."
        )

    setup_logging(settings.log_level)
    app = TradingApp(settings, once=args.once)

    loop = asyncio.get_running_loop()
    liquidate = settings.liquidate_on_kill or args.liquidate_on_kill

    def _handle_signal(sig: signal.Signals) -> None:
        logger.warning("Received %s — initiating kill-switch", sig.name)
        app.request_stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig)
        except NotImplementedError:
            # Windows
            signal.signal(sig, lambda *_: app.request_stop())

    def _asyncio_exception_handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
        """Log + urgent Telegram for unhandled task exceptions (no secrets)."""
        exc = context.get("exception")
        msg = context.get("message", "Unhandled asyncio exception")
        if exc is None:
            logger.error("Asyncio handler: %s context=%s", msg, {k: context.get(k) for k in context if k != "exception"})
            return
        logger.error("Asyncio task exception: %s", format_crash_alert(exc), exc_info=exc)

        async def _alert() -> None:
            await notify_critical_crash(app.notifier, exc if isinstance(exc, BaseException) else RuntimeError(str(exc)))

        try:
            loop.create_task(_alert())
        except Exception:
            logger.warning("Could not schedule crash alert task")

    try:
        loop.set_exception_handler(_asyncio_exception_handler)
    except Exception as exc:
        logger.warning("set_exception_handler failed: %s", exc)

    exit_code = 0
    fatal_exc: Optional[BaseException] = None
    try:
        await app.run_loop()
    except Exception as exc:
        fatal_exc = exc
        logger.exception("Fatal error")
        exit_code = 1
    finally:
        if fatal_exc is not None:
            try:
                await notify_critical_crash(app.notifier, fatal_exc)
            except Exception:
                logger.warning("Fatal crash notify failed")
        if app.ops.kill_requested:
            liquidate = liquidate or bool(app.ops.kill_liquidate)
        await app.shutdown(liquidate=liquidate)
    return exit_code


def main(argv: Optional[list[str]] = None) -> None:
    """Entrypoint with global unhandled-exception → CRITICAL Telegram → non-zero exit."""
    code = 1
    try:
        code = asyncio.run(async_main(argv))
    except KeyboardInterrupt:
        code = 130
    except SystemExit as se:
        raise se
    except BaseException as exc:
        # Last-resort handler when asyncio.run itself fails before/around loop
        logger.critical("Unhandled top-level crash", exc_info=exc)
        try:
            settings = get_settings()
            notifier = build_notifier(
                discord_webhook_url=settings.discord_webhook_url,
                telegram_bot_token=settings.telegram_bot_token,
                telegram_chat_id=settings.telegram_chat_id,
                quiet=bool(settings.quiet_notifier),
            )

            async def _ping() -> None:
                await notify_critical_crash(notifier, exc)

            asyncio.run(_ping())
        except Exception as notify_exc:
            logger.warning("Top-level crash Telegram failed: %s", notify_exc)
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
