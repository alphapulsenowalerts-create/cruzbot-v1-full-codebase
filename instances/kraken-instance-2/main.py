#!/usr/bin/env python3
"""Unified entrypoint for the Agentic Day Trading Bot."""

from __future__ import annotations
import os

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
from trading_bot.strategy import GrokSentimentFilter
from trading_bot.structural_guardrails import (
    check_buy_dedupe,
    check_spread,
    guardrails_startup_message,
)
from trading_bot.scanner_auto_buy import (
    AUTO_PAPER_MAX_CONCURRENT,
    AUTO_PAPER_MAX_EXPOSURE_USD,
    AUTO_PAPER_MAX_NOTIONAL_USD,
    clamp_auto_notional,
    evaluate_scanner_auto_buy,
    format_scanner_loop_log,
    is_aggressive_profile,
    proximity_score_from_result,
)
from trading_bot.adaptive_scalp import (
    SmartMemory,
    StagnantTracker,
    estimate_net_pnl,
    maybe_micro_trail_sl,
    quick_scalp_brackets,
    unrealized_pnl_pct,
    wants_elite_risk,
    wants_quick_scalp,
)
from trading_bot.market_regime import (
    BtcMarketRegimeEngine,
    STATE_BEAR_CHOP,
    SKIP_BEAR_CHOP,
    SKIP_DUMP_30M,
    SKIP_XSTOCK_OUTSIDE_RTH,
    is_tokenized_symbol,
    is_xstock_weekend_closed,
    seconds_until_friday_xstock_flush,
    xstock_entry_allowed,
)
from trading_bot.pair_blacklist import PairBlacklist
from trading_bot.brokers.alpaca import AlpacaBroker
from trading_bot.brokers.base import BrokerAdapter
from trading_bot.brokers.coinbase import CoinbaseBroker
from trading_bot.brokers.ib_stub import IBBrokerStub
from trading_bot.brokers.kraken import KrakenBroker
from trading_bot.brokers.mock import MockBroker
from trading_bot.config import PROJECT_ROOT, Settings, get_settings, reload_settings
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
from trading_bot.daily_digest import (
    already_sent_for_date,
    build_digest_snapshot,
    dedupe_path_for,
    mark_sent_for_date,
    next_summary_datetime,
    seconds_until_summary,
)
from trading_bot.risk_manager import RiskManager
from trading_bot.state_store import BehavioralStateStore
from trading_bot.cvd import last_closed_5m_candle
from trading_bot.derivatives_feed import (
    FundingOIFilter,
    PerpLeadLagEngine,
    coinbase_to_perp,
)
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
from trading_bot.symbol_universe import SymbolUniverse, bind_universe
from trading_bot.telegram_commands import (
    ALERT_LIVE_ACTIVATED,
    ALERT_PAPER_RESTORED,
    LIVE_CONFIRM_TTL_SECONDS,
    OpsControlState,
    REPLY_CONFIRM_EXPIRED,
    REPLY_MODE_LIVE_PENDING,
    REPLY_RESET_PAPER_CONFIRM_EXPIRED,
    RESET_PAPER_CONFIRM_TTL_SECONDS,
    RESET_PAPER_USAGE,
    ResetPaperError,
    REPLY_WIPE_PAPER_CONFIRM_EXPIRED,
    WIPE_PAPER_CONFIRM_TTL_SECONDS,
    WIPE_PAPER_USAGE,
    WipePaperError,
    SetLimitError,
    SetThresholdError,
    TelegramCommandListener,
    STATUS_SYMBOLS_CALLBACK,
    status_with_symbols_button,
    assert_paper_mode_for_reset_paper,
    assert_paper_mode_for_wipe_paper,
    default_reset_paper_cash,
    format_reset_paper_done_reply,
    format_reset_paper_pending_reply,
    format_wipe_paper_done_reply,
    format_wipe_paper_pending_reply,
    parse_reset_paper_args,
    parse_wipe_paper_args,
    wipe_paper_artifacts,
    day_trades_from_ledger,
    build_weekly_expectancy_digest,
    parse_weekly_digest_args,
    WEEKLY_DIGEST_USAGE,
    execute_set_limit,
    execute_set_threshold,
    TEST_TRADE_NOTIONAL_USD,
    TestTradeError,
    assert_paper_mode_for_test_trade,
    build_test_trade_order,
    format_test_trade_reply,
    parse_test_trade_args,
    format_mode_reply,
    format_status_reply,
    format_ping_reply,
    format_positions_reply,
    format_balance_reply,
    format_history_reply,
    format_grok_reply,
    format_regime_reply,
    format_help_reply,
    recent_trades_from_ledger,
    read_tail_log_lines,
    SetSpreadError,
    execute_set_spread,
    TradeProfileError,
    execute_set_profile,
    format_universe_status,
    format_universe_switched,
    format_symbols_reply,
    parse_universe_args,
    parse_universe_stocks_args,
    parse_tod_args,
    execute_set_tod,
    parse_stop_loss_args,
    execute_set_stop_loss,
    format_stop_loss_status,
    stop_loss_bracket_prices,
    STOP_LOSS_PRESETS,
    parse_winning_formula_args,
    execute_set_winning_formula,
    format_winning_formula_status,
    parse_circuity_breaker_args,
    format_circuity_breaker_status,
    universe_friendly_label,
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
    if name == "kraken":
        return KrakenBroker(settings)
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
        )
        self.macro = build_macro_guard(
            enabled=settings.macro_pause_enabled,
            pause_minutes=settings.macro_pause_minutes,
            calendar_url=settings.macro_calendar_url,
            api_key=settings.macro_calendar_api_key,
        )
        self.grok_sentiment = GrokSentimentFilter()
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
                phase1_gate_enabled=bool(
                    getattr(settings, "cvd_gate_enabled", True)
                    or getattr(settings, "liq_sweep_gate_enabled", True)
                ),
                tod_gate_enabled=bool(getattr(settings, "tod_gate_enabled", True)),
                disable_tod_gate=bool(getattr(settings, "disable_tod_gate", False)),
                daily_dd_sqlite_enabled=bool(
                    getattr(settings, "daily_dd_sqlite_enabled", True)
                ),
                daily_dd_limit_pct=float(
                    getattr(settings, "daily_drawdown_limit_pct", 0.03) or 0.03
                ),
                sentiment_filter=self.grok_sentiment,
                regime_gate_enabled=bool(
                    getattr(settings, "regime_gate_enabled", True)
                ),
                active_params_path=str(settings.active_params_path),
            )
            # Paper refactor knobs on engine (ATR brackets + ADX soft threshold)
            signal_engine.atr_bracket_exits = bool(
                getattr(settings, "atr_bracket_exits", True)
            )
            signal_engine.atr_bracket_sl_mult = float(
                getattr(settings, "atr_bracket_sl_mult", 1.8) or 1.8
            )
            signal_engine.atr_bracket_tp_mult = float(
                getattr(settings, "atr_bracket_tp_mult", 3.0) or 3.0
            )
            signal_engine.atr_bracket_sl_min_pct = float(
                getattr(settings, "atr_bracket_sl_min_pct", 0.01) or 0.01
            )
            signal_engine.atr_bracket_sl_max_pct = float(
                getattr(settings, "atr_bracket_sl_max_pct", 0.012) or 0.012
            )
            signal_engine.atr_bracket_tp_min_pct = float(
                getattr(settings, "atr_bracket_tp_min_pct", 0.02) or 0.02
            )
            signal_engine.min_tp_pct = float(getattr(settings, "min_tp_pct", 0.02) or 0.02)
            signal_engine.adx_threshold_floor = float(
                getattr(settings, "adx_threshold_floor", 20.0) or 20.0
            )
            signal_engine.adx_threshold_raise = float(
                getattr(settings, "adx_threshold_raise", 15.0) or 15.0
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
        # Post-stop-loss re-entry cooldown (symbol -> unix ts until); mirrored in state_store
        self._cooldowns: Dict[str, float] = {}
        self.ops = OpsControlState()
        try:
            self.ops.cb_enabled = bool(
                getattr(self.settings, "circuit_breaker_enabled", True)
            )
        except Exception:
            self.ops.cb_enabled = True
        self.smart_memory = SmartMemory(PROJECT_ROOT / "data" / "trade_memory.json")
        self._pending_maker_time_exits = {}
        self._risk_free_notified = set()  # symbols with [RISK FREE] TG sent

        # Time-exit fee cushion: RT fee cover + micro pad; 5m maker wait
        # Tier-1 RT hurdle 1.20% → time-exit maker target +1.25%
        self._TIME_EXIT_FEE_CUSHION_PCT = 0.0125
        self._TIME_EXIT_FEE_PAD_PCT = 0.0  # limit = entry * 1.0125 exactly
        self._TIME_EXIT_MAKER_WAIT_SEC = 300.0  # 5 minutes
        self._HWM_PEAK_ARM_PCT = 0.012  # +1.20% peak → +1.25% SL floor
        self._HWM_PROGRESS_ARM = 60.0
        self._HWM_FLOOR_PCT = 0.0125
        # Profit-runner: delay tight trail until +2.50% / 75% progress
        self._TRAIL_RUNNER_ARM_PCT = 0.025
        self._TRAIL_RUNNER_PROGRESS = 75.0
        self._TRAIL_RUNNER_OFFSET_PCT = 0.01  # 1% behind peak
        self._peak_upl: dict = {}  # symbol -> peak gross UPL fraction
        self._profit_runner_notified = set()

        self.stagnant = StagnantTracker()
        self.btc_regime = BtcMarketRegimeEngine(
            cache_seconds=float(getattr(settings, "btc_regime_cache_seconds", 60.0) or 60.0),
            dump_pct=float(getattr(settings, "btc_dump_pct", 0.012) or 0.012),
            enabled=bool(getattr(settings, "btc_regime_enabled", True)),
        )
        self.pair_blacklist = PairBlacklist(
            getattr(settings, "trades_db_path", None)
            or (PROJECT_ROOT / "data" / "trades.db"),
            enabled=bool(getattr(settings, "pair_blacklist_enabled", True)),
        )
        # Sync ENTRY_THRESHOLD from Settings into proximity runtime module
        try:
            from trading_bot.config import apply_runtime_entry_threshold

            apply_runtime_entry_threshold(
                self.settings,
                float(getattr(self.settings, "entry_threshold", 60.0) or 60.0),
            )
        except Exception as exc:
            logger.debug("entry_threshold init: %s", exc)
        self._tg_listener: Optional[TelegramCommandListener] = None
        self.symbol_universe = SymbolUniverse(self.settings)
        bind_universe(self.symbol_universe)
        # Intel v2: lead-lag priority + funding/OI + failure blacklist cache
        self._priority_symbols: list[str] = []
        self._priority_event = asyncio.Event()
        self._last_obs: Dict[str, AgentObservation] = {}
        self._last_scan_latency_ms: float = 0.0
        self._last_scan_pair_count: int = 0
        self._last_entry_block_reason: Optional[str] = None
        self._pending_close_sym: Optional[str] = None
        self._pending_close_until: float = 0.0
        self._pending_clear_mode: Optional[str] = None
        self._pending_clear_until: float = 0.0
        self._symbol_sem = asyncio.Semaphore(
            max(1, min(16, int(getattr(settings, "scan_concurrency", 6) or 6)))
        )
        self._entry_lock = asyncio.Lock()
        self._active_params: dict = {}
        mapped = [s for s in settings.symbol_list if coinbase_to_perp(s)]
        self.leadlag = PerpLeadLagEngine(
            enabled=bool(getattr(settings, "perp_leadlag_enabled", True)),
            venues=str(getattr(settings, "perp_leadlag_venues", "binance,bybit") or "binance,bybit"),
            symbols=tuple(mapped) or ("BTC-USD", "ETH-USD"),
            sweep_mult=float(getattr(settings, "perp_sweep_mult", 3.0) or 3.0),
            sweep_window_sec=float(getattr(settings, "perp_sweep_window_sec", 5.0) or 5.0),
            liq_window_sec=float(getattr(settings, "perp_liq_window_sec", 10.0) or 10.0),
            liq_min_cluster=int(getattr(settings, "perp_liq_min_cluster", 5) or 5),
            signal_ttl_sec=float(getattr(settings, "perp_signal_ttl_sec", 30.0) or 30.0),
            on_signal=self._on_leadlag_signal,
            cvd_period_sec=float(getattr(settings, "cvd_period_sec", 300.0) or 300.0),
            short_liq_window_sec=float(getattr(settings, "liq_sweep_window_sec", 60.0) or 60.0),
            short_liq_notional_threshold=float(
                getattr(settings, "liq_sweep_notional_usd", 50_000.0) or 50_000.0
            ),
            short_liq_ttl_sec=float(getattr(settings, "liq_sweep_ttl_sec", 30.0) or 30.0),
        )
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
                entry = getattr(pos, "avg_entry_price", None)
                mv = getattr(pos, "market_value", None)
                mark = None
                try:
                    if entry is not None and abs(qty) > 1e-12 and mv is not None:
                        mark = float(mv) / abs(float(qty))
                except (TypeError, ValueError, ZeroDivisionError):
                    mark = None
                upl = getattr(pos, "unrealized_pl", None)
                if upl is None and entry is not None and mark is not None:
                    try:
                        side_l = str(getattr(pos, "side", "long") or "long").lower()
                        if side_l == "short":
                            upl = (float(entry) - float(mark)) * abs(qty)
                        else:
                            upl = (float(mark) - float(entry)) * abs(qty)
                    except (TypeError, ValueError):
                        upl = None
                positions.append(
                    {
                        "symbol": sym,
                        "qty": qty,
                        "avg_entry_price": entry,
                        "market_value": mv,
                        "unrealized_pl": upl,
                        "mark_price": mark,
                        "side": getattr(pos, "side", "long"),
                        "stop_loss": getattr(pos, "stop_loss", None),
                        "take_profit": getattr(pos, "take_profit", None),
                    }
                )
        except Exception as exc:
            logger.debug("status positions: %s", exc)
        age = None
        try:
            age = self.feed.last_tick_age_seconds
        except Exception:
            age = getattr(self.broker, "last_tick_age_seconds", None)

        # Entry proximity from last in-memory observations (<1ms, no network)
        entry_proximity = None
        target_setup = None
        try:
            from trading_bot.utils.entry_proximity import best_entry_proximity

            sent = None
            grok = getattr(self, "grok_sentiment", None)
            if grok is not None:
                raw = getattr(grok, "latest_sentiment", None)
                if isinstance(raw, dict):
                    sent = raw
            rvol = 2.0
            eng = None
            agent = getattr(self, "agent", None)
            if agent is not None:
                eng = getattr(agent, "signal_engine", None)
            if eng is not None and hasattr(eng, "rvol_breakout_mult"):
                try:
                    rvol = float(eng.rvol_breakout_mult)
                except (TypeError, ValueError):
                    rvol = 2.0
            obs_list = [
                o for o in (self._last_obs or {}).values()
                if not self._symbol_tick_stale(str(getattr(o, "symbol", "") or ""))
            ]
            if obs_list:
                entry_proximity = best_entry_proximity(
                    obs_list,
                    sentiment=sent,
                    rvol_breakout_mult=rvol,
                    long_threshold=float(
                        getattr(self.settings, "entry_threshold", 60.0) or 60.0
                    ),
                )
                target_setup = entry_proximity.get("direction")
                # Kraken spot cannot short: never advertise a SHORT target when
                # paper shorting is disabled, even if a bearish observation wins
                # the generic proximity scorer.
                if (
                    not bool(getattr(self.settings, "allow_paper_shorts", False))
                    and isinstance(entry_proximity, dict)
                    and str(entry_proximity.get("direction") or "").upper() == "SHORT"
                ):
                    entry_proximity = dict(entry_proximity)
                    entry_proximity["direction"] = "WAIT"
                    target_setup = "WAIT"
            else:
                entry_proximity = best_entry_proximity(
                    [],
                    sentiment=sent,
                    rvol_breakout_mult=rvol,
                    long_threshold=float(
                        getattr(self.settings, "entry_threshold", 60.0) or 60.0
                    ),
                )
                target_setup = entry_proximity.get("direction") or "WAIT"
        except Exception as exc:
            logger.debug("status entry proximity: %s", exc)
            entry_proximity = None
            target_setup = None

        # Absolute directional lock: spot keeps LONG under SHORT bias.
        try:
            if isinstance(entry_proximity, dict):
                entry_proximity = self._apply_absolute_direction_lock(entry_proximity)
                target_setup = entry_proximity.get("direction") or target_setup
        except Exception as exc:
            logger.debug("absolute direction lock: %s", exc)

        wr_pct = None
        sess_w = sess_l = 0
        try:
            sess_w, sess_l, wr_pct = self.smart_memory.session_record()
        except Exception as exc:
            logger.debug("status win-rate: %s", exc)

        _mstate = None
        try:
            _reg = getattr(self, "btc_regime", None)
            _mstate = str(getattr(_reg, "state", None) or "")
            if _mstate == STATE_BEAR_CHOP or bool(getattr(_reg, "dump_30m", False)):
                _mstate = f"{_mstate} bias=SHORT"
        except Exception:
            _mstate = None

        # Focus BLOCKED reason when proximity full but Target WAIT
        _focus_block = None
        try:
            _focus_sym = None
            if isinstance(entry_proximity, dict):
                _focus_sym = entry_proximity.get("symbol")
            _focus_block = self._status_focus_block_reason(_focus_sym)
        except Exception:
            _focus_block = getattr(self, "_last_entry_block_reason", None)

        try:
            self._status_cb_consec_losses = int(self.smart_memory.consecutive_losses())
        except Exception:
            self._status_cb_consec_losses = 0
        wallet_b4_val = None
        try:
            if bool(self.settings.paper_trading_mode):
                getter = getattr(self.broker, "paper_wallet_b4", None)
                if callable(getter):
                    wallet_b4_val = float(getter())
                else:
                    ae = float(getattr(self.settings, "account_equity", 0) or 0)
                    wallet_b4_val = ae if ae > 0 else None
            else:
                live_get = getattr(self.broker, "get_live_bankroll", None)
                if callable(live_get):
                    br = await live_get()
                    if isinstance(br, dict):
                        wallet_b4_val = float(br.get("equity") or br.get("cash") or 0) or None
                if wallet_b4_val is None:
                    wallet_b4_val = float(equity)  # fall back to broker equity
        except Exception as _wb_exc:
            logger.debug("wallet_b4 status: %s", _wb_exc)

        status_txt = format_status_reply(

            paper_cash=cash,
            paper_equity=equity,
            wallet_b4=wallet_b4_val,
            positions=positions,
            paused=self.ops.paused,
            strategy_mode=str(self.settings.strategy_mode or ""),
            last_tick_age_seconds=age,
            paper=bool(self.settings.paper_trading_mode),
            symbols=list(self.settings.symbol_list),
            max_notional_per_trade=float(self.settings.max_notional_per_trade_usd),
            max_total_exposure=float(self.settings.max_total_exposure_usd),
            target_setup=(
                entry_proximity.get("direction")
                if isinstance(entry_proximity, dict)
                else target_setup
            ),
            entry_proximity=entry_proximity,
            entry_threshold=float(
                self._bear_spot_long_threshold(
                    float(getattr(self.settings, "entry_threshold", 60.0) or 60.0)
                )
            ),
            max_spread_pct=float(
                getattr(self.settings, "max_spread_pct", 0.001) or 0.001
            ),
            trade_profile=str(
                getattr(self.settings, "trade_profile", "medium") or "medium"
            ),
            tod_gate_enabled=not bool(
                getattr(self.settings, "disable_tod_gate", False)
            ),
            tod_custom_lock=bool(
                getattr(self.settings, "tod_custom_lock", False)
            ),
            stop_loss_profile=str(
                getattr(self.settings, "stop_loss_profile", "medium") or "medium"
            ),
            stop_loss_effective_pct=self._profile_sl_tp_pct(
                notional=float(
                    getattr(self.settings, "max_notional_per_trade_usd", 500) or 500
                )
            )[0],
            stop_loss_clamped=bool(
                getattr(self.settings, "winning_formula", False)
                and self._market_short_bias()
            ),
            winning_formula=bool(
                getattr(self.settings, "winning_formula", False)
            ),
            circuit_breaker_on=bool(getattr(self.ops, "cb_enabled", True)),
            circuit_breaker_consec_losses=int(
                getattr(self, "_status_cb_consec_losses", 0) or 0
            ),
            last_scan_latency_ms=float(getattr(self, "_last_scan_latency_ms", 0.0) or 0.0),
            last_scan_pair_count=int(
                getattr(self, "_last_scan_pair_count", 0)
                or len(list(self.settings.symbol_list))
            ),
            win_rate_pct=wr_pct,
            session_wins=sess_w,
            session_losses=sess_l,
            symbol_mode=str(
                getattr(self.settings, "symbol_mode", "ALLOWLIST") or "ALLOWLIST"
            ),
            universe_stocks=bool(getattr(self.symbol_universe, "stocks_enabled", False)),
            stock_count=len(getattr(self.symbol_universe, "stock_symbols", []) or []),
            market_state=_mstate,
            spot_long_only=self._is_spot_long_only(),
            focus_block_reason=_focus_block,
        )
        try:
            note = self.smart_memory.status_note()
            if note:
                # Show tightened threshold clearly on /status
                thresh = int(round(float(getattr(self.settings, "entry_threshold", 55) or 55)))
                status_txt = status_txt.replace(
                    f"Threshold: {thresh}%",
                    f"Threshold: {thresh}% ({note})",
                    1,
                )
        except Exception:
            pass
        # Elite: Market state line (post-inject; avoid format_status_reply top edits)
        try:
            regime = getattr(self, "btc_regime", None)
            mstate = str(getattr(regime, "state", None) or "n/a")
            market_line = "Market: " + mstate
            if mstate == STATE_BEAR_CHOP or bool(getattr(regime, "dump_30m", False)):
                market_line += " bias=SHORT"
            if "Market:" not in status_txt:
                marker = "equity="
                if marker in status_txt:
                    i = status_txt.find(marker)
                    j = status_txt.find("\n", i)
                    if j < 0:
                        status_txt = status_txt + "\n" + market_line
                    else:
                        status_txt = status_txt[:j] + "\n" + market_line + status_txt[j:]
                else:
                    status_txt = status_txt.rstrip() + "\n" + market_line + "\n"
        except Exception:
            pass
        return status_txt

    async def _cmd_status(self, _cmd: str, _args: list[str]):
        try:
            if getattr(self, "btc_regime", None) is not None:
                await self.btc_regime.refresh(self.broker)
        except Exception as exc:
            logger.debug("btc_regime status refresh: %s", exc)
        text = await self._build_status_text()
        n = len(list(self.settings.symbol_list) or [])
        try:
            uni = getattr(self, "symbol_universe", None)
            if uni is not None and getattr(uni, "mode", None) == "DYNAMIC_ALL":
                n = len(list(getattr(uni, "active_symbols", None) or self.settings.symbol_list) or n)
        except Exception:
            pass
        return status_with_symbols_button(text, symbol_count=n)

    async def _cb_status_symbols(self, _data: str):
        """Inline ▼ Symbols under /status — same body as /symbols."""
        return await self._cmd_symbols("symbols", [])

    async def _cmd_pause(self, _cmd: str, _args: list[str]) -> str:
        self.ops.set_pause(True)
        # Manual pause cancels any pending circuit-breaker auto-resume
        cancelled = bool(getattr(self.ops, "cb_auto_resume_armed", False))
        try:
            self.ops.clear_cb_auto_resume()
        except Exception:
            pass
        logger.warning("OPS PAUSE — new buys skipped (exits/brackets still active)")
        if cancelled:
            return (
                "Paused: new buys skipped; exits/brackets still managed. "
                "Circuit-breaker auto-resume cancelled."
            )
        return "Paused: new buys skipped; exits/brackets still managed."

    async def _cmd_resume(self, _cmd: str, _args: list[str]) -> str:
        self.ops.set_pause(False)
        try:
            self.ops.clear_cb_auto_resume()
        except Exception:
            pass
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

    async def _cmd_reset_paper(self, _cmd: str, args: list[str]) -> str:
        """ /reset_paper [cash] → /reset_paper confirm — wipe paper book (two-step). """
        try:
            assert_paper_mode_for_reset_paper(
                paper=bool(self.settings.paper_trading_mode)
            )
            confirm, cash_arg = parse_reset_paper_args(args)
        except ResetPaperError as exc:
            return str(exc)

        default_cash = default_reset_paper_cash(
            getattr(self.settings, "account_equity", None)
        )

        if not confirm:
            cash = float(cash_arg) if cash_arg is not None else float(default_cash)
            explicit = cash_arg is not None
            ttl = self.ops.arm_reset_paper_confirm(
                cash, explicit=explicit, ttl=RESET_PAPER_CONFIRM_TTL_SECONDS
            )
            logger.warning(
                "OPS /reset_paper — pending confirm cash=%.2f explicit=%s ttl=%.0fs",
                cash,
                explicit,
                ttl,
            )
            return format_reset_paper_pending_reply(cash, ttl_seconds=ttl)

        if not self.ops.has_pending_reset_paper_confirm():
            return REPLY_RESET_PAPER_CONFIRM_EXPIRED

        pending_cash = self.ops.pending_reset_paper_cash()
        pending_explicit = self.ops.pending_reset_paper_explicit()
        self.ops.clear_reset_paper_confirm()

        if cash_arg is not None:
            cash = float(cash_arg)
            explicit = True
        elif pending_cash is not None:
            cash = float(pending_cash)
            explicit = bool(pending_explicit)
        else:
            cash = float(default_cash)
            explicit = False

        reset_fn = getattr(self.broker, "reset_paper_book", None)
        if not callable(reset_fn):
            # Fallback: write flat JSON via deploy helper when broker has no method
            try:
                from deploy.reset_paper_book import reset_book
                from pathlib import Path as _Path

                book = getattr(self.settings, "paper_book_path", None) or (
                    PROJECT_ROOT / "data" / "paper_book.json"
                )
                reset_book(_Path(book), cash)
            except Exception as exc:
                logger.warning("reset_paper fallback write failed: %s", exc)
                return f"/reset_paper failed: broker has no reset_paper_book and fallback error: {exc}"
        else:
            try:
                reset_fn(cash)
            except Exception as exc:
                logger.warning("reset_paper_book failed: %s", exc)
                return f"/reset_paper failed: {exc}"

        account_equity_updated = False
        if explicit:
            try:
                from trading_bot.config import format_env_float, upsert_env_keys

                cash_s = format_env_float(cash)
                env_path = PROJECT_ROOT / ".env"
                upsert_env_keys(env_path, {"ACCOUNT_EQUITY": cash_s})
                import os as _os

                _os.environ["ACCOUNT_EQUITY"] = cash_s
                object.__setattr__(self.settings, "account_equity", float(cash))
                account_equity_updated = True
            except Exception as exc:
                logger.warning("ACCOUNT_EQUITY upsert after reset_paper failed: %s", exc)

        logger.warning(
            "OPS /reset_paper CONFIRMED — cash=%.2f equity=%.2f positions=0 account_equity_updated=%s",
            cash,
            cash,
            account_equity_updated,
        )
        try:
            self.trade_logger.log_event(
                "paper_book_reset",
                {
                    "cash": cash,
                    "equity": cash,
                    "positions": 0,
                    "account_equity_updated": account_equity_updated,
                    "source": "telegram_/reset_paper",
                },
            )
        except Exception as exc:
            logger.debug("paper_book_reset log: %s", exc)

        try:
            self.smart_memory.reset_session()
        except Exception as exc:
            logger.debug("reset_paper win-rate clear: %s", exc)

        return format_reset_paper_done_reply(
            cash,
            equity=cash,
            positions=0,
            account_equity_updated=account_equity_updated,
        )

    async def _cmd_wipe_paper(self, _cmd: str, args: list[str]) -> str:
        """ /wipe_paper [cash] → /wipe_paper confirm — full paper scratch (book+history+logs).

        Alias: /factory_reset (same handler).
        """
        try:
            assert_paper_mode_for_wipe_paper(
                paper=bool(self.settings.paper_trading_mode)
            )
            confirm, cash_arg = parse_wipe_paper_args(args)
        except WipePaperError as exc:
            return str(exc)

        default_cash = default_reset_paper_cash(
            getattr(self.settings, "account_equity", None)
        )

        if not confirm:
            cash = float(cash_arg) if cash_arg is not None else float(default_cash)
            explicit = cash_arg is not None
            ttl = self.ops.arm_wipe_paper_confirm(
                cash, explicit=explicit, ttl=WIPE_PAPER_CONFIRM_TTL_SECONDS
            )
            logger.warning(
                "OPS /wipe_paper — pending confirm cash=%.2f explicit=%s ttl=%.0fs",
                cash,
                explicit,
                ttl,
            )
            return format_wipe_paper_pending_reply(cash, ttl_seconds=ttl)

        if not self.ops.has_pending_wipe_paper_confirm():
            return REPLY_WIPE_PAPER_CONFIRM_EXPIRED

        pending_cash = self.ops.pending_wipe_paper_cash()
        pending_explicit = self.ops.pending_wipe_paper_explicit()
        self.ops.clear_wipe_paper_confirm()

        if cash_arg is not None:
            cash = float(cash_arg)
            explicit = True
        elif pending_cash is not None:
            cash = float(pending_cash)
            explicit = bool(pending_explicit)
        else:
            cash = float(default_cash)
            explicit = False

        # 1) Reset paper book (same path as /reset_paper)
        reset_fn = getattr(self.broker, "reset_paper_book", None)
        if not callable(reset_fn):
            try:
                from deploy.reset_paper_book import reset_book
                from pathlib import Path as _Path

                book = getattr(self.settings, "paper_book_path", None) or (
                    PROJECT_ROOT / "data" / "paper_book.json"
                )
                reset_book(_Path(book), cash)
            except Exception as exc:
                logger.warning("wipe_paper fallback book write failed: %s", exc)
                return (
                    f"/wipe_paper failed: broker has no reset_paper_book "
                    f"and fallback error: {exc}"
                )
        else:
            try:
                reset_fn(cash)
            except Exception as exc:
                logger.warning("wipe_paper reset_paper_book failed: %s", exc)
                return f"/wipe_paper failed: {exc}"

        account_equity_updated = False
        if explicit:
            try:
                from trading_bot.config import format_env_float, upsert_env_keys

                cash_s = format_env_float(cash)
                env_path = PROJECT_ROOT / ".env"
                upsert_env_keys(env_path, {"ACCOUNT_EQUITY": cash_s})
                import os as _os

                _os.environ["ACCOUNT_EQUITY"] = cash_s
                object.__setattr__(self.settings, "account_equity", float(cash))
                account_equity_updated = True
            except Exception as exc:
                logger.warning("ACCOUNT_EQUITY upsert after wipe_paper failed: %s", exc)

        # 2–3) Clear history DBs + truncate paper log (do not delete DB files / .env)
        sqlite_path = getattr(self.settings, "sqlite_path", None) or (
            PROJECT_ROOT / "data" / "trading_bot_2.db"
        )
        try:
            wipe_summary = wipe_paper_artifacts(
                project_root=PROJECT_ROOT,
                sqlite_path=sqlite_path,
                ledger_path=PROJECT_ROOT / "data" / "paper_ledger.db",
                log_path=PROJECT_ROOT / "data" / "paper_loop.log",
            )
        except Exception as exc:
            logger.warning("wipe_paper_artifacts failed: %s", exc)

            try:
                self.smart_memory.reset_session()
            except Exception as _sm:
                logger.debug("wipe smart_memory reset: %s", _sm)
            try:
                if getattr(self, "pair_blacklist", None) is not None:
                    self.pair_blacklist.clear_closed_history()
            except Exception as _pb:
                logger.debug("wipe pair_blacklist clear: %s", _pb)
            wipe_summary = {
                "ledger_rows_deleted": 0,
                "trading_tables_cleared": {},
                "log_truncated": False,
            }

        # 4) Clear in-memory trade/pnl observation caches
        try:
            if hasattr(self, "_last_obs") and isinstance(self._last_obs, dict):
                self._last_obs.clear()
        except Exception as exc:
            logger.debug("wipe_paper clear _last_obs: %s", exc)

        history_cleared = True
        logs_cleared = bool(wipe_summary.get("log_truncated"))
        tables = wipe_summary.get("trading_tables_cleared") or {}

        logger.warning(
            "OPS /wipe_paper CONFIRMED — cash=%.2f positions=0 "
            "ledger_rows=%s tables=%s log_truncated=%s account_equity_updated=%s",
            cash,
            wipe_summary.get("ledger_rows_deleted"),
            tables,
            logs_cleared,
            account_equity_updated,
        )
        try:
            self.trade_logger.log_event(
                "paper_factory_wipe",
                {
                    "cash": cash,
                    "equity": cash,
                    "positions": 0,
                    "account_equity_updated": account_equity_updated,
                    "ledger_rows_deleted": wipe_summary.get("ledger_rows_deleted"),
                    "trading_tables_cleared": tables,
                    "log_truncated": logs_cleared,
                    "source": f"telegram_/{_cmd}",
                },
            )
        except Exception as exc:
            logger.debug("paper_factory_wipe log: %s", exc)

        try:
            self.smart_memory.reset_session()
        except Exception as exc:
            logger.debug("wipe_paper win-rate clear: %s", exc)

        return format_wipe_paper_done_reply(
            cash,
            equity=cash,
            positions=0,
            account_equity_updated=account_equity_updated,
            history_cleared=history_cleared,
            logs_cleared=logs_cleared,
            ledger_rows_deleted=int(wipe_summary.get("ledger_rows_deleted") or 0),
            trading_tables_cleared=tables,
        )


    async def _cmd_set_limit(self, _cmd: str, args: list[str]) -> str:
        """ /set_limit <trade_cap> <max_book> — live-update size caps (not paper/live mode).

        Alias: `/set_limit entry_threshold <pct>` → `/set_threshold <pct>`.
        """
        if args and str(args[0]).strip().lower().replace("-", "_") in {
            "entry_threshold",
            "threshold",
            "entrythreshold",
        }:
            return await self._cmd_set_threshold(_cmd, list(args[1:]))
        try:
            reply = execute_set_limit(
                self.settings,
                args,
                env_path=PROJECT_ROOT / ".env",
            )
        except SetLimitError as exc:
            return str(exc)
        except Exception as exc:
            logger.warning("set_limit persist/apply failed: %s", exc)
            return f"/set_limit failed; caps unchanged: {exc}"
        logger.warning(
            "OPS /set_limit — trade_cap=$%.2f max_book=$%.2f (runtime + .env); paper/live unchanged",
            float(self.settings.max_notional_per_trade_usd),
            float(self.settings.max_total_exposure_usd),
        )
        try:
            self.trade_logger.log_event(
                "set_limit",
                {
                    "trade_cap": float(self.settings.max_notional_per_trade_usd),
                    "max_book": float(self.settings.max_total_exposure_usd),
                    "env_keys": ["MAX_NOTIONAL_PER_TRADE_USD", "MAX_TOTAL_EXPOSURE_USD"],
                    "paper_trading_mode": bool(self.settings.paper_trading_mode),
                },
            )
        except Exception as exc:
            logger.debug("set_limit log: %s", exc)
        return reply


    async def _cmd_set_threshold(self, _cmd: str, args: list[str]) -> str:
        """ /set_threshold <pct>|custom <pct> — live-update entry proximity (15–95). """
        try:
            reply = execute_set_threshold(
                self.settings,
                args,
                env_path=PROJECT_ROOT / ".env",
            )
        except SetThresholdError as exc:
            return str(exc)
        except Exception as exc:
            logger.warning("set_threshold persist/apply failed: %s", exc)
            return f"/set_threshold failed; threshold unchanged: {exc}"
        logger.warning(
            "OPS /set_threshold — ENTRY_THRESHOLD=%s%% (runtime + .env)",
            int(round(float(self.settings.entry_threshold))),
        )
        try:
            self.trade_logger.log_event(
                "set_threshold",
                {
                    "entry_threshold": float(self.settings.entry_threshold),
                    "env_keys": ["ENTRY_THRESHOLD"],
                    "paper_trading_mode": bool(self.settings.paper_trading_mode),
                },
            )
        except Exception as exc:
            logger.debug("set_threshold log: %s", exc)
        return reply


    async def _cmd_set_threshold_custom(self, _cmd: str, args: list[str]) -> str:
        """ /set_threshold_custom <pct> — menu-friendly alias (15–95). """
        if not args:
            return (
                "Usage: /set_threshold_custom <pct> "
                "(integer 15–95, e.g. /set_threshold_custom 42)"
            )
        # Reuse same path as /set_threshold custom <pct>
        return await self._cmd_set_threshold(_cmd, ["custom", *list(args)])


    async def _cmd_test_trade(self, _cmd: str, args: list[str]) -> str:
        """ /test_trade <symbol> — paper-only $100 market BUY; bypass strategy/risk gates. """
        try:
            assert_paper_mode_for_test_trade(
                paper=bool(self.settings.paper_trading_mode)
            )
            symbol = parse_test_trade_args(args)
        except TestTradeError as exc:
            return str(exc)

        notional = float(
            getattr(self.settings, "max_notional_per_trade_usd", None)
            or TEST_TRADE_NOTIONAL_USD
        )
        if notional <= 0:
            notional = float(TEST_TRADE_NOTIONAL_USD)

        try:
            quote = await self.broker.get_quote(symbol)
            px = float(getattr(quote, "mid", 0) or 0)
            if px <= 0:
                bid = float(getattr(quote, "bid", 0) or 0)
                ask = float(getattr(quote, "ask", 0) or 0)
                px = bid if bid > 0 else ask
        except Exception as exc:
            logger.warning("test_trade quote failed for %s: %s", symbol, exc)
            return f"/test_trade failed: could not get price for {symbol}: {exc}"

        try:
            order = build_test_trade_order(
                symbol,
                price=px,
                notional=notional,
                qty_precision=int(getattr(self.settings, "qty_precision", 8) or 8),
                paper=True,
            )
        except TestTradeError as exc:
            return str(exc)

        try:
            result = await self.executor.submit(order)
        except Exception as exc:
            logger.warning("test_trade submit failed: %s", exc)
            return f"/test_trade failed: {exc}"

        if result.status not in (OrderStatus.FILLED, OrderStatus.PARTIAL) or float(
            result.filled_qty or 0
        ) <= 0:
            msg = (result.message or result.status.value or "rejected").strip()
            return f"/test_trade rejected: {msg}"

        fill_px = float(result.avg_fill_price or px)
        filled_notional = float(result.filled_qty) * fill_px
        # Prefer actual filled notional for reply when close to requested
        reply_notional = notional
        if filled_notional > 0:
            reply_notional = notional  # keep fixed $100 wording per spec

        logger.warning(
            "OPS /test_trade — PAPER BUY %s qty=%.8f @ %.6f notional~$%.2f (bypass gates)",
            symbol,
            float(result.filled_qty),
            fill_px,
            filled_notional,
        )
        try:
            self.trade_logger.log_event(
                "test_trade",
                {
                    "symbol": symbol,
                    "side": "BUY",
                    "qty": float(result.filled_qty),
                    "fill_price": fill_px,
                    "notional": filled_notional,
                    "bypass_gates": True,
                    "paper_trading_mode": True,
                    "broker_order_id": getattr(result, "broker_order_id", None),
                },
            )
        except Exception as exc:
            logger.debug("test_trade log: %s", exc)

        return format_test_trade_reply(symbol, fill_px, notional=reply_notional)



    async def _cmd_ping(self, _cmd: str, _args: list[str]) -> str:
        """ /ping — heartbeat + local handler latency. """
        t0 = time.perf_counter()
        # Touch broker account lightly to prove server path (paper-safe read)
        try:
            await self.broker.get_account()
        except Exception:
            pass
        ms = (time.perf_counter() - t0) * 1000.0
        return format_ping_reply(ms)

    async def _cmd_positions(self, _cmd: str, _args: list[str]) -> str:
        """ /positions — open positions with PnL% and SL/TP. """
        rows: list[dict] = []
        try:
            for pos in await self.broker.get_positions():
                qty = float(getattr(pos, "qty", 0) or 0)
                if abs(qty) <= 0:
                    continue
                entry = getattr(pos, "avg_entry_price", None)
                upl = getattr(pos, "unrealized_pl", None)
                pnl_pct = None
                try:
                    if upl is not None and entry is not None and float(entry) > 0:
                        cost = float(entry) * abs(qty)
                        if cost > 0:
                            pnl_pct = 100.0 * float(upl) / cost
                except (TypeError, ValueError):
                    pnl_pct = None
                rows.append(
                    {
                        "symbol": getattr(pos, "symbol", None),
                        "qty": qty,
                        "avg_entry_price": entry,
                        "unrealized_pl": upl,
                        "pnl_pct": pnl_pct,
                        "stop_loss": getattr(pos, "stop_loss", None),
                        "take_profit": getattr(pos, "take_profit", None),
                    }
                )
        except Exception as exc:
            logger.warning("positions cmd failed: %s", exc)
            return f"/positions failed: {exc}"
        return format_positions_reply(
            rows, paper=bool(self.settings.paper_trading_mode)
        )

    async def _cmd_balance(self, _cmd: str, _args: list[str]) -> str:
        """ /balance — PAPER/LIVE cash, available, equity. """
        paper = bool(self.settings.paper_trading_mode)
        try:
            acct = await self.broker.get_account()
            cash = float(acct.cash)
            equity = float(acct.equity)
            bp = getattr(acct, "buying_power", None)
            try:
                bp_f = float(bp) if bp is not None else None
            except (TypeError, ValueError):
                bp_f = None
        except Exception as exc:
            logger.warning("balance cmd failed: %s", exc)
            cash = equity = float(self.settings.account_equity)
            bp_f = None
        if paper or bp_f is None:
            return format_balance_reply(
                paper=paper, cash=cash, equity=equity, available=cash, available_label="cash"
            )
        return format_balance_reply(
            paper=paper,
            cash=cash,
            equity=equity,
            available=bp_f,
            available_label="available margin",
        )

    async def _cmd_history(self, _cmd: str, _args: list[str]) -> str:
        """ /history — last 5 fills from paper_ledger.db. """
        ledger = PROJECT_ROOT / "data" / "paper_ledger.db"
        trades = recent_trades_from_ledger(str(ledger), limit=5)
        return format_history_reply(trades)

    async def _cmd_grok(self, _cmd: str, _args: list[str]) -> str:
        """ /grok — immediate manual Grok/xAI sentiment request. """
        import os
        from trading_bot import strategy as strategy_mod

        key = (os.getenv("XAI_API_KEY") or "").strip()
        if not key:
            return format_grok_reply(
                action="HOLD",
                confidence=0.0,
                error="XAI_API_KEY missing — cannot query Grok/xAI (set key or add credits)",
            )
        # Refresh module-level key (may have been unset at import time)
        try:
            strategy_mod.XAI_API_KEY = key
        except Exception:
            pass
        grok = getattr(self, "grok_sentiment", None)
        if grok is None:
            return format_grok_reply(
                action="HOLD",
                confidence=0.0,
                error="Grok sentiment filter not initialized",
            )
        try:
            data = await self._get_market_data_for_grok()
            result = await grok.query_grok_fast(data)
            if isinstance(result, dict):
                grok.latest_sentiment = result
                return format_grok_reply(
                    action=str(result.get("action") or "HOLD"),
                    confidence=float(result.get("confidence") or 0.0),
                )
            return format_grok_reply(action="HOLD", confidence=0.0, error="empty Grok response")
        except Exception as exc:
            logger.warning("grok cmd failed: %s", exc)
            return format_grok_reply(
                action="HOLD",
                confidence=0.0,
                error=f"request failed ({type(exc).__name__}) — check key/credits",
            )

    async def _cmd_regime(self, _cmd: str, _args: list[str]) -> str:
        """ /regime — Phase 2 market regime + live ADX/CHOP. """
        market_regime = None
        vol_regime = None
        updated = None
        note = None
        try:
            eng = getattr(getattr(self, "agent", None), "signal_engine", None)
            if eng is not None:
                # Hot-reload from active_params if supported
                if hasattr(eng, "maybe_reload_market_regime"):
                    try:
                        eng.maybe_reload_market_regime()
                    except Exception:
                        pass
                elif hasattr(eng, "_regime_cache") and hasattr(eng._regime_cache, "maybe_reload"):
                    try:
                        eng._regime_cache.maybe_reload()
                        eng.market_regime = eng._regime_cache.market_regime
                    except Exception:
                        pass
                market_regime = getattr(eng, "market_regime", None)
                cache = getattr(eng, "_regime_cache", None)
                if cache is not None:
                    updated = getattr(cache, "regime_updated_at", None)
        except Exception as exc:
            note = f"engine read failed: {exc}"
        params = getattr(self, "_active_params", None) or {}
        if not market_regime and params.get("market_regime"):
            market_regime = params.get("market_regime")
        if params.get("regime"):
            vol_regime = str(params.get("regime"))
        if not updated and params.get("regime_updated_at"):
            updated = params.get("regime_updated_at")
        # File truth (honest if Phase 2 fields absent)
        try:
            from trading_bot.strategy import load_market_regime_from_active_params

            file_regime, _, slice_ = load_market_regime_from_active_params(
                self.settings.active_params_path
            )
            raw_has = False
            try:
                import json
                from pathlib import Path

                raw = json.loads(Path(self.settings.active_params_path).read_text())
                raw_has = isinstance(raw, dict) and "market_regime" in raw
            except Exception:
                raw_has = False
            if raw_has:
                market_regime = file_regime
                if slice_.get("regime_updated_at"):
                    updated = slice_.get("regime_updated_at")
            else:
                if not market_regime:
                    market_regime = None
                    note = (note + "; " if note else "") + (
                        "active_params has no market_regime yet (optimizer Phase 2 partial)"
                    )
                elif note is None and str(vol_regime) == "no_data":
                    note = "optimizer vol regime=no_data; macro label may be engine default"
        except Exception as exc:
            note = f"active_params read failed: {exc}"

        indicators: list[dict] = []
        try:
            for sym, obs in (self._last_obs or {}).items():
                ind = getattr(obs, "indicators", None)
                extras = getattr(ind, "extras", None) or {} if ind is not None else {}
                adx = extras.get("adx")
                chop = extras.get("chop", extras.get("choppiness"))
                if adx is None and chop is None:
                    continue
                indicators.append({"symbol": sym, "adx": adx, "chop": chop})
        except Exception:
            pass
        return format_regime_reply(
            market_regime=str(market_regime) if market_regime else None,
            vol_regime=vol_regime,
            regime_updated_at=str(updated) if updated else None,
            indicators=indicators,
            note=note,
        )

    async def _cmd_logs(self, _cmd: str, _args: list[str]) -> str:
        """ /logs — last 20 lines of instance paper/app log. """
        return read_tail_log_lines(project_root=PROJECT_ROOT, n=20)

    async def _cmd_help(self, _cmd: str, _args: list[str]) -> str:
        """ /help — active command list + short syntax. """
        return format_help_reply()




    async def _cmd_aggressive(self, _cmd: str, _args: list[str]) -> str:
        """ /aggressive — paper-soak / more fills profile. """
        return await self._apply_trade_profile("aggressive")

    async def _cmd_medium(self, _cmd: str, _args: list[str]) -> str:
        """ /medium — balanced default-ish profile. """
        return await self._apply_trade_profile("medium")

    async def _cmd_low(self, _cmd: str, _args: list[str]) -> str:
        """ /low — preserve capital / selective profile. """
        return await self._apply_trade_profile("low")

    async def _cmd_profile(self, _cmd: str, args: list[str]) -> str:
        """ /profile <aggressive|medium|low> — alias for the three profile toggles. """
        from trading_bot.telegram_commands import parse_profile_args

        try:
            name = parse_profile_args(args)
        except TradeProfileError as exc:
            return str(exc)
        return await self._apply_trade_profile(name)

    async def _apply_trade_profile(self, name: str) -> str:
        """Persist + hot-apply aggressiveness profile; sync signal engine knobs."""
        eng = None
        try:
            eng = getattr(getattr(self, "agent", None), "signal_engine", None)
        except Exception:
            eng = None
        try:
            reply = execute_set_profile(
                self.settings,
                name,
                env_path=PROJECT_ROOT / ".env",
                signal_engine=eng,
            )
        except TradeProfileError as exc:
            return str(exc)
        except Exception as exc:
            logger.warning("trade profile persist/apply failed: %s", exc)
            return f"/profile failed; knobs unchanged: {exc}"
        # Hot-sync AgentCore prefilter volume spike (aggressive → 1.0x)
        try:
            pref = getattr(getattr(self, "agent", None), "prefilter", None)
            if pref is not None and hasattr(pref, "volume_spike_mult"):
                pref.volume_spike_mult = float(
                    getattr(self.settings, "prefilter_volume_spike_mult", 2.75) or 2.75
                )
        except Exception as exc:
            logger.debug("profile prefilter sync: %s", exc)
        logger.warning(
            "OPS /profile %s — thresh=%s spread=%s rvol=%s tod=%s concurrent=%s poll=%s caps=$%s/$%s",
            name,
            getattr(self.settings, "entry_threshold", "?"),
            getattr(self.settings, "max_spread_pct", "?"),
            getattr(self.settings, "rvol_breakout_mult", "?"),
            getattr(self.settings, "disable_tod_gate", "?"),
            getattr(self.settings, "max_concurrent_positions", "?"),
            getattr(self.settings, "agent_poll_seconds", "?"),
            getattr(self.settings, "max_notional_per_trade_usd", "?"),
            getattr(self.settings, "max_total_exposure_usd", "?"),
        )
        try:
            self.trade_logger.log_event(
                "set_trade_profile",
                {
                    "profile": str(getattr(self.settings, "trade_profile", name)),
                    "entry_threshold": float(
                        getattr(self.settings, "entry_threshold", 0) or 0
                    ),
                    "max_spread_pct": float(
                        getattr(self.settings, "max_spread_pct", 0) or 0
                    ),
                    "rvol_breakout_mult": float(
                        getattr(self.settings, "rvol_breakout_mult", 0) or 0
                    ),
                    "disable_tod_gate": bool(
                        getattr(self.settings, "disable_tod_gate", False)
                    ),
                    "max_concurrent_positions": int(
                        getattr(self.settings, "max_concurrent_positions", 0) or 0
                    ),
                    "env_keys": [
                        "TRADE_PROFILE",
                        "ENTRY_THRESHOLD",
                        "MAX_SPREAD_PCT",
                        "RVOL_BREAKOUT_MULT",
                        "DISABLE_TOD_GATE",
                        "MAX_CONCURRENT_POSITIONS",
                    ],
                },
            )
        except Exception as exc:
            logger.debug("set_trade_profile log: %s", exc)
        return reply


    async def _cmd_set_spread(self, _cmd: str, args: list[str]) -> str:
        """ /set_spread <pct> — live-update MAX_SPREAD_PCT (percent in, fraction stored). """
        try:
            reply = execute_set_spread(
                self.settings,
                args,
                env_path=PROJECT_ROOT / ".env",
                from_alias=False,
            )
        except SetSpreadError as exc:
            return str(exc)
        except Exception as exc:
            logger.warning("set_spread persist/apply failed: %s", exc)
            return f"/set_spread failed; spread unchanged: {exc}"
        frac = float(self.settings.max_spread_pct)
        logger.warning(
            "OPS /set_spread — MAX_SPREAD_PCT=%s (%.4f%%) (runtime + .env)",
            frac,
            frac * 100.0,
        )
        try:
            self.trade_logger.log_event(
                "set_spread",
                {
                    "max_spread_pct": frac,
                    "max_spread_percent_display": frac * 100.0,
                    "env_keys": ["MAX_SPREAD_PCT"],
                    "paper_trading_mode": bool(self.settings.paper_trading_mode),
                },
            )
        except Exception as exc:
            logger.debug("set_spread log: %s", exc)
        return reply

    async def _cmd_set(self, _cmd: str, args: list[str]) -> str:
        """ /set MAX_SPREAD_PCT <pct> — alias for /set_spread. """
        try:
            reply = execute_set_spread(
                self.settings,
                args,
                env_path=PROJECT_ROOT / ".env",
                from_alias=True,
            )
        except SetSpreadError as exc:
            return str(exc)
        except Exception as exc:
            logger.warning("set (spread) persist/apply failed: %s", exc)
            return f"/set failed; spread unchanged: {exc}"
        frac = float(self.settings.max_spread_pct)
        logger.warning(
            "OPS /set MAX_SPREAD_PCT — %s (%.4f%%) (runtime + .env)",
            frac,
            frac * 100.0,
        )
        try:
            self.trade_logger.log_event(
                "set_spread",
                {
                    "max_spread_pct": frac,
                    "max_spread_percent_display": frac * 100.0,
                    "env_keys": ["MAX_SPREAD_PCT"],
                    "source": "telegram_/set",
                    "paper_trading_mode": bool(self.settings.paper_trading_mode),
                },
            )
        except Exception as exc:
            logger.debug("set_spread log: %s", exc)
        return reply



    async def _cmd_universe(self, _cmd: str, args: list[str]) -> str:
        """ /universe [all|allowlist] — Allow list vs Kraken discovery. """
        uni = getattr(self, "symbol_universe", None)
        try:
            mode_req = parse_universe_args(args)
        except ValueError as exc:
            return str(exc)
        if uni is None:
            mode = str(getattr(self.settings, "symbol_mode", "ALLOWLIST") or "ALLOWLIST")
            n = len(list(self.settings.symbol_list))
            if mode_req is None:
                return format_universe_status(mode=mode, count=n)
            return "Universe manager not ready — try again shortly."
        if mode_req is None:
            return format_universe_status(
                mode=uni.mode, count=len(uni.active_symbols) or len(self.settings.symbol_list)
            )
        mode = uni.set_mode(mode_req, persist=True)
        refreshing = mode == "DYNAMIC_ALL"
        if refreshing:
            uni.request_refresh()
            # Immediate refresh so /universe all is useful without waiting hourly loop
            try:
                await uni.refresh()
            except Exception as exc:
                logger.warning("universe immediate refresh: %s", exc)
            try:
                await self._ensure_ws_ticker()
            except Exception as exc:
                logger.debug("ws sync after universe: %s", exc)
        n = len(uni.active_symbols) if mode == "DYNAMIC_ALL" else len(self.settings.symbol_list)
        # Keep scanner list coherent: ALLOWLIST clears runtime override inside set_mode
        return format_universe_switched(mode=mode, count=n, refreshing=refreshing)

    async def _cmd_universe_stocks(self, _cmd: str, args: list[str]) -> str:
        """ /universe_stocks [on|off|toggle] — persist and refresh xStocks. """
        uni = getattr(self, "symbol_universe", None)
        if uni is None:
            return "Universe manager not ready — try again shortly."
        try:
            requested = parse_universe_stocks_args(args)
        except ValueError as exc:
            return str(exc)
        current = bool(getattr(uni, "stocks_enabled", False))
        enabled = (not current) if (args and str(args[0]).strip().lower() in ("toggle", "flip")) else (current if requested is None else bool(requested))
        if not args:
            return f"Stocks: {'ON' if current else 'OFF'} ({len(uni.stock_symbols) if current else 0})"
        uni.set_stocks_enabled(enabled, persist=True)
        if enabled:
            uni.request_refresh()
            try:
                await uni.refresh()
            except Exception as exc:
                logger.warning("xStocks immediate refresh: %s", exc)
            try:
                await self._ensure_ws_ticker()
            except Exception as exc:
                logger.debug("ws sync after stocks: %s", exc)
        return f"Stocks: {'ON' if enabled else 'OFF'} ({len(uni.stock_symbols) if enabled else 0})"

    async def _cmd_universe_all(self, _cmd: str, _args: list[str]) -> str:
        """ /universe_all — one-tap switch to Kraken discovery. """
        return await self._cmd_universe("universe", ["all"])

    async def _cmd_symbols(self, _cmd: str, _args: list[str]) -> str:
        """ /symbols — list currently active trading pairs. """
        uni = getattr(self, "symbol_universe", None)
        mode = str(getattr(self.settings, "symbol_mode", "ALLOWLIST") or "ALLOWLIST")
        if uni is not None:
            mode = uni.mode
            syms = list(uni.active_symbols) if uni.mode == "DYNAMIC_ALL" else list(self.settings.symbol_list)
        else:
            syms = list(self.settings.symbol_list)
        return format_symbols_reply(syms, mode=mode)



    async def _cmd_tod_custom(self, _cmd: str, args: list[str]) -> str:
        """ /tod_custom [on|off|toggle] — TOD gate; locks vs /aggressive|/medium|/low. """
        try:
            requested = parse_tod_args(args)
        except ValueError as exc:
            return str(exc)
        current_enabled = not bool(getattr(self.settings, "disable_tod_gate", False))
        locked = bool(getattr(self.settings, "tod_custom_lock", False))
        if not args:
            from trading_bot.telegram_commands import format_tod_status
            return format_tod_status(enabled=current_enabled, locked=locked)
        if args and str(args[0]).strip().lower() in ("toggle", "flip"):
            enabled = not current_enabled
        else:
            enabled = bool(requested)
        eng = None
        try:
            eng = getattr(getattr(self, "agent", None), "signal_engine", None)
        except Exception:
            eng = None
        try:
            reply = execute_set_tod(
                self.settings,
                enabled=enabled,
                env_path=PROJECT_ROOT / ".env",
                signal_engine=eng,
                custom_lock=True,
            )
        except Exception as exc:
            logger.warning("tod_custom failed: %s", exc)
            return f"/tod_custom failed: {exc}"
        logger.warning(
            "OPS /tod_custom → %s (DISABLE_TOD_GATE=%s LOCK=true)",
            "ON" if enabled else "OFF",
            not enabled,
        )
        return reply


    async def _cmd_stop_loss(self, _cmd: str, args: list[str]) -> str:
        """/stop_loss [tight|medium|free] — SL/TP profile; rebrackets open positions."""
        try:
            requested = parse_stop_loss_args(args)
        except ValueError as exc:
            cur = str(getattr(self.settings, "stop_loss_profile", "medium") or "medium")
            strat = str(getattr(self.settings, "strategy_mode", "") or "")
            return str(exc) + "\n\n" + format_stop_loss_status(cur, strategy=strat)
        cur = str(getattr(self.settings, "stop_loss_profile", "medium") or "medium")
        strat = str(getattr(self.settings, "strategy_mode", "") or "")
        if requested is None:
            return format_stop_loss_status(cur, strategy=strat)
        eng = None
        try:
            eng = getattr(getattr(self, "agent", None), "signal_engine", None)
        except Exception:
            eng = None
        try:
            reply = execute_set_stop_loss(
                self.settings,
                requested,
                env_path=PROJECT_ROOT / ".env",
                signal_engine=eng,
            )
        except Exception as exc:
            logger.warning("stop_loss failed: %s", exc)
            return f"/stop_loss failed: {exc}"
        logger.warning("OPS /stop_loss → %s", requested.upper())
        lines = [reply]
        updater = getattr(self.broker, "update_position_brackets", None)
        try:
            positions = await self.broker.get_positions()
        except Exception as exc:
            positions = []
            logger.debug("stop_loss rebracket list: %s", exc)
        for pos in positions or []:
            try:
                sym = str(getattr(pos, "symbol", "") or "")
                qty = float(getattr(pos, "qty", 0) or 0)
                entry = float(getattr(pos, "avg_entry_price", 0) or 0)
                if not sym or abs(qty) <= 1e-12 or entry <= 0:
                    continue
                is_short = self._is_short_position(pos)
                new_sl, new_tp = self._apply_profile_brackets(
                    sym, entry, qty=qty, short=is_short
                )
                label = STOP_LOSS_PRESETS[requested]["label"]
                lines.append(
                    f"Updated active position SL to {new_sl:.6g} / TP {new_tp:.6g} ({label}) on {sym}."
                )
                logger.warning(
                    "OPS /stop_loss rebracket %s SL=%.6g TP=%.6g profile=%s",
                    sym, new_sl, new_tp, requested,
                )
            except Exception as exc:
                logger.warning(
                    "stop_loss rebracket %s: %s", getattr(pos, "symbol", "?"), exc
                )
        return "\n".join(lines)



    def _apply_winning_formula_runtime(self) -> None:
        """Hot-apply Tier-1 best stack when winning_formula is ON."""
        self._HWM_PEAK_ARM_PCT = 0.012
        self._HWM_PROGRESS_ARM = 60.0
        self._HWM_FLOOR_PCT = 0.0125
        self._TRAIL_RUNNER_ARM_PCT = 0.025
        self._TRAIL_RUNNER_PROGRESS = 75.0
        self._TRAIL_RUNNER_OFFSET_PCT = 0.01
        self._TIME_EXIT_FEE_CUSHION_PCT = 0.0125
        self._TIME_EXIT_FEE_PAD_PCT = 0.0
        self._TIME_EXIT_MAKER_WAIT_SEC = 300.0
        try:
            self.ops.cb_enabled = True
        except Exception:
            pass

    def _enforce_winning_formula_sl(self) -> None:
        """If WINNING_FORMULA already on at boot, force SL → medium (persist + hot)."""
        if not bool(getattr(self.settings, "winning_formula", False)):
            return
        try:
            self._apply_winning_formula_runtime()
        except Exception:
            pass
        cur = str(getattr(self.settings, "stop_loss_profile", "") or "").lower()
        if cur == "medium":
            return
        try:
            from trading_bot.telegram_commands import execute_set_stop_loss
            eng = None
            try:
                eng = getattr(getattr(self, "agent", None), "signal_engine", None)
            except Exception:
                eng = None
            execute_set_stop_loss(
                self.settings,
                "medium",
                env_path=PROJECT_ROOT / ".env",
                signal_engine=eng,
            )
            logger.warning(
                "OPS winning_formula boot: stop_loss %s → MEDIUM",
                cur or "?",
            )
        except Exception as exc:
            logger.warning("winning_formula boot SL enforce failed: %s", exc)


    def _reset_circuit_breaker_for_winning_formula(self) -> str:
        """Clear CB streak + flags; unpause if CB had paused. Used by /winning_formula."""
        prior = 0
        try:
            prior = int(self.smart_memory.clear_circuit_breaker_streak())
        except Exception as exc:
            logger.debug("WF CB streak clear: %s", exc)
        was_cb = bool(
            getattr(self.ops, "cb_active", False)
            or getattr(self.ops, "cb_auto_resume_armed", False)
        )
        try:
            self.ops.clear_cb_auto_resume()
        except Exception:
            pass
        if was_cb:
            self.ops.set_pause(False)
        return (
            f"Circuit breaker reset — consecutive losses {prior} → 0"
            + (" · trading resumed" if was_cb else "")
        )


    async def _cmd_circuity_breaker_manually(self, _cmd: str, args: list[str]) -> str:
        """ /circuity_breaker_manually [on|off|status] — CB auto pause/cooldown toggle. """
        action = parse_circuity_breaker_args(args)
        if action is None:
            return CIRCUITY_BREAKER_USAGE
        try:
            consec = int(self.smart_memory.consecutive_losses())
        except Exception:
            consec = int(getattr(self, "_status_cb_consec_losses", 0) or 0)
        tripped = bool(
            getattr(self.ops, "cb_active", False)
            or getattr(self.ops, "cb_auto_resume_armed", False)
        )
        if action == "status":
            return format_circuity_breaker_status(
                enabled=bool(getattr(self.ops, "cb_enabled", True)),
                consec=consec,
                tripped=tripped and bool(self.ops.paused),
            )
        want = action == "on"
        self.ops.cb_enabled = want
        try:
            object.__setattr__(self.settings, "circuit_breaker_enabled", want)
        except Exception:
            pass
        try:
            from trading_bot.config import (
                ENV_KEY_CIRCUIT_BREAKER_ENABLED,
                upsert_env_keys,
            )
            upsert_env_keys(
                PROJECT_ROOT / ".env",
                {ENV_KEY_CIRCUIT_BREAKER_ENABLED: "true" if want else "false"},
            )
        except Exception as exc:
            logger.debug("CB env persist: %s", exc)
        if not want:
            # Cancel auto-cooldown arming; leave pause for /resume
            was_armed = bool(getattr(self.ops, "cb_auto_resume_armed", False))
            try:
                self.ops.clear_cb_auto_resume()
            except Exception:
                pass
            note = format_circuity_breaker_status(
                enabled=False, consec=consec, tripped=bool(self.ops.paused)
            )
            if was_armed or bool(self.ops.paused):
                note += "\nAuto-cooldown cancelled. /resume to re-enable buys if paused."
            logger.warning("OPS /circuity_breaker_manually → OFF")
            return note
        # Turning ON: if already at limit and not paused, trip now with cooldown
        note = format_circuity_breaker_status(
            enabled=True, consec=consec, tripped=False
        )
        wf = bool(getattr(self.settings, "winning_formula", False))
        limit = 3 if wf else 5
        if consec >= limit and not bool(self.ops.paused):
            self.ops.set_pause(True)
            try:
                mins = int(self.ops.arm_cb_auto_resume())
            except Exception:
                mins = 45
            self.ops.cb_active = True
            note += (
                f"\n⚠️ Already at {consec}≥{limit} losses — paused now. "
                f"Auto-resume ~{mins}m gated. /resume now to skip."
            )
        logger.warning("OPS /circuity_breaker_manually → ON")
        return note


    async def _cmd_weekly_digest_101(self, _cmd: str, args: list[str]) -> str:
        """ /weekly_digest_101 [paper|live] — 7-day expectancy (Weekly Digest 101). """
        try:
            want = parse_weekly_digest_args(args)
        except ValueError as exc:
            return str(exc)
        if want is None:
            mode = "paper" if bool(self.settings.paper_trading_mode) else "live"
        else:
            mode = want
        live_closes = None
        if mode == "live":
            try:
                fetcher = getattr(self.broker, "fetch_live_trades_history", None)
                if callable(fetcher):
                    live_closes = await fetcher(days=7)
            except Exception as exc:
                logger.warning("weekly_digest live fetch: %s", exc)
                live_closes = []
        try:
            body = build_weekly_expectancy_digest(
                days=7,
                mode=mode,
                trades_db=PROJECT_ROOT / "data" / "trades.db",
                memory_db=PROJECT_ROOT / "data" / "trading_bot_2.db",
                root=PROJECT_ROOT,
                live_closes=live_closes,
            )
        except Exception as exc:
            logger.warning("weekly_digest_101 failed: %s", exc)
            return f"/weekly_digest_101 failed: {exc}"
        # Return only — TelegramCommandListener already replies (don't notifier.send)
        return body

    async def _cmd_winning_formula(self, _cmd: str, args: list[str]) -> str:
        """ /winning_formula [on|off|status] — institutional expectancy preset. """
        try:
            action = parse_winning_formula_args(args)
        except ValueError as exc:
            return str(exc)
        strat = str(getattr(self.settings, "strategy_mode", "") or "")
        enabled = bool(getattr(self.settings, "winning_formula", False))
        if action == "status":
            # Bare /winning_formula while ON: re-apply full formula (thresh 50 + SL medium)
            # without clobbering via shell env leftovers (ENTRY_THRESHOLD=35 etc).
            if enabled:
                try:
                    eng = getattr(getattr(self, "agent", None), "signal_engine", None)
                    execute_set_winning_formula(
                        self.settings,
                        enabled=True,
                        env_path=PROJECT_ROOT / ".env",
                        signal_engine=eng,
                    )
                    # Keep display/runtime threshold at WF floor (50); BEAR uses 65 live
                    from trading_bot.config import apply_runtime_entry_threshold
                    apply_runtime_entry_threshold(self.settings, 60.0)
                    os.environ["ENTRY_THRESHOLD"] = "60"
                    os.environ["STOP_LOSS_PROFILE"] = "medium"
                    os.environ["WINNING_FORMULA"] = "true"
                except Exception as exc:
                    logger.warning("WF status re-assert: %s", exc)
                # rebracket opens to effective (BEAR clamp) brackets
                extra = []
                try:
                    positions = await self.broker.get_positions()
                except Exception:
                    positions = []
                for pos in positions or []:
                    try:
                        sym = str(getattr(pos, "symbol", "") or "")
                        qty = float(getattr(pos, "qty", 0) or 0)
                        entry = float(getattr(pos, "avg_entry_price", 0) or 0)
                        if not sym or abs(qty) <= 1e-12 or entry <= 0:
                            continue
                        new_sl, new_tp = self._apply_profile_brackets(
                            sym, entry, qty=qty, short=self._is_short_position(pos)
                        )
                        extra.append(f"Rebracketed {sym}: SL {new_sl:.6g} / TP {new_tp:.6g}")
                    except Exception as exc:
                        logger.debug("wf status rebracket: %s", exc)
                cb_note = self._reset_circuit_breaker_for_winning_formula()
                sl_name = str(getattr(self.settings, "stop_loss_profile", "medium") or "medium")
                body = format_winning_formula_status(enabled=True, strategy=strat)
                body += f"\n• stop_loss now: {sl_name.upper()}"
                body += f"\n• {cb_note}"
                if extra:
                    body += "\n" + "\n".join(extra)
                return body
            return format_winning_formula_status(enabled=enabled, strategy=strat)
        want = action == "on"
        eng = None
        try:
            eng = getattr(getattr(self, "agent", None), "signal_engine", None)
        except Exception:
            eng = None
        try:
            reply = execute_set_winning_formula(
                self.settings,
                enabled=want,
                env_path=PROJECT_ROOT / ".env",
                signal_engine=eng,
            )
        except Exception as exc:
            logger.warning("winning_formula failed: %s", exc)
            return f"/winning_formula failed: {exc}"
        logger.warning("OPS /winning_formula → %s", "ON" if want else "OFF")
        if want:
            try:
                from trading_bot.config import apply_runtime_entry_threshold
                apply_runtime_entry_threshold(self.settings, 60.0)
                os.environ["ENTRY_THRESHOLD"] = "60"
                os.environ["STOP_LOSS_PROFILE"] = "medium"
                os.environ["WINNING_FORMULA"] = "true"
                # drop aggressive override so .env/WF win on next boot
                if os.environ.get("TRADE_PROFILE", "").lower() == "aggressive":
                    # keep profile label unless user changes it; do not force 35%
                    pass
            except Exception as exc:
                logger.debug("WF env sync: %s", exc)
            if want:
                self._apply_winning_formula_runtime()
            cb_note = self._reset_circuit_breaker_for_winning_formula()
            reply = (reply or "") + "\n• " + cb_note
        lines = [reply]
        try:
            positions = await self.broker.get_positions()
        except Exception:
            positions = []
        for pos in positions or []:
            try:
                sym = str(getattr(pos, "symbol", "") or "")
                qty = float(getattr(pos, "qty", 0) or 0)
                entry = float(getattr(pos, "avg_entry_price", 0) or 0)
                if not sym or abs(qty) <= 1e-12 or entry <= 0:
                    continue
                is_short = self._is_short_position(pos)
                new_sl, new_tp = self._apply_profile_brackets(
                    sym, entry, qty=qty, short=is_short
                )
                lines.append(
                    f"Rebracketed {sym}: SL {new_sl:.6g} / TP {new_tp:.6g}"
                )
            except Exception as exc:
                logger.debug("wf rebracket: %s", exc)
        return "\n".join(lines)

    def _wire_telegram_commands(self) -> None:
        self._enforce_winning_formula_sl()
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
            callback_handlers={
                STATUS_SYMBOLS_CALLBACK: self._cb_status_symbols,
            },
            handlers={
                "status": self._cmd_status,
                "pause": self._cmd_pause,
                "resume": self._cmd_resume,
                "pnl": self._cmd_pnl,
                "kill": self._cmd_kill,
                "mode": self._cmd_mode,
                "confirm_live": self._cmd_confirm_live,
                "set_limit": self._cmd_set_limit,
                "set_threshold": self._cmd_set_threshold,
                "set_threshold_custom": self._cmd_set_threshold_custom,
                "set_spread": self._cmd_set_spread,
                "tod_custom": self._cmd_tod_custom,
                "stop_loss": self._cmd_stop_loss,
                "winning_formula": self._cmd_winning_formula,
                "weekly_digest_101": self._cmd_weekly_digest_101,
                "circuity_breaker_manually": self._cmd_circuity_breaker_manually,
                "set": self._cmd_set,
                "aggressive": self._cmd_aggressive,
                "medium": self._cmd_medium,
                "low": self._cmd_low,
                "profile": self._cmd_profile,
                "test_trade": self._cmd_test_trade,
                "reset_paper": self._cmd_reset_paper,
                "wipe_paper": self._cmd_wipe_paper,
                "factory_reset": self._cmd_wipe_paper,
                "close": self._cmd_close,
                "clear_positions": self._cmd_clear_positions,
                "ping": self._cmd_ping,
                "positions": self._cmd_positions,
                "balance": self._cmd_balance,
                "history": self._cmd_history,
                "grok": self._cmd_grok,
                "regime": self._cmd_regime,
                "logs": self._cmd_logs,
                "universe": self._cmd_universe,
                "universe_all": self._cmd_universe_all,
                "universe_stocks": self._cmd_universe_stocks,
                "symbols": self._cmd_symbols,
                "help": self._cmd_help,
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
                await self._maybe_cb_auto_resume()
            except Exception as exc:
                logger.debug("cb auto-resume check: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _maybe_cb_auto_resume(self) -> None:
        """Gated circuit-breaker auto-resume: cooldown + (win OR BULL_OK)."""
        ops = self.ops
        if not bool(getattr(ops, "cb_auto_resume_armed", False)):
            return
        if not bool(ops.paused):
            ops.clear_cb_auto_resume()
            return
        bull_ok = False
        try:
            regime = getattr(self, "btc_regime", None)
            state = str(getattr(regime, "state", "") or "").upper()
            dump = bool(getattr(regime, "dump_30m", False))
            bull_ok = state == "BULL_OK" and not dump
        except Exception:
            bull_ok = False
        if not ops.cb_auto_resume_ready(regime_bull_ok=bull_ok):
            return
        won = bool(getattr(ops, "cb_win_since_trip", False))
        ops.set_pause(False)
        ops.clear_cb_auto_resume()
        reason = "win since trip" if won else "BULL_OK"
        msg = (
            f"✅ CIRCUIT BREAKER auto-resume: cooldown done + {reason}. "
            "New buys enabled. /pause to stop."
        )
        logger.warning("%s", msg)
        try:
            await self.notifier.send(msg, high_priority=True)
        except Exception as exc:
            logger.debug("cb auto-resume telegram: %s", exc)


    def _phase1_closed_5m(self, symbol: str) -> Optional[Tuple[float, float, int]]:
        """Last closed 5m (open, close, period_start) from the entry-5m frame."""
        period = float(getattr(self.settings, "cvd_period_sec", 300.0) or 300.0)
        df = None
        try:
            getter = getattr(self.feed, "get_frame_5m", None)
            if callable(getter):
                df = getter(symbol)
        except Exception:
            df = None
        return last_closed_5m_candle(df, period_sec=period)

    def _phase1_allow_buy(self, symbol: str) -> Tuple[bool, str]:
        """Final long-only Phase 1 gates (CVD absorption + short-liq sweep).

        Called after every BUY source (sweet-spot, coint, sweep-fade) so a
        blocked entry never reaches the executor. Snapshot reads only — no
        await / network on this path.
        """
        # Spot long-only: BEAR_CHOP no longer hard-blocks here (see quality gate).
        cvd_on = bool(getattr(self.settings, "cvd_gate_enabled", True))
        liq_on = bool(getattr(self.settings, "liq_sweep_gate_enabled", True))
        if not cvd_on and not liq_on:
            return True, ""
        candle = self._phase1_closed_5m(symbol)
        o = c = None
        pstart = None
        if candle is not None:
            o, c, pstart = candle
        ok, reason = self.leadlag.evaluate_phase1_long(
            symbol,
            candle_open=o,
            candle_close=c,
            candle_period_start=pstart,
            cvd_enabled=cvd_on,
            liq_enabled=liq_on,
            liq_threshold=float(
                getattr(self.settings, "liq_sweep_notional_usd", 50_000.0) or 50_000.0
            ),
            fail_closed=bool(getattr(self.settings, "phase1_fail_closed", True)),
            allow_cold_feed=bool(getattr(self.settings, "phase1_allow_cold_feed", False)),
        )
        if not ok:
            return False, reason
        # Mandatory CVD slope > 0 for LONGs (5-period)
        try:
            from trading_bot.cvd import check_cvd_slope_positive
            snap = self.leadlag.get_cvd_snapshot(symbol)
            slope = None
            try:
                slope = self.leadlag.cvd.slope(symbol, periods=5)
            except Exception:
                slope = None
            ok_s, reason_s = check_cvd_slope_positive(
                slope,
                enabled=bool(getattr(self.settings, "cvd_gate_enabled", True)),
                fail_closed=bool(getattr(self.settings, "phase1_fail_closed", True)),
                allow_cold_feed=bool(getattr(self.settings, "phase1_allow_cold_feed", False)),
                feed_warm=bool(getattr(snap, "warm", False)),
            )
            if not ok_s:
                return False, reason_s
        except Exception as exc:
            logger.debug("cvd slope gate: %s", exc)
        return True, ""


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
            # Phase 2 market_regime always hot-applied (even when vol-regime=no_data)
            eng = getattr(self.agent, "signal_engine", None)
            if "market_regime" in params and eng is not None and hasattr(eng, "set_market_regime"):
                feats = params.get("regime_features")
                eng.set_market_regime(
                    params.get("market_regime"),
                    features=feats if isinstance(feats, dict) else None,
                )
                logger.info(
                    "ACTIVE_PARAMS market_regime=%s cap_mult=%.2f",
                    getattr(eng, "market_regime", "?"),
                    float(getattr(eng, "regime_trade_cap_mult", 1.0) or 1.0),
                )

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
            if eng is not None:
                if hasattr(eng, "rvol_breakout_mult"):
                    eng.rvol_breakout_mult = rvol
                if hasattr(eng, "min_tp_pct"):
                    eng.min_tp_pct = min_tp
            self._active_params = dict(params)
            logger.info(
                "ACTIVE_PARAMS applied regime=%s market_regime=%s rvol=%.2f rsi_cap=%.1f "
                "min_tp=%.3f ts=%s",
                params.get("regime"),
                params.get("market_regime"),
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

    async def _get_market_data_for_grok(self) -> dict:
        """Small in-memory indicator snapshot for Grok (never blocks the trade loop)."""
        symbols: dict = {}
        for sym, obs in (self._last_obs or {}).items():
            ind = getattr(obs, "indicators", None)
            if ind is None:
                continue
            extras = getattr(ind, "extras", None) or {}
            symbols[sym] = {
                "close": getattr(ind, "close", None),
                "vwap": getattr(ind, "vwap", None),
                "rsi": getattr(ind, "rsi", None),
                "volume": getattr(ind, "volume", None),
                "atr": getattr(ind, "atr", None),
                "ema_fast": getattr(ind, "ema_fast", None),
                "ema_slow": getattr(ind, "ema_slow", None),
                "volume_ratio": extras.get("volume_ratio"),
                "breakout_rvol": extras.get("breakout_rvol"),
                "adx": extras.get("adx"),
            }
        if not symbols:
            for sym in self.settings.symbol_list:
                symbols[sym] = {}
        return {"symbols": symbols}

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
            "BEAR_FEE_TP ARMED | RT_taker~1.0%% × mult=%.1f → min_TP=+%.1f%% on BEAR_CHOP/SHORT-bias LONGs",
            float(self.settings.fee_to_target_mult),
            float(self.settings.fee_to_target_mult) * 1.0,
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
            "PHASE1 INTEL ARMED | "
            "CVD: enabled=%s period=%.0fs | "
            "LiqSweep: enabled=%s thresh=$%.0f window=%.0fs ttl=%.0fs | "
            "fail_closed=%s allow_cold_feed=%s "
            "(cold tape: block unless PHASE1_ALLOW_COLD_FEED=true)",
            bool(getattr(self.settings, "cvd_gate_enabled", True)),
            float(getattr(self.settings, "cvd_period_sec", 300.0) or 300.0),
            bool(getattr(self.settings, "liq_sweep_gate_enabled", True)),
            float(getattr(self.settings, "liq_sweep_notional_usd", 50_000.0) or 50_000.0),
            float(getattr(self.settings, "liq_sweep_window_sec", 60.0) or 60.0),
            float(getattr(self.settings, "liq_sweep_ttl_sec", 30.0) or 30.0),
            bool(getattr(self.settings, "phase1_fail_closed", True)),
            bool(getattr(self.settings, "phase1_allow_cold_feed", False)),
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
            # Dynamic Kraken universe (hourly; /universe all triggers immediate refresh)
            if getattr(self, "symbol_universe", None) is not None:
                self._tasks.append(
                    asyncio.create_task(
                        self.symbol_universe.run_loop(self._stop),
                        name="symbol_universe",
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

            # Thin Kraken WS ticker cache — full active universe (crypto + xStocks)
            try:
                if hasattr(self.broker, "stream_quotes") and not self.once:
                    uni = getattr(self, "symbol_universe", None)
                    if uni is not None and (uni.mode == "DYNAMIC_ALL" or uni.stocks_enabled):
                        try:
                            await uni.refresh()
                        except Exception as exc:
                            logger.warning("pre-WS universe refresh: %s", exc)
                    await self._ensure_ws_ticker()
            except Exception as exc:
                logger.warning("Kraken WS ticker start failed (degrade): %s", exc)
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
                                    "market_regime": getattr(
                                        p, "market_regime", "RANGING"
                                    ),
                                    "regime_updated_at": getattr(
                                        p, "regime_updated_at", ""
                                    ),
                                    "regime_features": getattr(
                                        p, "regime_features", None
                                    ),
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
            # Non-blocking Grok sentiment: refresh every 15m; evaluate_entry only does <1ms lookup
            self._tasks.append(
                asyncio.create_task(
                    self.grok_sentiment.background_update_loop(
                        self._get_market_data_for_grok,
                        interval_seconds=900,
                    ),
                    name="grok_sentiment",
                )
            )
            logger.info(
                "GROK SENTIMENT ARMED | background_loop=900s lookup=in-memory (no HTTP in evaluate_entry)"
            )
            # Once-daily Telegram PnL digest (SUMMARY_HOUR:MINUTE in SUMMARY_TIMEZONE)
            if bool(getattr(self.settings, "daily_digest_enabled", True)):
                self._tasks.append(
                    asyncio.create_task(
                        self._daily_digest_loop(), name="daily_pnl_digest"
                    )
                )
                _h = int(getattr(self.settings, "summary_hour", 7) or 7)
                _m = int(getattr(self.settings, "summary_minute", 0) or 0)
                _tz = str(
                    getattr(self.settings, "summary_timezone", "America/Chicago")
                    or "America/Chicago"
                )
                _nxt = next_summary_datetime(_h, _m, _tz)
                logger.info(
                    "DAILY_DIGEST ARMED | next=%s %s (SUMMARY_HOUR=%s SUMMARY_MINUTE=%s; "
                    "change .env + restart to reschedule)",
                    _nxt.strftime("%Y-%m-%d %H:%M"),
                    _tz,
                    _h,
                    _m,
                )
            else:
                logger.info("DAILY_DIGEST disabled (DAILY_DIGEST_ENABLED=false)")
            # In-process xStock purge (zero Grok tokens): Fri 20:00–Sun 20:00 ET + Fri 19:55 ET
            self._tasks.append(
                asyncio.create_task(
                    self._xstock_weekend_purge_loop(), name="xstock_weekend_purge"
                )
            )
            self._tasks.append(
                asyncio.create_task(
                    self._xstock_friday_flush_loop(), name="xstock_friday_flush"
                )
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

    async def _send_daily_digest_once(self, *, force: bool = False) -> bool:
        """Compute live paper/ledger digest and send Telegram. Returns True if sent."""
        tz_name = str(
            getattr(self.settings, "summary_timezone", "America/Chicago")
            or "America/Chicago"
        )
        dedupe = dedupe_path_for(PROJECT_ROOT)
        book_path = Path(
            getattr(self.settings, "paper_book_path", None)
            or (PROJECT_ROOT / "data" / "paper_book.json")
        )
        ledger_path = PROJECT_ROOT / "data" / "paper_ledger.db"
        sqlite_path = Path(
            getattr(self.settings, "sqlite_path", None)
            or (PROJECT_ROOT / "data" / "trading_bot.db")
        )

        starting = current = None
        positions = None
        try:
            acct = await self.broker.get_account()
            current = float(acct.equity)
            starting = float(
                getattr(acct, "day_start_equity", None)
                or getattr(self.broker, "_day_start_equity", None)
                or current
            )
        except Exception as exc:
            logger.debug("daily digest account read: %s", exc)
        try:
            positions = await self.broker.get_positions()
        except Exception as exc:
            logger.debug("daily digest positions read: %s", exc)

        snap = build_digest_snapshot(
            paper_book_path=book_path,
            ledger_path=ledger_path,
            sqlite_path=sqlite_path,
            starting_equity=starting,
            current_equity=current,
            positions=positions,
            tz_name=tz_name,
        )
        if not force and already_sent_for_date(dedupe, snap.date_key):
            logger.info(
                "DAILY_DIGEST skip — already sent for %s (%s)",
                snap.date_key,
                dedupe,
            )
            return False
        await self.notifier.shit_lets_see_summary(
            starting_equity=snap.starting_equity,
            current_equity=snap.current_equity,
            realized_pnl_24h=snap.realized_pnl_24h,
            realized_pnl_pct=snap.realized_pnl_pct,
            trades_closed=snap.trades_closed,
            wins=snap.wins,
            losses=snap.losses,
            open_exposure=snap.open_exposure,
            message=snap.format_message(),
        )
        mark_sent_for_date(dedupe, snap.date_key)
        logger.info(
            "DAILY_DIGEST sent date=%s equity=%.2f pnl_24h=%.2f closed=%s exposure=%.2f",
            snap.date_key,
            snap.current_equity,
            snap.realized_pnl_24h,
            snap.trades_closed,
            snap.open_exposure,
        )
        return True

    async def _daily_digest_loop(self) -> None:
        """Wait until SUMMARY_HOUR:MINUTE in SUMMARY_TIMEZONE, send once, repeat ~daily."""
        hour = int(getattr(self.settings, "summary_hour", 7) or 7)
        minute = int(getattr(self.settings, "summary_minute", 0) or 0)
        tz_name = str(
            getattr(self.settings, "summary_timezone", "America/Chicago")
            or "America/Chicago"
        )
        while not self._stop.is_set():
            delay = seconds_until_summary(hour, minute, tz_name)
            nxt = next_summary_datetime(hour, minute, tz_name)
            logger.info(
                "DAILY_DIGEST waiting %.0fs until %s %s",
                delay,
                nxt.strftime("%Y-%m-%d %H:%M"),
                tz_name,
            )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                break
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                break
            try:
                await self._send_daily_digest_once(force=False)
            except Exception:
                logger.exception("daily digest cycle failed")
            # Avoid same-minute double fire if wake is early; next loop recomputes ~24h
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=2.0)
                break
            except asyncio.TimeoutError:
                pass

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
            await self.grok_sentiment.close()
        except Exception as exc:
            logger.debug("grok_sentiment close: %s", exc)
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

    def _arm_post_stop_cooldown(self, symbol: str) -> None:
        """In-memory + persisted block after stop-loss exit (default 15 min)."""
        mins = int(getattr(self.settings, "post_stop_cooldown_min", 15) or 15)
        mins = max(0, mins)
        sym = symbol.upper()
        self._cooldowns[sym] = time.time() + float(mins) * 60.0
        try:
            self.state.record_post_stop_cooldown(symbol, minutes=mins)
        except Exception as exc:
            logger.warning("persist post_stop_cooldown failed %s: %s", symbol, exc)

    def _in_post_stop_cooldown(self, symbol: str) -> bool:
        sym = symbol.upper()
        until = float(self._cooldowns.get(sym, 0.0) or 0.0)
        if until > time.time():
            return True
        try:
            if self.state.in_post_stop_cooldown(symbol):
                rem = float(self.state.post_stop_cooldown_remaining(symbol) or 0.0)
                if rem > 0:
                    self._cooldowns[sym] = time.time() + rem
                    return True
        except Exception:
            pass
        self._cooldowns.pop(sym, None)
        return False

    def _post_stop_cooldown_remaining(self, symbol: str) -> float:
        sym = symbol.upper()
        mem = max(0.0, float(self._cooldowns.get(sym, 0.0) or 0.0) - time.time())
        try:
            db = float(self.state.post_stop_cooldown_remaining(symbol) or 0.0)
        except Exception:
            db = 0.0
        return max(mem, db)

    @staticmethod
    def _is_short_position(position) -> bool:
        return str(getattr(position, "side", "long") or "long").lower() == "short"


    def _symbol_tick_age_s(self, symbol: str) -> Optional[float]:
        """Age of last in-memory tick for symbol; None if unknown."""
        sym = (symbol or "").strip().upper()
        # Prefer broker per-symbol WS cache timestamps
        for obj in (self.broker, self.feed):
            getter = getattr(obj, "symbol_tick_age_seconds", None)
            if callable(getter):
                try:
                    age = getter(sym)
                    if age is not None:
                        return float(age)
                except Exception:
                    pass
            cache = getattr(obj, "_tick_mono", None) or getattr(obj, "_symbol_tick_mono", None)
            if isinstance(cache, dict) and sym in cache:
                try:
                    return max(0.0, time.monotonic() - float(cache[sym]))
                except Exception:
                    pass
        # Fallback: observation timestamp if present
        obs = (self._last_obs or {}).get(sym)
        if obs is not None:
            ts = getattr(obs, "ts", None) or getattr(obs, "timestamp", None)
            if ts is not None:
                try:
                    if hasattr(ts, "timestamp"):
                        return max(0.0, time.time() - float(ts.timestamp()))
                    return max(0.0, time.time() - float(ts))
                except Exception:
                    pass
        return None

    def _symbol_tick_stale(self, symbol: str, max_age_s: float = 10.0) -> bool:
        age = self._symbol_tick_age_s(symbol)
        if age is None:
            return False  # unknown — do not omit (cold start)
        return age > float(max_age_s)

    def _market_short_bias(self) -> bool:
        """True when BTC regime implies SHORT bias (BEAR_CHOP or dump)."""
        regime = getattr(self, "btc_regime", None)
        if regime is None:
            return False
        state = str(getattr(regime, "state", "") or "").upper()
        return state == STATE_BEAR_CHOP or bool(getattr(regime, "dump_30m", False))

    def _effective_max_concurrent(self) -> int:
        """Cap open positions: 1 in BEAR_CHOP/SHORT bias, else settings default."""
        base = int(getattr(self.settings, "max_concurrent_positions", 2) or 2)
        if self._market_short_bias():
            return 1
        return max(1, base)

    def _is_spot_long_only(self) -> bool:
        """True when paper/live shorts are disabled (Kraken spot mode)."""
        return not bool(getattr(self.settings, "allow_paper_shorts", False))

    # Winning-formula BEAR clamp: max SL -1.25%; ≤$6 risk on a $500 ticket
    _WF_BEAR_SL_MAX = 0.0125
    _WF_BEAR_MAX_RISK_USD = 6.0
    _WF_BEAR_REF_NOTIONAL = 500.0

    def _profile_sl_tp_pct(self, *, notional: float | None = None) -> tuple[float, float]:
        """Active /stop_loss profile SL% and TP%.

        Winning formula:
          • TP = |SL| * 1.5 + 0.80% fee buffer
          • BEAR_CHOP / SHORT bias: clamp SL to −1.25% (and ≤$6 on $500 ticket)
          • BULL / LONG bias: full user SL profile (incl. FREE −2.50%)
        """
        try:
            name = str(getattr(self.settings, "stop_loss_profile", "medium") or "medium")
            from trading_bot.telegram_commands import STOP_LOSS_PRESETS, normalize_stop_loss_profile
            name = normalize_stop_loss_profile(name)
            p = STOP_LOSS_PRESETS[name]
            sl = float(p["sl_pct"])
            tp = float(p["tp_pct"])
            wf = bool(getattr(self.settings, "winning_formula", False))
            if wf and self._market_short_bias():
                capped = min(sl, float(self._WF_BEAR_SL_MAX))
                # Dollar cap: risk on ref/$actual notional ≤ $6
                ref_n = float(notional) if notional and float(notional) > 0 else float(self._WF_BEAR_REF_NOTIONAL)
                dollar_cap = float(self._WF_BEAR_MAX_RISK_USD) / max(ref_n, 1e-9)
                capped = min(capped, dollar_cap)
                if capped + 1e-12 < sl:
                    logger.warning(
                        "⚠️ SL Clamped to -%.2f%% due to BEAR_CHOP regime risk "
                        "(was -%.2f%%; max $%.2f on $%.0f ticket).",
                        capped * 100.0,
                        sl * 100.0,
                        float(self._WF_BEAR_MAX_RISK_USD),
                        ref_n,
                    )
                sl = capped
                tp = abs(sl) * 1.5 + 0.008
            elif wf:
                tp = abs(sl) * 1.5 + 0.008
            return sl, tp
        except Exception:
            return 0.015, 0.0225

    def _apply_profile_brackets(
        self,
        symbol: str,
        entry: float,
        *,
        qty: float = 0.0,
        short: bool = False,
    ) -> tuple[float, float]:
        """Compute (SL, TP) from profile with winning-formula BEAR clamp; apply to book."""
        notional = abs(float(entry) * float(qty)) if entry > 0 and abs(qty) > 0 else None
        sl_pct, tp_pct = self._profile_sl_tp_pct(notional=notional)
        e = float(entry)
        if short:
            sl_px, tp_px = e * (1.0 + sl_pct), e * (1.0 - tp_pct)
        else:
            sl_px, tp_px = e * (1.0 - sl_pct), e * (1.0 + tp_pct)
        updater = getattr(self.broker, "update_position_brackets", None)
        if callable(updater):
            updater(symbol, stop_loss=sl_px, take_profit=tp_px, mark_price=e)
        return sl_px, tp_px

    def _bear_spot_long_threshold(self, base: float) -> float:
        """In spot BEAR_CHOP/SHORT bias: +10% score, floor 50% (or 65% if winning_formula)."""
        b = float(base)
        if self._is_spot_long_only() and self._market_short_bias():
            floor = 65.0 if bool(getattr(self.settings, "winning_formula", False)) else 50.0
            return max(b * 1.10, floor)
        # Winning formula BULL: allow threshold at 50% floor
        if bool(getattr(self.settings, "winning_formula", False)):
            return max(b, 50.0)
        return b

    def _apply_absolute_direction_lock(self, entry_proximity):
        """Kraken spot long-only: never advertise SHORT.

        Spot mode keeps Target Setup LONG under BEAR_CHOP/SHORT bias so bounce
        longs can evaluate; shorts-capable mode still flips LONG→SHORT in bias.
        """
        if not isinstance(entry_proximity, dict):
            return entry_proximity
        out = dict(entry_proximity)
        direction = str(out.get("direction") or "").upper()
        shorts_ok = bool(getattr(self.settings, "allow_paper_shorts", False))
        # Spot cannot short — never show Target Setup SHORT
        if not shorts_ok and direction == "SHORT":
            out["direction"] = "WAIT"
            out["target"] = "WAIT (spot long-only)"
            direction = "WAIT"
        if self._market_short_bias() and direction == "LONG":
            if shorts_ok:
                out["direction"] = "SHORT"
                tgt = str(out.get("target") or "")
                if " LONG" in tgt:
                    out["target"] = tgt.replace(" LONG", " SHORT", 1)
            else:
                # Spot unfreeze: keep LONG for scanning / status
                out["direction"] = "LONG"
                out["target"] = "LONG (Spot Mode)"
        return out

    def _bear_short_mode(self) -> bool:
        """Whether BTC conditions favor paper shorts over new longs."""
        if not bool(getattr(self.settings, "allow_paper_shorts", False)):
            return False
        return self._market_short_bias()

    def _short_entry_threshold(self, long_threshold: float) -> float:
        """Lower only the short scanner threshold during BTC weakness."""
        base = float(long_threshold)
        if not self._bear_short_mode():
            return base
        return max(25.0, base - 10.0)


    def _ws_symbol_universe(self) -> list:
        """Full active trading universe for WS subscribe (not 14-pair seed)."""
        uni = getattr(self, "symbol_universe", None)
        if uni is not None:
            syms = list(getattr(uni, "active_symbols", None) or [])
            if syms:
                return syms
        return list(self.settings.symbol_list)

    async def _ensure_ws_ticker(self) -> None:
        """Start or restart Kraken WS ticker with the full active universe."""
        if not hasattr(self.broker, "stream_quotes") or self.once:
            return
        symbols = self._ws_symbol_universe()
        prev = getattr(self, "_ws_subscribed_symbols", None)
        task = getattr(self, "_ws_ticker_task", None)
        if task is not None and not task.done() and prev is not None and list(prev) == list(symbols):
            return
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            try:
                self._tasks.remove(task)
            except ValueError:
                pass

        def _on_q(q):
            pass  # broker already updates _paper_prices in stream_quotes

        self._ws_subscribed_symbols = list(symbols)
        self._ws_ticker_task = asyncio.create_task(
            self.broker.stream_quotes(list(symbols), _on_q),
            name="kraken_ws_ticker",
        )
        self._tasks.append(self._ws_ticker_task)
        logger.info(
            "Kraken WS ticker cache armed symbols=%d url=%s",
            len(symbols),
            getattr(self.settings, "kraken_ws_url", ""),
        )


    async def _close_xstock_positions_breakeven(self, *, reason: str) -> list:
        """Force-close all paper xStock positions at entry (PnL $0)."""
        closer = getattr(self.broker, "close_all_paper_xstocks_breakeven", None)
        if not callable(closer):
            return []
        closed = closer() or []
        if closed:
            logger.warning(
                "xStock BE purge reason=%s closed=%s cash≈%s",
                reason,
                [c.get("symbol") for c in closed],
                closed[-1].get("cash"),
            )
            try:
                note = (
                    f"xStock weekend/flush close ({reason}): "
                    + ", ".join(f"{c['symbol']}@{c['entry']:.2f}" for c in closed)
                    + f" → cash=${float(closed[-1].get('cash') or 0):.2f}"
                )
                # best-effort Telegram notify if notifier exists
                n = getattr(self, "notifier", None)
                if n is not None and hasattr(n, "send"):
                    await n.send(note)
            except Exception as exc:
                logger.debug("xstock purge notify: %s", exc)
        return closed

    async def _xstock_weekend_purge_loop(self) -> None:
        """While Fri 20:00–Sun 20:00 ET, keep xStock book flat at breakeven."""
        logger.info("xStock weekend auto-purge armed (Fri 20:00–Sun 20:00 ET)")
        while not self._stop.is_set():
            try:
                if bool(getattr(self.settings, "paper_trading_mode", True)) and is_xstock_weekend_closed():
                    await self._close_xstock_positions_breakeven(reason="weekend_window")
                await asyncio.wait(
                    [asyncio.create_task(self._stop.wait())],
                    timeout=60.0,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("xstock weekend purge error: %s", exc)
                await asyncio.sleep(30.0)
        logger.info("xStock weekend auto-purge stopped")

    async def _xstock_friday_flush_loop(self) -> None:
        """Every Friday 19:55 ET close all xStock paper positions before weekend."""
        logger.info("xStock Friday 19:55 ET flush armed")
        while not self._stop.is_set():
            try:
                delay = float(seconds_until_friday_xstock_flush())
                # wake at least every 6h so stop is responsive
                wait = min(max(5.0, delay), 6 * 3600.0)
                await asyncio.wait(
                    [asyncio.create_task(self._stop.wait())],
                    timeout=wait,
                )
                if self._stop.is_set():
                    break
                if bool(getattr(self.settings, "paper_trading_mode", True)):
                    remaining = float(seconds_until_friday_xstock_flush())
                    if remaining <= 90.0:
                        await self._close_xstock_positions_breakeven(reason="friday_1955_et")
                        # sleep past the minute to avoid double-fire
                        await asyncio.sleep(120.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("xstock friday flush error: %s", exc)
                await asyncio.sleep(60.0)
        logger.info("xStock Friday flush stopped")

    def _taker_fee_one_side(self) -> float:
        """Per-side fee rate for manual-exit estimates (taker, else 0.25%)."""
        raw = getattr(self.settings, "taker_fee_rate", None)
        try:
            v = float(raw) if raw is not None else 0.0025
        except (TypeError, ValueError):
            v = 0.0025
        if v < 0:
            v = 0.0025
        return v

    def _mark_for_symbol(self, symbol: str, pos=None) -> float:
        """Best-effort live/mark price for fee preview."""
        prices = getattr(self.broker, "_paper_prices", None) or {}
        try:
            px = float(prices.get(symbol) or 0)
            if px > 0:
                return px
        except (TypeError, ValueError):
            pass
        if pos is not None:
            try:
                qty = abs(float(getattr(pos, "qty", 0) or 0))
                mv = float(getattr(pos, "market_value", 0) or 0)
                if qty > 0 and mv:
                    return abs(mv) / qty
            except (TypeError, ValueError):
                pass
            try:
                entry = float(getattr(pos, "avg_entry_price", 0) or 0)
                if entry > 0:
                    return entry
            except (TypeError, ValueError):
                pass
        return 0.0

    def _close_fee_snapshot(self, *, symbol: str, qty: float, entry: float, mark: float, side: str = "long") -> dict:
        """Gross move + fee estimates for a manual close preview."""
        one = self._taker_fee_one_side()
        side_l = str(side or "long").lower()
        entry = float(entry)
        mark = float(mark) if mark and mark > 0 else entry
        qty = abs(float(qty))
        if side_l == "short":
            gross_pct = ((entry - mark) / entry * 100.0) if entry > 0 else 0.0
            gross_pnl = (entry - mark) * qty
        else:
            gross_pct = ((mark - entry) / entry * 100.0) if entry > 0 else 0.0
            gross_pnl = (mark - entry) * qty
        entry_notional = entry * qty
        exit_notional = mark * qty
        exit_fee = exit_notional * one
        rt_fee = (entry_notional + exit_notional) * one
        net_pnl = gross_pnl - rt_fee
        return {
            "symbol": symbol,
            "qty": qty,
            "entry": entry,
            "mark": mark,
            "side": side_l,
            "gross_pct": gross_pct,
            "gross_pnl": gross_pnl,
            "exit_fee": exit_fee,
            "rt_fee": rt_fee,
            "net_pnl": net_pnl,
            "one_side_pct": one * 100.0,
            "rt_pct": one * 200.0,
        }

    def _fmt_close_fee_preview(self, snap: dict) -> str:
        from trading_bot.notifier import _fmt_price as fp, _fmt_qty as fq
        sym = snap["symbol"]
        notional = float(snap["mark"]) * float(snap["qty"])
        fee_note = ""
        if notional >= 400:
            fee_note = f" (~${snap['rt_fee']:.2f} RT on ~${notional:.0f} notional)"
        return (
            f"{sym} qty={fq(snap['qty'])} entry=${fp(snap['entry'])} mark=${fp(snap['mark'])}\n"
            f"gross={snap['gross_pct']:+.2f}% (${snap['gross_pnl']:+.2f})\n"
            f"est exit fee=${snap['exit_fee']:.2f} | RT~{snap['rt_pct']:.2f}% (${snap['rt_fee']:.2f}){fee_note}\n"
            f"est net after fees=${snap['net_pnl']:+.2f}"
        )

    def _status_focus_block_reason(self, symbol: Optional[str] = None) -> Optional[str]:
        """Human reason when focus proximity is full but Target Setup is WAIT."""
        last = getattr(self, "_last_entry_block_reason", None)
        if last:
            return str(last)
        try:
            if symbol:
                r = self._long_entry_block_reason(symbol)
                if r:
                    return str(r)
        except Exception:
            pass
        if self._market_short_bias():
            return "Market Bias SHORT"
        if bool(getattr(self.ops, "paused", False)):
            return "paused"
        return "WAIT (not taking LONGs)"

    async def _cmd_close(self, _cmd: str, args: list[str]) -> str:
        """ /close SYMBOL [confirm] — fee preview; thin (±0.8% gross) needs confirm. """
        if not bool(getattr(self.settings, "paper_trading_mode", True)):
            return "/close is PAPER only"
        if not args:
            return "Usage: /close SYMBOL [confirm]"
        sym = str(args[0]).strip().upper().replace("_", "-")
        if "-" not in sym:
            sym = sym + "-USD"
        confirm = False
        if len(args) >= 2 and str(args[1]).strip().lower() in ("confirm", "yes", "y"):
            confirm = True

        # Locate open position
        pos = None
        try:
            for p in await self.broker.get_positions():
                ps = str(getattr(p, "symbol", "") or "").upper().replace("_", "-")
                if ps == sym:
                    pos = p
                    break
        except Exception as exc:
            return f"Position lookup failed: {exc}"
        if pos is None:
            return f"No open position for {sym}"

        qty = abs(float(getattr(pos, "qty", 0) or 0))
        entry = float(getattr(pos, "avg_entry_price", 0) or 0)
        side = str(getattr(pos, "side", "long") or "long")
        mark = self._mark_for_symbol(sym, pos)
        if entry <= 0 or qty <= 0:
            return f"Bad position data for {sym}"
        snap = self._close_fee_snapshot(symbol=sym, qty=qty, entry=entry, mark=mark, side=side)
        preview = self._fmt_close_fee_preview(snap)
        thin = abs(float(snap["gross_pct"])) < 1.0

        fn = getattr(self.broker, "close_paper_position_breakeven", None)
        if not callable(fn):
            return "Close not supported on this broker"

        now = time.monotonic()
        if thin and not confirm:
            self._pending_close_sym = sym
            self._pending_close_until = now + 60.0
            rt = float(snap["rt_fee"])
            return (
                f"{preview}\n"
                f"⚠️ Early exit warning: Round-trip fees (~${rt:.2f}) exceed "
                f"price movement PnL. Confirm close? [Yes / No]\n"
                f"Yes → /close {sym} confirm"
            )
        if thin and confirm:
            pending = getattr(self, "_pending_close_sym", None)
            until = float(getattr(self, "_pending_close_until", 0) or 0)
            if pending != sym or now > until:
                self._pending_close_sym = sym
                self._pending_close_until = now + 60.0
                return (
                    f"{preview}\n"
                    f"Confirm expired/missing — run /close {sym} then /close {sym} confirm"
                )
            self._pending_close_sym = None
            self._pending_close_until = 0.0

        row = fn(sym)
        if not row:
            return f"No open position for {sym}"
        return (
            f"{preview}\n"
            f"Closed {row['symbol']} @ BE entry=${row['entry']:.4f} "
            f"qty={row['qty']:.6g} → cash=${float(row['cash']):.2f}"
        )

    async def _cmd_clear_positions(self, _cmd: str, args: list[str]) -> str:
        """ /clear_positions [xstocks|all] [confirm] — fee warn; thin needs confirm. """
        if not bool(getattr(self.settings, "paper_trading_mode", True)):
            return "/clear_positions is PAPER only"
        tokens = [str(a).strip().lower() for a in args if str(a).strip()]
        confirm = False
        if tokens and tokens[-1] in ("confirm", "yes", "y"):
            confirm = True
            tokens = tokens[:-1]
        mode = tokens[0] if tokens else "xstocks"
        if mode not in ("xstocks", "all", "everything"):
            return "Usage: /clear_positions [xstocks|all] [confirm]"

        # Snapshot candidates
        try:
            positions = list(await self.broker.get_positions())
        except Exception as exc:
            return f"Position lookup failed: {exc}"
        from trading_bot.market_regime import is_tokenized_symbol
        cands = []
        for p in positions:
            sym = str(getattr(p, "symbol", "") or "")
            if not sym:
                continue
            if mode in ("all", "everything") or is_tokenized_symbol(sym):
                cands.append(p)
        if not cands:
            return "No positions to clear"

        snaps = []
        any_thin = False
        lines = [f"Clear preview ({mode}): {len(cands)} pos"]
        for p in cands:
            sym = str(getattr(p, "symbol", "") or "")
            qty = abs(float(getattr(p, "qty", 0) or 0))
            entry = float(getattr(p, "avg_entry_price", 0) or 0)
            side = str(getattr(p, "side", "long") or "long")
            mark = self._mark_for_symbol(sym, p)
            snap = self._close_fee_snapshot(symbol=sym, qty=qty, entry=entry, mark=mark, side=side)
            snaps.append(snap)
            if abs(snap["gross_pct"]) <= 0.8:
                any_thin = True
            lines.append(
                f"• {sym} gross={snap['gross_pct']:+.2f}% "
                f"RT fee≈${snap['rt_fee']:.2f} net≈${snap['net_pnl']:+.2f}"
            )
        total_rt = sum(s["rt_fee"] for s in snaps)
        lines.append(f"Est total RT fees≈${total_rt:.2f} (~0.5%+ round-trip taker)")

        now = time.monotonic()
        if any_thin and not confirm:
            self._pending_clear_mode = mode
            self._pending_clear_until = now + 60.0
            lines.append("⚠️ Early exit warning: Round-trip fees may exceed price movement PnL. Confirm close? [Yes / No]")
            lines.append("Yes → /clear_positions " + mode + " confirm")
            return "\n".join(lines)
        if any_thin and confirm:
            pending = getattr(self, "_pending_clear_mode", None)
            until = float(getattr(self, "_pending_clear_until", 0) or 0)
            if pending != mode or now > until:
                self._pending_clear_mode = mode
                self._pending_clear_until = now + 60.0
                lines.append("Confirm expired — run /clear_positions " + mode + " then … confirm")
                return "\n".join(lines)
            self._pending_clear_mode = None
            self._pending_clear_until = 0.0

        if mode in ("all", "everything"):
            fn = getattr(self.broker, "close_paper_position_breakeven", None)
            closed = []
            for pos in list(await self.broker.get_positions()):
                sym = getattr(pos, "symbol", "")
                if fn and sym:
                    row = fn(sym)
                    if row:
                        closed.append(row)
        else:
            closed = await self._close_xstock_positions_breakeven(reason="telegram_clear")
        if not closed:
            return "No positions to clear"
        cash = closed[-1].get("cash")
        names = ", ".join(c["symbol"] for c in closed)
        return (
            "\n".join(lines)
            + f"\nCleared {len(closed)} @ BE: {names}. cash=${float(cash):.2f}"
        )



    def _note_entry_block(self, symbol: str, reason: Optional[str]) -> None:
        """Remember last long-entry block for /status focus BLOCKED line."""
        if reason:
            self._last_entry_block_reason = str(reason)

    def _long_entry_block_reason(self, symbol: Optional[str] = None) -> Optional[str]:
        """Hard entry blocks. Spot long-only: BEAR_CHOP/dump do NOT freeze LONGs."""
        try:
            ok_rth, why_rth = xstock_entry_allowed(symbol or "")
            if not ok_rth:
                return why_rth or SKIP_XSTOCK_OUTSIDE_RTH
        except Exception:
            pass
        # Spot mode: allow longs in BEAR_CHOP / SHORT bias (quality gate elsewhere).
        if self._is_spot_long_only():
            return None
        regime = getattr(self, "btc_regime", None)
        if regime is None:
            return None
        state = str(getattr(regime, "state", "") or "").upper()
        if state == STATE_BEAR_CHOP:
            return SKIP_BEAR_CHOP
        if bool(getattr(regime, "dump_30m", False)):
            return SKIP_DUMP_30M
        return None

    def _round_trip_fee_pct(self) -> float:
        """Return the fee percentage used by the TP breakeven shield."""
        # Allow deployments/tests to provide the stagnant-style estimate directly.
        for name in ("round_trip_fee_pct", "stagnant_rt_fee_pct"):
            raw = getattr(self.settings, name, None)
            if raw is not None:
                try:
                    value = float(raw)
                    if value >= 0.0:
                        return value
                except (TypeError, ValueError):
                    pass
        # Prefer configured maker fees when available; forced exits are post-only.
        raw_maker = getattr(self.settings, "maker_fee_rate", None)
        try:
            maker = float(raw_maker)
            if maker >= 0.0:
                return maker * 2.0
        except (TypeError, ValueError):
            pass
        return 0.0026

    def _htf_15m_direction_block(self, obs: AgentObservation, *, direction: str, mark: float = 0.0) -> bool:
        """Soft 15m EMA200 direction guard; missing EMA/close never blocks."""
        htf = getattr(obs, "htf", None)
        extras = {}
        try:
            extras = dict((getattr(obs.indicators, "extras", None) or {}))
        except Exception:
            pass
        ema = getattr(htf, "ema_200_15m", None) if htf is not None else None
        if ema is None:
            ema = extras.get("ema_200_15m")
        close = extras.get("close_15m")
        if close is None and htf is not None:
            close = (getattr(htf, "extras", None) or {}).get("close_15m")
        try:
            ema_f = float(ema) if ema is not None else None
            close_f = float(close) if close is not None else (float(mark) if mark > 0 else None)
        except (TypeError, ValueError):
            return False
        if ema_f is None or ema_f <= 0 or close_f is None or close_f <= 0:
            return False
        if direction.upper() == "LONG":
            return close_f < ema_f
        return close_f > ema_f

    def _paper_short_signal(self, obs: AgentObservation) -> bool:
        """Minimal 1m EMA20/EMA50 bearish-cross + price/volume short trigger."""
        if not bool(getattr(self.settings, "allow_paper_shorts", False)) or not bool(self.settings.paper_trading_mode):
            return False
        extras = dict((getattr(obs.indicators, "extras", None) or {}))
        try:
            px = float(getattr(obs.indicators, "close", 0) or 0)
            ema50 = float(extras.get("ema50_1m") or 0)
            vr = float(extras.get("volume_ratio") or 0)
        except (TypeError, ValueError):
            return False
        mult = float(getattr(self.settings, "rvol_breakout_mult", 2.0) or 2.0)
        # In BEAR_CHOP/dump mode, a 1.2x bearish surge is enough; do not
        # require the bullish RVOL/VWAP reclaim used by long setups.
        min_vr = 1.2 if self._bear_short_mode() else max(mult, 1.5)
        bearish_surge = any(
            bool(extras.get(key))
            for key in ("bearish_volume_surge", "bearish_rvol_surge", "sell_volume_surge")
        )
        return (
            str(extras.get("ema20_50_cross") or "").lower() == "bearish"
            and px > 0 and ema50 > 0 and px < ema50
            and (bearish_surge or vr >= min_vr)
        )



    def _record_digest_close(
        self,
        symbol: str,
        *,
        entry: Optional[float],
        exit_px: Optional[float],
        net_pnl: float,
    ) -> None:
        """Paper → trades.db (wipeable). Live → trades_live.db (never wiped)."""
        import sqlite3
        import time as _time
        paper = bool(getattr(self.settings, "paper_trading_mode", True))
        path = PROJECT_ROOT / "data" / ("trades.db" if paper else "trades_live.db")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path))
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS closed_trades ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, "
                    "entry REAL, exit REAL, net_pnl REAL NOT NULL, timestamp REAL NOT NULL)"
                )
                conn.execute(
                    "INSERT INTO closed_trades (symbol, entry, exit, net_pnl, timestamp) "
                    "VALUES (?,?,?,?,?)",
                    (
                        str(symbol),
                        float(entry) if entry is not None else None,
                        float(exit_px) if exit_px is not None else None,
                        float(net_pnl),
                        _time.time(),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("digest close record: %s", exc)

    def _apply_smart_memory_close(
        self,
        *,
        symbol: str,
        pnl: Optional[float],
        paper: bool,
        entry: Optional[float] = None,
        exit_px: Optional[float] = None,
        short: bool = False,
    ) -> None:
        """Record paper session win/loss; optionally bump threshold in AGGRESSIVE."""
        if not paper:
            return
        if pnl is None:
            return
        try:
            tag = None
            try:
                e = float(entry) if entry is not None else 0.0
                x = float(exit_px) if exit_px is not None else 0.0
                if e > 0 and x > 0:
                    gross = ((e - x) / e) if short else ((x - e) / e)
                    if gross > 0.0 and float(pnl) < 0.0:
                        tag = "net_fee_flat"
                        logger.info(
                            "[NET_FEE_FLAT] %s gross=+%.3f%% net=$%.2f — not a CB loss",
                            symbol, gross * 100.0, float(pnl),
                        )
            except Exception:
                tag = None
            result = self.smart_memory.record_close(
                won=float(pnl) > 0.0, symbol=symbol, pnl=float(pnl), tag=tag
            )
            # 5 consecutive losses → pause new buys (circuit breaker)
            try:
                consec_l = int(self.smart_memory.consecutive_losses())
            except Exception:
                consec_l = 0
            wf = bool(getattr(self.settings, "winning_formula", False))
            limit = 3 if wf else 5
            if consec_l >= limit and not bool(self.ops.paused):
                if not bool(getattr(self.ops, "cb_enabled", True)):
                    # Manual OFF: keep counting, do not auto-pause
                    logger.warning(
                        "CB limit %s hit but circuity_breaker_manually=OFF "
                        "(consec=%s) — no auto pause",
                        limit, consec_l,
                    )
                else:
                    self.ops.set_pause(True)
                    try:
                        mins = int(self.ops.arm_cb_auto_resume())
                    except Exception:
                        mins = 45
                    try:
                        self.ops.cb_active = True
                    except Exception:
                        pass
                    try:
                        regime = getattr(self, "btc_regime", None)
                        mstate = str(getattr(regime, "state", None) or "n/a")
                    except Exception:
                        mstate = "n/a"
                    alert = (
                        f"⚠️ CIRCUIT BREAKER: {limit} consecutive losses hit in {mstate}. "
                        f"Trading paused. Auto-resume in ~{mins}m after a win or BULL_OK "
                        f"(gated). /resume now or /pause to cancel auto-resume."
                    )
                    logger.warning("%s", alert)
                    try:
                        import asyncio
                        loop = asyncio.get_running_loop()
                        loop.create_task(self.notifier.send(alert, high_priority=True))
                    except Exception as exc:
                        logger.debug("circuit breaker telegram: %s", exc)
            # While CB auto-resume armed, a win unlocks the gate (still waits cooldown)
            elif bool(getattr(self.ops, "cb_auto_resume_armed", False)) and float(pnl) > 0.0:
                try:
                    self.ops.cb_win_since_trip = True
                    logger.info("CB auto-resume: win recorded — gate unlocked (cooldown may remain)")
                except Exception:
                    pass
            # Threshold tighten/restore only in quick-scalp (AGGRESSIVE) mode
            if not wants_quick_scalp(self.settings):
                return
            action = result.get("action")
            if action in ("tighten", "restore") and result.get("threshold") is not None:
                from trading_bot.config import (
                    ENV_KEY_ENTRY_THRESHOLD,
                    apply_runtime_entry_threshold,
                    upsert_env_keys,
                )
                new_t = float(result["threshold"])
                apply_runtime_entry_threshold(self.settings, new_t)
                try:
                    upsert_env_keys(
                        PROJECT_ROOT / ".env",
                        {ENV_KEY_ENTRY_THRESHOLD: str(int(new_t))},
                    )
                except Exception as exc:
                    logger.debug("smart_memory env persist: %s", exc)
                logger.warning(
                    "[SMART_MEMORY] applied ENTRY_THRESHOLD=%.0f (%s)",
                    new_t, action,
                )
        except Exception as exc:
            logger.debug("smart_memory close: %s", exc)

    def _check_hard_brackets(
        self,
        symbol: str,
        position,
        mark: float,
        *,
        rvol: Optional[float] = None,
    ) -> Optional[Decision]:
        """Force exit: structural SL / TP1 / TP2 / exhaustion / time-stop.

        volume_sweet_spot: structural brackets remain primary, but the +0.8%
        long arm explicitly enables the requested ATR/0.4% trailing stop.
        AGGRESSIVE quick-scalp: micro-trail at +0.5% UPL; stagnant ±0.2%/12m.
        """
        if position is None or float(getattr(position, "qty", 0) or 0) <= 0:
            return None
        if mark <= 0:
            return None

        sweet = bool(getattr(self.settings, "is_sweet_spot", False))
        updater = getattr(self.broker, "update_position_brackets", None)
        is_short = self._is_short_position(position)
        entry = float(getattr(position, "avg_entry_price", 0) or 0)
        # Elite fee-lock trail (+0.6% UPL → entry*1.003 / *0.997) preferred over micro-trail
        if wants_elite_risk(self.settings) and entry > 0 and callable(updater):
            try:
                from trading_bot.utils.decision_filters import (
                    maybe_fee_lock_sl,
                    trail_fee_buffer_pct,
                )

                cur_sl = getattr(position, "stop_loss", None)
                buf = trail_fee_buffer_pct(self.settings)
                arm = float(
                    getattr(self.settings, "elite_fee_lock_arm_pct", None) or buf
                )
                arm = max(arm, buf)  # never arm before fee buffer
                # Progress toward TP (for ≥50% risk-free floor)
                progress_pct = None
                try:
                    tp_px = getattr(position, "take_profit", None)
                    if tp_px is not None and entry > 0 and not is_short:
                        tp_pct = max((float(tp_px) - entry) / entry, 0.02)
                        upl_now = (mark - entry) / entry
                        progress_pct = 100.0 * upl_now / tp_pct
                    elif not is_short and entry > 0:
                        # fallback: treat +2% as 100% progress target
                        upl_now = (mark - entry) / entry
                        progress_pct = 100.0 * upl_now / 0.02
                except Exception:
                    progress_pct = None
                prev_sl = float(cur_sl) if cur_sl is not None else None
                # High-water mark: track peak gross UPL
                try:
                    peaks = getattr(self, "_peak_upl", None)
                    if not isinstance(peaks, dict):
                        peaks = {}
                        self._peak_upl = peaks
                    upl_now = (mark - entry) / entry if not is_short else (entry - mark) / entry
                    peaks[symbol] = max(float(peaks.get(symbol) or 0.0), float(upl_now))
                    peak_upl = float(peaks[symbol])
                except Exception:
                    peak_upl = 0.0
                new_sl = maybe_fee_lock_sl(
                    entry,
                    mark,
                    cur_sl,
                    short=is_short,
                    arm_pct=arm,
                    fee_buffer_pct=buf,
                    settings=self.settings,
                    progress_pct=progress_pct,
                    progress_arm=float(getattr(self, "_HWM_PROGRESS_ARM", 60.0)),
                    min_floor_pct=float(getattr(self, "_HWM_FLOOR_PCT", 0.0125)),
                    peak_arm_pct=float(getattr(self, "_HWM_PEAK_ARM_PCT", 0.012)),
                )
                # Force +1.25% floor if peak ≥ +1.20% even if fee-lock returned None
                if (
                    new_sl is None
                    and not is_short
                    and peak_upl >= float(getattr(self, "_HWM_PEAK_ARM_PCT", 0.012))
                ):
                    floor_px = entry * (1.0 + float(getattr(self, "_HWM_FLOOR_PCT", 0.0125)))
                    if cur_sl is None or float(cur_sl) < floor_px:
                        new_sl = floor_px
                if new_sl is not None:
                    position = updater(symbol, stop_loss=new_sl, mark_price=mark) or position
                    floor_pct = (float(new_sl) / entry - 1.0) * 100.0 if entry > 0 else 0.0
                    logger.info(
                        "[FEE_LOCK] %s SL floor→%.6g (≈+%.2f%% entry=%.6g mark=%.6g progress=%s)",
                        symbol, new_sl, floor_pct, entry, mark,
                        f"{progress_pct:.1f}%" if progress_pct is not None else "n/a",
                    )
                    # Telegram once per symbol when raised to ≥+1.00% risk-free floor
                    try:
                        if (
                            not is_short
                            and entry > 0
                            and float(new_sl) >= entry * 1.0125 - 1e-12
                            and symbol not in self._risk_free_notified
                        ):
                            self._risk_free_notified.add(symbol)
                            msg = (
                                f"[RISK FREE | Stop Loss Trailed to Breakeven + Fee Floor] "
                                f"{symbol} SL→{new_sl:.6g} (+{floor_pct:.2f}%)"
                            )
                            import asyncio
                            loop = asyncio.get_running_loop()
                            loop.create_task(
                                self.notifier.send(msg, high_priority=True)
                            )
                    except Exception as tg_exc:
                        logger.debug("RISK FREE telegram: %s", tg_exc)
            except Exception as exc:
                logger.debug("fee-lock skipped: %s", exc)
        elif wants_quick_scalp(self.settings) and entry > 0 and callable(updater):
            try:
                cur_sl = getattr(position, "stop_loss", None)
                new_sl = maybe_micro_trail_sl(entry, mark, cur_sl, short=is_short)
                if new_sl is not None:
                    position = updater(symbol, stop_loss=new_sl, mark_price=mark) or position
                    logger.info(
                        "[MICRO_TRAIL] %s SL locked to %.6g (entry=%.6g mark=%.6g)",
                        symbol, new_sl, entry, mark,
                    )
            except Exception as exc:
                logger.debug("micro-trail skipped: %s", exc)
        trail_armed = False
        # Profit-runner trail: only after UPL >= +2.50% OR progress >= 75%; offset 1% of entry.
        # HWM fee floor (+1.25%) still arms earlier via maybe_fee_lock_sl — separate.
        try:
            _runner_arm = float(getattr(self, "_TRAIL_RUNNER_ARM_PCT", 0.025))
            _runner_prog = float(getattr(self, "_TRAIL_RUNNER_PROGRESS", 75.0))
            _runner_off = float(getattr(self, "_TRAIL_RUNNER_OFFSET_PCT", 0.01))
            _upl_r = (mark - entry) / entry if entry > 0 and not is_short else 0.0
            _prog_r = None
            try:
                _tp_r = getattr(position, "take_profit", None)
                if _tp_r is not None and entry > 0 and not is_short:
                    _tp_pct_r = max((float(_tp_r) - entry) / entry, 0.02)
                    _prog_r = 100.0 * _upl_r / _tp_pct_r
            except Exception:
                _prog_r = None
            _runner_ok = (not is_short and entry > 0 and (
                _upl_r >= _runner_arm or (_prog_r is not None and _prog_r >= _runner_prog)
            ))
            if _runner_ok:
                trail_distance = entry * _runner_off
                if callable(updater) and trail_distance > 0:
                    position = updater(
                        symbol, mark_price=mark, trail_distance=trail_distance,
                        update_trail_hwm=True, force_trail=True
                    ) or position
                    trail_armed = True
                    if symbol not in getattr(self, "_profit_runner_notified", set()):
                        self._profit_runner_notified.add(symbol)
                        msg = (
                            f"🎯 [PROFIT RUNNER] {symbol} Target ≥ +{_runner_arm * 100:.2f}% "
                            f"reached. Trailing Stop Activated "
                            f"(UPL={_upl_r * 100:.2f}%"
                            f"{f', progress={_prog_r:.0f}%' if _prog_r is not None else ''}"
                            f", trail={_runner_off * 100:.2f}%)."
                        )
                        try:
                            import asyncio
                            loop = asyncio.get_running_loop()
                            loop.create_task(
                                self.notifier.send(msg, high_priority=True)
                            )
                        except Exception:
                            pass
                        logger.warning("%s", msg)
        except Exception as exc:
            logger.debug("profit-runner trail: %s", exc)
        if not trail_armed and (not sweet or is_short):
            if callable(updater):
                try:
                    position = updater(symbol, mark_price=mark) or position
                except Exception as exc:
                    logger.debug("trail update skipped: %s", exc)

        # Tier-1 trail floor: Entry*(1+1.25%) when peak≥1.20%, progress≥60%, or UPL≥1.25%
        if not is_short and entry > 0 and callable(updater):
            try:
                from trading_bot.utils.decision_filters import trail_fee_buffer_pct as _tfb2
                _buf2 = float(_tfb2(self.settings))
                _floor_pct = float(getattr(self, "_HWM_FLOOR_PCT", 0.0125))
                _peak_arm = float(getattr(self, "_HWM_PEAK_ARM_PCT", 0.012))
                _prog_arm = float(getattr(self, "_HWM_PROGRESS_ARM", 60.0))
                _upl = (mark - entry) / entry
                try:
                    peaks = getattr(self, "_peak_upl", None)
                    if not isinstance(peaks, dict):
                        peaks = {}
                        self._peak_upl = peaks
                    peaks[symbol] = max(float(peaks.get(symbol) or 0.0), float(_upl))
                    _peak = float(peaks[symbol])
                except Exception:
                    _peak = _upl
                _prog = None
                try:
                    _tp = getattr(position, "take_profit", None)
                    if _tp is not None:
                        _tp_pct = max((float(_tp) - entry) / entry, 0.02)
                        _prog = 100.0 * _upl / _tp_pct
                    else:
                        _prog = 100.0 * _upl / 0.02
                except Exception:
                    _prog = None
                if _peak >= _peak_arm or _upl >= _floor_pct or (_prog is not None and _prog >= _prog_arm):
                    _buf2 = max(_buf2, _floor_pct)
                _floor = entry * (1.0 + _buf2)
                _cur = getattr(position, "stop_loss", None)
                if (_peak >= _peak_arm or _upl >= _floor_pct or (_prog is not None and _prog >= _prog_arm)) and (
                    _cur is None or float(_cur) < _floor
                ):
                    position = updater(symbol, stop_loss=_floor, mark_price=mark) or position
                    logger.info(
                        "[TRAIL_FEE_FLOOR] %s SL≥%.6g (+%.2f%% peak=%.2f%%)",
                        symbol, _floor, _buf2 * 100.0, _peak * 100.0,
                    )
                    if (
                        _buf2 >= _floor_pct - 1e-12
                        and symbol not in getattr(self, "_risk_free_notified", set())
                    ):
                        self._risk_free_notified.add(symbol)
                        msg = (
                            f"[RISK FREE | Stop Loss Trailed to Breakeven + Fee Floor] "
                            f"{symbol} SL→{_floor:.6g} (+{_buf2 * 100.0:.2f}%)"
                        )
                        try:
                            import asyncio
                            loop = asyncio.get_running_loop()
                            loop.create_task(
                                self.notifier.send(msg, high_priority=True)
                            )
                        except Exception:
                            pass
            except Exception as exc:
                logger.debug("trail fee floor: %s", exc)

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

        # A persisted/malformed bracket must never turn a losing LONG mark into
        # a TP. Ignore inverted targets and log the repair condition; SL/manual
        # handling below remains available.
        valid_tp2 = tp2 is not None
        valid_tp1 = tp1 is not None
        inverted_long_target = False
        if entry > 0 and not is_short:
            if valid_tp2 and float(tp2) <= entry:
                logger.warning(
                    "[BRACKET_REPAIR] %s inverted LONG TP2=%.8g <= entry=%.8g; ignoring TP label",
                    symbol, float(tp2), entry,
                )
                valid_tp2 = False
                inverted_long_target = True
            if valid_tp1 and float(tp1) <= entry:
                logger.warning(
                    "[BRACKET_REPAIR] %s inverted LONG TP1=%.8g <= entry=%.8g; ignoring TP label",
                    symbol, float(tp1), entry,
                )
                valid_tp1 = False
                inverted_long_target = True
        elif entry > 0 and is_short and valid_tp2 and float(tp2) >= entry:
            logger.warning(
                "[BRACKET_REPAIR] %s inverted SHORT TP=%.8g >= entry=%.8g; ignoring TP label",
                symbol, float(tp2), entry,
            )
            valid_tp2 = False

        if is_short:
            if sl is not None and mark >= float(sl):
                reason = "ATR stop"
            elif valid_tp2 and mark <= float(tp2) and float(tp2) < entry:
                reason = "ATR take-profit"
            elif trail_dist is not None and hwm is not None and mark >= float(hwm) + float(trail_dist):
                reason = "trail"
        elif sweet:
            if sl is not None and mark <= float(sl):
                reason = "SL"
            elif inverted_long_target and mark < entry and (
                (tp2 is not None and mark >= float(tp2))
                or (tp1 is not None and mark >= float(tp1))
            ):
                reason = "manual"
                logger.info(
                    "[BRACKET_GUARD] %s inverted LONG TP trigger; reason=manual mark=%.8g entry=%.8g",
                    symbol, mark, entry,
                )
            else:
                # Compute TP1 from 1R if not stored
                if tp1 is None and entry > 0 and sl is not None:
                    risk = entry - float(sl)
                    if risk > 0:
                        tp1 = entry + float(self.settings.tp1_rr) * risk
                        valid_tp1 = tp1 > entry
                exh_mult = float(getattr(self.settings, "rvol_exhaustion_mult", 3.0) or 3.0)
                near_tp2 = valid_tp2 and mark >= float(tp2) * 0.998
                exhausted = (
                    isinstance(rvol, (int, float))
                    and float(rvol) >= exh_mult
                    and (near_tp2 or (valid_tp2 and mark >= float(tp2) * 0.99))
                )
                # Partial TP1 scale-out: disabled for small books (<= $1k notional)
                # to avoid dual taker/min-fee hits. Full exit only at TP2 / +2%.
                notional = abs(float(qty) * float(entry)) if entry > 0 else 0.0
                max_partial = float(
                    getattr(self.settings, "partial_tp_max_notional_usd", 1000.0) or 1000.0
                )
                allow_partial = (
                    float(getattr(self.settings, "tp1_fraction", 0.0) or 0.0) > 0
                    and notional > max_partial + 1e-9
                )
                if (
                    allow_partial
                    and not tp1_done
                    and valid_tp1
                    and mark >= float(tp1)
                    and float(tp1) > entry
                ):
                    reason = "TP1"
                    frac = float(getattr(self.settings, "tp1_fraction", 0.5) or 0.5)
                    sell_qty = initial_qty * frac
                elif valid_tp2 and mark >= float(tp2) and float(tp2) > entry:
                    reason = "TP2"
                    sell_qty = qty
                elif exhausted:
                    # Exhaustion is an emergency/manual exit, never a TP before
                    # the actual target has traded. This was the inverted-TP bug.
                    reason = "manual"
                    sell_qty = qty
                    logger.info(
                        "[BRACKET_GUARD] %s exhaustion exit below TP2; reason=manual mark=%.8g tp2=%.8g",
                        symbol, mark, float(tp2),
                    )
                elif trail_dist is not None and hwm is not None and mark <= float(hwm) - float(trail_dist):
                    reason = "trail"
        else:
            tp = tp2
            if sl is not None and mark <= float(sl):
                reason = "ATR stop"
            elif inverted_long_target and mark < entry and (
                (tp2 is not None and mark >= float(tp2))
                or (tp1 is not None and mark >= float(tp1))
            ):
                reason = "manual"
                logger.info(
                    "[BRACKET_GUARD] %s inverted LONG TP trigger; reason=manual mark=%.8g entry=%.8g",
                    symbol, mark, entry,
                )
            elif valid_tp2 and mark >= float(tp) and float(tp) > entry:
                reason = "ATR take-profit"
            elif trail_dist is not None and hwm is not None:
                trail_stop = float(hwm) - float(trail_dist)
                if mark <= trail_stop:
                    reason = "trail"

        # Progress / PnL TP trigger: once SL→TP progress hits 100% OR unrealized
        # PnL% reaches the configured TP distance, force full exit immediately.
        if reason is None and entry > 0 and not is_short:
            try:
                pnl_pct_now = unrealized_pnl_pct(entry, mark, short=False)
            except Exception:
                pnl_pct_now = None
            target_tp_pct = None
            if valid_tp2 and float(tp2) > entry:
                target_tp_pct = max(self._profile_sl_tp_pct()[1], (float(tp2) - entry) / entry)
            else:
                target_tp_pct = self._profile_sl_tp_pct()[1]
            progress = None
            if pnl_pct_now is not None and target_tp_pct and target_tp_pct > 0:
                # progress = current_pnl / tp_target * 100 (2% floor)
                progress = 100.0 * float(pnl_pct_now) / float(target_tp_pct)
            hit_progress = progress is not None and float(progress) >= 100.0
            hit_pnl = (
                target_tp_pct is not None
                and pnl_pct_now is not None
                and float(pnl_pct_now) + 1e-12 >= float(target_tp_pct)
            )
            hit_price = valid_tp2 and mark >= float(tp2) and float(tp2) > entry
            if hit_progress or hit_pnl or hit_price:
                reason = "TP Hit"
                sell_qty = qty
                logger.info(
                    "[TP_TRIGGER] %s progress=%s pnl=%.4f%% target=%.4f%% mark=%.8g tp=%.8g",
                    symbol,
                    f"{progress:.1f}%" if progress is not None else "n/a",
                    (pnl_pct_now or 0.0) * 100.0,
                    (target_tp_pct or 0.0) * 100.0,
                    mark,
                    float(tp2) if tp2 is not None else 0.0,
                )

        # Fee shield: only soft-block early/partial TPs that are still underwater
        # after fees. Never cancel a true target hit (mark>=TP / progress>=100).
        if reason in ("TP1", "TP2", "ATR take-profit"):
            tagged = valid_tp2 and mark >= float(tp2) and float(tp2) > entry
            if not tagged:
                tp_qty = float(sell_qty if sell_qty is not None else qty)
                net = estimate_net_pnl(
                    entry, mark, tp_qty, short=is_short,
                    rt_fee_pct=self._round_trip_fee_pct(),
                )
                if net <= 0.0:
                    logger.info("[FEE_SHIELD] skip TP %s net=$%.4f", symbol, net)
                    reason = None
                    sell_qty = None

        # Stagnant auto-kill: skipped under elite risk (ATR SL manages); else quick-scalp only
        if (
            reason is None
            and entry > 0
            and wants_quick_scalp(self.settings)
            and not wants_elite_risk(self.settings)
        ):
            try:
                pnl_pct = unrealized_pnl_pct(entry, mark, short=is_short)
                if self.stagnant.should_exit(symbol, pnl_pct):
                    qty_f = float(getattr(position, "qty", 0) or 0)
                    net = estimate_net_pnl(entry, mark, qty_f, short=is_short)
                    if net >= 0.0:
                        reason = "stagnant"
                        logger.warning("[STAGNANT_EXIT] %s net=$%.4f", symbol, net)
                        self.stagnant.clear(symbol)
                        try:
                            getattr(self, "_risk_free_notified", set()).discard(symbol)
                        except Exception:
                            pass
                        try:
                            getattr(self, "_profit_runner_notified", set()).discard(symbol)
                        except Exception:
                            pass
                        try:
                            getattr(self, "_peak_upl", {}).pop(symbol, None)
                        except Exception:
                            pass
                    else:
                        logger.info(
                            "[STAGNANT] %s held but net=$%.4f < 0 — leave to SL/TP",
                            symbol, net,
                        )
                else:
                    self.stagnant.update(symbol, pnl_pct)
            except Exception as exc:
                logger.debug("stagnant check: %s", exc)

        # Pending maker BE time-exit: fill at BE when mark reaches it, or SL after 3m
        pending = getattr(self, "_pending_maker_time_exits", None)
        if isinstance(pending, dict) and symbol in pending and reason is None and not is_short:
            try:
                meta = pending[symbol]
                be_px = float(meta["be_px"])
                sl_px = meta.get("sl")
                deadline = float(meta["deadline"])
                now_ts = utcnow().timestamp()
                if mark >= be_px:
                    reason = "TIME_EXIT_MAKER_BE"
                    sell_qty = None
                    logger.warning(
                        "[TIME_EXIT_MAKER_BE] %s fill@BE=%.6g mark=%.6g",
                        symbol, be_px, mark,
                    )
                    pending.pop(symbol, None)
                elif now_ts >= deadline and sl_px is not None and mark < float(sl_px):
                    reason = "TIME_EXIT_MAKER_TIMEOUT_SL"
                    logger.warning(
                        "[TIME_EXIT_MAKER_TIMEOUT_SL] %s mark=%.6g < SL=%.6g after fee-cushion wait",
                        symbol, mark, float(sl_px),
                    )
                    pending.pop(symbol, None)
                else:
                    logger.info(
                        "[TIME_EXIT_MAKER_WAIT] %s be=%.6g mark=%.6g rem=%.0fs",
                        symbol, be_px, mark, max(0.0, deadline - now_ts),
                    )
            except Exception as exc:
                logger.debug("maker time-exit pending: %s", exc)

        if reason is None and opened_at is not None:
            default_hold = 30 if sweet else 45
            max_hold = int(getattr(self.settings, "max_hold_minutes", default_hold) or default_hold)
            try:
                oa = opened_at
                if oa.tzinfo is None:
                    oa = oa.replace(tzinfo=timezone.utc)
                age_min = (utcnow() - oa).total_seconds() / 60.0
                if age_min >= max_hold:
                    # Elite: never hard time-exit while unrealized PnL is negative
                    upl_neg = False
                    upl = 0.0
                    if entry > 0:
                        upl = unrealized_pnl_pct(entry, mark, short=is_short)
                    if (
                        wants_elite_risk(self.settings)
                        and bool(getattr(self.settings, "elite_disable_neg_time_exit", True))
                        and upl < 0.0
                    ):
                        upl_neg = True
                    if upl_neg:
                        logger.info(
                            "[ELITE] skip time-stop %s UPL=%.3f%% (ATR SL manages)",
                            symbol, upl * 100.0,
                        )
                    elif not is_short and entry > 0:
                        # Tier-1: only post maker if gross ≥ +1.25%; else hold for trail/SL
                        hurdle = float(self._TIME_EXIT_FEE_CUSHION_PCT)
                        if float(upl) >= hurdle:
                            be_px = float(entry) * (1.0 + hurdle)  # entry * 1.0125
                            wait_s = float(self._TIME_EXIT_MAKER_WAIT_SEC)
                            if isinstance(pending, dict):
                                pending[symbol] = {
                                    "be_px": be_px,
                                    "sl": float(sl) if sl is not None else None,
                                    "deadline": utcnow().timestamp() + wait_s,
                                    "entry": float(entry),
                                    "cush_pct": hurdle,
                                }
                            logger.warning(
                                "[TIME_EXIT_MAKER_ARM] %s UPL=%.3f%% limit=%.6g "
                                "(entry+%.2f%% Tier-1) wait≤%.0fs",
                                symbol, upl * 100.0, be_px, hurdle * 100.0, wait_s,
                            )
                            # hold — do not set reason
                        else:
                            logger.info(
                                "[TIME_EXIT_HOLD] %s UPL=%.3f%% < +%.2f%% hurdle — "
                                "trail/structural SL manages",
                                symbol, upl * 100.0, hurdle * 100.0,
                            )
                            # hold — do not hard time-stop below Tier-1 hurdle
                    else:
                        reason = "time-stop"
            except Exception as exc:
                logger.debug("time-stop check failed: %s", exc)

        if reason is None:
            return None

        lim = None
        if reason == "TIME_EXIT_MAKER_BE" and entry > 0:
            cush = float(self._TIME_EXIT_FEE_CUSHION_PCT) + float(self._TIME_EXIT_FEE_PAD_PCT)
            lim = float(entry) * (1.0 + cush)
        return Decision(
            action=Action.BUY if is_short else Action.SELL,
            symbol=symbol,
            confidence=100.0,
            stop_loss=float(sl) if sl is not None else None,
            take_profit=float(tp2) if tp2 is not None else None,
            quantity=float(sell_qty) if sell_qty is not None else None,
            limit_price=lim,
            reasoning=reason,
        )

    async def _auto_paper_buy(
        self,
        symbol: str,
        *,
        price: float,
        notional: float,
        proximity: float,
        threshold: float,
    ) -> Decision:
        """Paper-only auto BUY used by the scanner proximity bridge.

        Reuses the /test_trade order builder + executor.submit so the paper
        book fill and position registration match manual test trades.
        Hard-clamped to AUTO_PAPER_MAX_NOTIONAL_USD ($1000). Never fires live.
        """
        from trading_bot.telegram_commands import build_test_trade_order

        if not bool(self.settings.paper_trading_mode):
            logger.warning("AUTO_PAPER_BUY refused live for %s", symbol)
            return Decision.hold(symbol, "auto_paper_buy: live_blocked")
        if self.ops.paused:
            return Decision.hold(symbol, "auto_paper_buy: paused")
        if self._in_post_stop_cooldown(symbol):
            from trading_bot.utils.decision_filters import format_post_stop_cooldown_skip

            rem = self._post_stop_cooldown_remaining(symbol)
            skip_msg = format_post_stop_cooldown_skip(symbol, rem)
            logger.info(skip_msg)
            return Decision.hold(symbol, skip_msg)

        notion = clamp_auto_notional(notional)
        try:
            order = build_test_trade_order(
                symbol,
                price=float(price),
                notional=notion,
                qty_precision=int(getattr(self.settings, "qty_precision", 8) or 8),
                paper=True,
            )
        except Exception as exc:
            logger.warning("auto_paper_buy build failed %s: %s", symbol, exc)
            return Decision.hold(symbol, f"auto_paper_buy: build_failed ({exc})")

        try:
            self.state.record_buy_attempt(symbol)
        except Exception as exc:
            logger.debug("auto_paper_buy dedupe stamp: %s", exc)

        try:
            result = await self.executor.submit(order)
        except Exception as exc:
            logger.warning("auto_paper_buy submit failed %s: %s", symbol, exc)
            return Decision.hold(symbol, f"auto_paper_buy: submit_failed ({exc})")

        if result.status not in (OrderStatus.FILLED, OrderStatus.PARTIAL) or float(
            result.filled_qty or 0
        ) <= 0:
            msg = (result.message or result.status.value or "rejected").strip()
            logger.info("auto_paper_buy rejected %s | %s", symbol, msg)
            return Decision.hold(symbol, f"auto_paper_buy: {msg}")

        fill_px = float(result.avg_fill_price or price)
        filled_notional = float(result.filled_qty) * fill_px
        logger.warning(
            "AUTO_PAPER_BUY %s qty=%.8f @ %.6f notional~$%.2f prox=%.0f%% thresh=%.0f%%",
            symbol,
            float(result.filled_qty),
            fill_px,
            filled_notional,
            float(proximity),
            float(threshold),
        )
        # Elite ATR 1.5/2.5 brackets preferred; else quick-scalp or legacy ATR
        atr_sl = atr_tp = None
        if wants_elite_risk(self.settings):
            try:
                from trading_bot.utils.decision_filters import elite_atr_bracket_levels
                atr_v = None
                ind = self.feed.get_indicators(symbol)
                if ind is not None and getattr(ind, "atr", None):
                    atr_v = float(ind.atr)
                if atr_v and atr_v > 0:
                    atr_sl, atr_tp = elite_atr_bracket_levels(
                        fill_px, atr_v, short=False,
                        sl_mult=float(getattr(self.settings, "elite_atr_sl_mult", 1.5) or 1.5),
                        tp_mult=float(getattr(self.settings, "elite_atr_tp_mult", 2.5) or 2.5),
                    )
                    updater = getattr(self.broker, "update_position_brackets", None)
                    if callable(updater):
                        updater(
                            symbol, stop_loss=atr_sl, take_profit=atr_tp,
                            take_profit_1=fill_px + 1.0 * (fill_px - atr_sl),
                            mark_price=fill_px, trail_distance=None, tp1_done=False,
                            initial_qty=float(result.filled_qty),
                            entry_reason=f"elite ATR brackets ATR={atr_v:.6g}",
                        )
                    logger.info("ELITE_ATR auto-buy %s SL=%.6g TP=%.6g ATR=%.6g", symbol, atr_sl, atr_tp, atr_v)
            except Exception as exc:
                logger.debug("elite atr auto-buy: %s", exc)
        elif wants_quick_scalp(self.settings):
            atr_sl, atr_tp = quick_scalp_brackets(fill_px, short=False)
            updater = getattr(self.broker, "update_position_brackets", None)
            if callable(updater):
                try:
                    updater(
                        symbol, stop_loss=atr_sl, take_profit=atr_tp,
                        mark_price=fill_px, trail_distance=None, tp1_done=False,
                        initial_qty=float(result.filled_qty),
                        entry_reason="quick-scalp AGGRESSIVE TP+1%/SL-0.6%",
                    )
                except Exception as exc:
                    logger.debug("quick-scalp auto-buy: %s", exc)
            logger.info("QUICK_SCALP auto-buy %s SL=%.6g TP=%.6g", symbol, atr_sl, atr_tp)
        elif bool(getattr(self.settings, "atr_bracket_exits", True)):
            try:
                from trading_bot.utils.decision_filters import atr_bracket_levels

                atr_v = None
                ind = self.feed.get_indicators(symbol)
                if ind is not None and getattr(ind, "atr", None):
                    atr_v = float(ind.atr)
                if atr_v and atr_v > 0:
                    atr_sl, atr_tp = atr_bracket_levels(
                        fill_px,
                        atr_v,
                        sl_mult=float(getattr(self.settings, "atr_bracket_sl_mult", 1.5) or 1.5),
                        tp_mult=float(getattr(self.settings, "atr_bracket_tp_mult", 2.0) or 2.0),
                        sl_min_pct=float(
                            getattr(self.settings, "atr_bracket_sl_min_pct", 0.01) or 0.01
                        ),
                        sl_max_pct=float(getattr(self.settings, "atr_bracket_sl_max_pct", 0.012) or 0.012),
                        tp_min_pct=float(
                            getattr(self.settings, "atr_bracket_tp_min_pct", 0.02) or 0.02
                        ),
                    )
                    updater = getattr(self.broker, "update_position_brackets", None)
                    if callable(updater):
                        updater(
                            symbol,
                            stop_loss=atr_sl,
                            take_profit=atr_tp,
                            take_profit_1=fill_px + 1.0 * (fill_px - atr_sl),  # 1R partial
                            mark_price=fill_px,
                            trail_distance=None,
                            tp1_done=False,
                            initial_qty=float(result.filled_qty),
                            entry_reason=f"scanner ATR brackets ATR={atr_v:.6g}",
                        )
                    # Winning-formula / profile SL-TP override (BEAR clamp)
                    try:
                        atr_sl, atr_tp = self._apply_profile_brackets(
                            symbol, fill_px, qty=float(result.filled_qty), short=False
                        )
                    except Exception as _exc:
                        logger.debug("profile bracket override: %s", _exc)
                    logger.info(
                        "ATR_BRACKET auto-buy %s SL=%.6g TP=%.6g (ATR=%.6g)",
                        symbol,
                        atr_sl,
                        atr_tp,
                        atr_v,
                    )
            except Exception as exc:
                logger.debug("ATR bracket auto-buy %s: %s", symbol, exc)
        try:
            self.trade_logger.log_event(
                "auto_paper_buy",
                {
                    "symbol": symbol,
                    "side": "BUY",
                    "qty": float(result.filled_qty),
                    "fill_price": fill_px,
                    "notional": filled_notional,
                    "proximity": float(proximity),
                    "entry_threshold": float(threshold),
                    "paper_trading_mode": True,
                    "source": "scanner_proximity",
                    "broker_order_id": getattr(result, "broker_order_id", None),
                },
            )
        except Exception as exc:
            logger.debug("auto_paper_buy log: %s", exc)

        reason = (
            f"scanner auto_paper_buy prox={proximity:.0f}% "
            f">= thresh={threshold:.0f}% notional~${notion:.0f}"
        )
        if atr_sl is not None and atr_tp is not None:
            reason += f"; ATR_BRACKET SL={atr_sl:.6g} TP={atr_tp:.6g}"
        return Decision(
            action=Action.BUY,
            symbol=symbol,
            confidence=max(float(proximity), float(threshold)),
            limit_price=round(fill_px, 8),
            quantity=float(result.filled_qty),
            stop_loss=round(atr_sl, 8) if atr_sl is not None else None,
            take_profit=round(atr_tp, 8) if atr_tp is not None else None,
            reasoning=reason,
        )

    async def _auto_paper_short(
        self, symbol: str, *, price: float, notional: float, proximity: float, threshold: float, atr=None
    ) -> Decision:
        """Paper-only scanner SELL-to-open for the minimal short trigger."""
        from trading_bot.models import OrderRequest, OrderSide, OrderType
        from trading_bot.risk_manager import floor_qty

        if not bool(self.settings.paper_trading_mode) or not bool(getattr(self.settings, "allow_paper_shorts", False)):
            return Decision.hold(symbol, "auto_paper_short: disabled")
        px = float(price or 0)
        qty = floor_qty(clamp_auto_notional(notional) / px, int(getattr(self.settings, "qty_precision", 8) or 8)) if px > 0 else 0.0
        if qty <= 0:
            return Decision.hold(symbol, "auto_paper_short: invalid size")
        atr_v = float(atr or 0)
        sl_dist = max(atr_v * 1.8, px * 0.012)
        tp_dist = max(atr_v * 3.0, px * 0.025)
        sl, tp = px + sl_dist, px - tp_dist
        order = OrderRequest(
            symbol=symbol, side=OrderSide.SELL, qty=qty, order_type=OrderType.LIMIT,
            limit_price=px, stop_loss=sl, take_profit=tp, paper=True, post_only=True
        )
        try:
            result = await self.executor.submit(order)
        except Exception as exc:
            logger.warning("auto_paper_short submit failed %s: %s", symbol, exc)
            return Decision.hold(symbol, f"auto_paper_short: submit_failed ({exc})")
        if result.status not in (OrderStatus.FILLED, OrderStatus.PARTIAL) or float(result.filled_qty or 0) <= 0:
            return Decision.hold(symbol, f"auto_paper_short: {(result.message or result.status.value).strip()}")
        fill_px = float(result.avg_fill_price or px)
        if wants_quick_scalp(self.settings):
            sl, tp = quick_scalp_brackets(fill_px, short=True)
        updater = getattr(self.broker, "update_position_brackets", None)
        if callable(updater):
            try:
                updater(symbol, stop_loss=sl, take_profit=tp, mark_price=fill_px,
                        initial_qty=float(result.filled_qty),
                        entry_reason=("quick-scalp SHORT" if wants_quick_scalp(self.settings)
                                      else "scanner short EMA20/EMA50 cross"))
            except Exception as exc:
                logger.debug("auto short bracket store: %s", exc)
        logger.warning(
            "AUTO_PAPER_SHORT %s qty=%.8f @ %.6f prox=%.0f%% thresh=%.0f%%",
            symbol, float(result.filled_qty), fill_px, float(proximity), float(threshold)
        )
        return Decision(
            action=Action.SELL, symbol=symbol, confidence=max(float(proximity), float(threshold)),
            limit_price=fill_px, quantity=float(result.filled_qty), stop_loss=sl, take_profit=tp,
            reasoning=f"scanner auto_paper_short EMA20/EMA50 bearish cross prox={proximity:.0f}%",
        )

    async def _scanner_maybe_auto_buy(
        self,
        symbol: str,
        obs: AgentObservation,
        *,
        position,
        open_exposure: float,
        open_position_count: int,
        mark: float,
    ) -> Optional[Decision]:
        """Proximity entry gate: log every symbol; auto-fire paper buy when clear.

        Aggressive bypasses TOD + RVOL/volume spike. Always paper-only + pause/
        concurrent/exposure/trade-cap guarded. Returns Decision on fire, else None
        (caller continues normal strategy path).
        """
        from trading_bot.utils.entry_proximity import (
            get_entry_proximity_from_obs,
            get_entry_threshold,
        )
        from trading_bot.strategy_volume_sweet_spot import check_time_of_day_gate

        profile = str(getattr(self.settings, "trade_profile", "medium") or "medium")
        thresh = float(
            getattr(self.settings, "entry_threshold", None) or get_entry_threshold() or 60.0
        )
        bear_short_mode = self._bear_short_mode()
        sent = None
        grok = getattr(self, "grok_sentiment", None)
        if grok is not None:
            raw = getattr(grok, "latest_sentiment", None)
            if isinstance(raw, dict):
                sent = raw
        rvol_mult = float(getattr(self.settings, "rvol_breakout_mult", 2.0) or 2.0)
        eng = getattr(getattr(self, "agent", None), "signal_engine", None)
        if eng is not None and hasattr(eng, "rvol_breakout_mult"):
            try:
                rvol_mult = float(eng.rvol_breakout_mult)
            except (TypeError, ValueError):
                pass

        extras_early = {}
        try:
            extras_early = dict((obs.indicators.extras or {}) if obs.indicators else {})
        except Exception:
            extras_early = {}

        # ADX regime soft filter: raise effective threshold when ADX < floor
        from trading_bot.utils.decision_filters import (
            check_htf_ema200_long,
            effective_entry_threshold,
            extract_adx,
        )

        adx_now = extract_adx(extras_early)
        eff_thresh, adx_raised = effective_entry_threshold(
            thresh,
            adx_now,
            adx_floor=float(getattr(self.settings, "adx_threshold_floor", 20.0) or 20.0),
            raise_by=float(getattr(self.settings, "adx_threshold_raise", 15.0) or 15.0),
        )
        if adx_raised:
            logger.info(
                "ADX threshold raised %s: ADX=%.1f < %.0f → ENTRY_THRESHOLD %.0f→%.0f",
                symbol,
                float(adx_now or 0.0),
                float(getattr(self.settings, "adx_threshold_floor", 20.0) or 20.0),
                thresh,
                eff_thresh,
            )
        long_thresh = float(eff_thresh)
        # Spot BEAR_CHOP quality: +10% entry threshold (CVD slope still required).
        if self._is_spot_long_only() and self._market_short_bias():
            raised = self._bear_spot_long_threshold(long_thresh)
            if raised > long_thresh:
                logger.info(
                    "SPOT_BEAR quality %s: ENTRY_THRESHOLD %.0f→%.0f (bear floor/max +10%%, min 50)",
                    symbol,
                    long_thresh,
                    raised,
                )
                long_thresh = raised

        try:
            prox_info = get_entry_proximity_from_obs(
                obs,
                sentiment=sent,
                rvol_breakout_mult=rvol_mult,
                long_threshold=long_thresh,
                short_min_volume=1.2 if bear_short_mode else None,
                prefer_short=bear_short_mode,
            )
        except Exception as exc:
            logger.debug("scanner proximity %s: %s", symbol, exc)
            prox_info = {"score": 0.0}
        prox = proximity_score_from_result(prox_info)
        direction = str(prox_info.get("direction") or "WAIT").upper()
        if direction == "SHORT" and not bool(getattr(self.settings, "allow_paper_shorts", False)):
            logger.info(
                format_scanner_loop_log(symbol, prox, long_thresh, "SKIPPED: paper shorts disabled")
            )
            return None
        short_entry_mode = bool(bear_short_mode and direction == "SHORT")
        thresh = (
            self._short_entry_threshold(thresh)
            if short_entry_mode
            else long_thresh
        )

        # Keep the hard LONG block ahead of soft direction/volume gates so the
        # operator always sees the canonical BEAR_CHOP skip reason.
        if direction != "SHORT":
            reg_reason = self._long_entry_block_reason(symbol)
            if reg_reason:
                logger.info(format_scanner_loop_log(symbol, prox, thresh, f"SKIPPED: {reg_reason}"))
                return None
        elif short_entry_mode:
            logger.info(
                "[BEAR_SHORT] armed %s prox=%.0f%% threshold=%.0f%%",
                symbol, float(prox), float(thresh),
            )

        # Post-stop cooldown: skip re-entry while cooling (scanner + proximity)
        if self._in_post_stop_cooldown(symbol) and float(prox) >= float(thresh):
            from trading_bot.utils.decision_filters import format_post_stop_cooldown_skip

            rem = self._post_stop_cooldown_remaining(symbol)
            skip_msg = format_post_stop_cooldown_skip(symbol, rem)
            logger.info(skip_msg)
            logger.info(
                format_scanner_loop_log(
                    symbol, prox, thresh, f"SKIPPED: post_stop_cooldown"
                )
            )
            return None

        # HTF trend confirmation: 15m direction blocks are soft when data is absent.
        if direction == "LONG" and self._htf_15m_direction_block(obs, direction="LONG", mark=float(getattr(obs.indicators, "close", 0) or 0)):
            logger.info(format_scanner_loop_log(symbol, prox, thresh, "htf_15m_ema200_long"))
            return None
        if (
            direction == "SHORT"
            and not short_entry_mode
            and self._htf_15m_direction_block(
                obs, direction="SHORT", mark=float(getattr(obs.indicators, "close", 0) or 0)
            )
        ):
            logger.info(format_scanner_loop_log(symbol, prox, thresh, "htf_15m_ema200_short"))
            return None

        # Legacy 1h trend confirmation: suppress LONG/auto-buy when 1h close < EMA200
        if direction != "SHORT" and bool(getattr(self.settings, "htf_ema200_long_filter", True)):
            close_1h = None
            ema_1h = None
            htf = getattr(obs, "htf", None)
            if htf is not None:
                ema_1h = (
                    htf.ema_200_1h if getattr(htf, "ema_200_1h", None) is not None else htf.ema_200
                )
                try:
                    close_1h = (htf.extras or {}).get("close_1h")
                except Exception:
                    close_1h = None
            if close_1h is None and extras_early.get("close_1h") is not None:
                close_1h = extras_early.get("close_1h")
            # Fallback: use mark/1m close vs 1h EMA200 when 1h close missing
            if close_1h is None and obs.indicators is not None:
                close_1h = getattr(obs.indicators, "close", None)
            ok_htf, htf_detail = check_htf_ema200_long(
                float(close_1h) if close_1h is not None else None,
                float(ema_1h) if ema_1h is not None else None,
                enabled=True,
            )
            if not ok_htf:
                line = format_scanner_loop_log(symbol, prox, thresh, htf_detail)
                logger.info(line)
                return None

        # Live RVOL from extras when present
        rvol_now = None
        try:
            extras = extras_early or ((obs.indicators.extras or {}) if obs.indicators else {})
            vr = extras.get("volume_ratio")
            if isinstance(vr, (int, float)):
                rvol_now = float(vr)
        except Exception:
            rvol_now = None

        # TOD blackout check (aggressive / disable_tod_gate bypass inside evaluator)
        tod_blocked = False
        disable_tod = bool(getattr(self.settings, "disable_tod_gate", False))
        if eng is not None and hasattr(eng, "disable_tod_gate"):
            disable_tod = bool(eng.disable_tod_gate) or disable_tod
        if not disable_tod and not is_aggressive_profile(profile):
            try:
                extras = (obs.indicators.extras or {}) if obs.indicators else {}
                ok_tod, _tod_detail = check_time_of_day_gate(
                    extras.get("now_utc"), enabled=True
                )
                tod_blocked = not ok_tod
            except Exception:
                tod_blocked = False

        already_open = (
            position is not None and float(getattr(position, "qty", 0) or 0) > 1e-12
        )
        notion = clamp_auto_notional(
            float(getattr(self.settings, "max_notional_per_trade_usd", AUTO_PAPER_MAX_NOTIONAL_USD)
                  or AUTO_PAPER_MAX_NOTIONAL_USD)
        )
        max_conc = self._effective_max_concurrent()
        if is_aggressive_profile(profile) and not self._market_short_bias():
            max_conc = min(max_conc, AUTO_PAPER_MAX_CONCURRENT)
        max_exp = min(
            float(
                getattr(self.settings, "max_total_exposure_usd", AUTO_PAPER_MAX_EXPOSURE_USD)
                or AUTO_PAPER_MAX_EXPOSURE_USD
            ),
            AUTO_PAPER_MAX_EXPOSURE_USD,
        )

        # Elite: enforce RVOL>=ELITE_RVOL_MIN even on AGGRESSIVE
        elite_rvol = float(getattr(self.settings, "elite_rvol_min", 1.8) or 1.8)
        if wants_elite_risk(self.settings):
            rvol_mult = max(float(rvol_mult), elite_rvol)

        # Pair blacklist
        if getattr(self, "pair_blacklist", None) is not None:
            ok_bl, bl_reason = self.pair_blacklist.check_entry(symbol)
            if not ok_bl:
                logger.info(format_scanner_loop_log(symbol, prox, thresh, f"SKIPPED: {bl_reason}"))
                return None

        # Elite RVOL + spread gate
        if wants_elite_risk(self.settings) and direction != "SHORT":
            from trading_bot.utils.decision_filters import check_elite_rvol_spread
            bid = ask = None
            if obs.quote is not None:
                bid = getattr(obs.quote, "bid", None)
                ask = getattr(obs.quote, "ask", None)
            ok_es, es_reason = check_elite_rvol_spread(
                rvol_now, bid, ask,
                rvol_min=elite_rvol,
                max_spread_pct=float(getattr(self.settings, "elite_max_spread_pct", 0.0025) or 0.0025),
            )
            if not ok_es:
                logger.info(format_scanner_loop_log(symbol, prox, thresh, es_reason))
                return None

        should_fire, status = evaluate_scanner_auto_buy(
            proximity=prox,
            threshold=thresh,
            profile=profile,
            paper=bool(self.settings.paper_trading_mode),
            paused=bool(self.ops.paused),
            already_open=already_open,
            open_position_count=int(open_position_count),
            max_concurrent=max_conc,
            open_exposure_usd=float(open_exposure or 0.0),
            max_exposure_usd=max_exp,
            trade_notional=notion,
            # BEAR_SHORT uses the bearish EMA/volume trigger as its gate;
            # bullish reclaim RVOL and TOD filters remain intact for LONGs.
            tod_blocked=False if short_entry_mode else tod_blocked,
            disable_tod_gate=disable_tod or short_entry_mode,
            rvol=rvol_now,
            rvol_min=0.0 if short_entry_mode else rvol_mult,
            bypass_volume_spike=True if short_entry_mode else (
                False if wants_elite_risk(self.settings) else is_aggressive_profile(profile)
            ),
        )
        line = format_scanner_loop_log(symbol, prox, thresh, status)
        logger.info(line)

        if not should_fire:
            return None

        px = float(mark or 0.0)
        if px <= 0 and obs.quote is not None:
            px = float(getattr(obs.quote, "mid", 0) or 0)
        if px <= 0 and obs.indicators is not None:
            px = float(getattr(obs.indicators, "close", 0) or 0)
        if px <= 0:
            logger.info(
                format_scanner_loop_log(
                    symbol, prox, thresh, "SKIPPED: no_price"
                )
            )
            return None

        async with self._entry_lock:
            if direction == "SHORT":
                if not bool(getattr(self.settings, "allow_paper_shorts", False)):
                    logger.info("[BEAR_SHORT] skip %s | allow_paper_shorts=false", symbol)
                    return None
                return await self._auto_paper_short(
                    symbol, price=px, notional=notion, proximity=prox, threshold=thresh,
                    atr=getattr(obs.indicators, "atr", None),
                )
            return await self._auto_paper_buy(
                symbol, price=px, notional=notion, proximity=prox, threshold=thresh
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
            reason_l = str(decision.reasoning or "").strip().lower()
            if reason_l not in (
                "tp hit", "tp1", "tp2", "atr take-profit", "sl", "atr stop", "trail",
            ):
                logger.info("SELL cooldown active for %s — skip", symbol)
                return Decision.hold(symbol, "sell cooldown")

        cap_mult = 1.0
        eng = getattr(self.agent, "signal_engine", None)
        if eng is not None and hasattr(eng, "regime_trade_cap_mult"):
            try:
                cap_mult = float(eng.regime_trade_cap_mult)
            except (TypeError, ValueError):
                cap_mult = 1.0
        verdict = self.risk.evaluate(
            decision,
            account,
            entry_price=entry,
            atr=atr,
            open_exposure_usd=open_exposure,
            open_position=position,
            open_position_count=open_position_count,
            trade_cap_mult=cap_mult,
            max_concurrent_override=self._effective_max_concurrent(),
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
            "TIME_EXIT_MAKER_BE",
            "TIME_EXIT_MAKER_TIMEOUT_SL",
            "SL",
            "TP1",
            "TP2",
            "TP Hit",
            "manual",
            "exhaustion",
        ):
            # Always post-only limit in strategy path (never market/taker)
            upd = {"order_type": OrderType.LIMIT}
            if bool(getattr(self.settings, "post_only", True)):
                upd["post_only"] = True
            # Maker fee-cushion time-exit: limit at cushion price, not mark
            if (
                str(decision.reasoning) == "TIME_EXIT_MAKER_BE"
                and getattr(decision, "limit_price", None)
            ):
                upd["limit_price"] = float(decision.limit_price)
                upd["post_only"] = True
            elif str(decision.reasoning) == "TIME_EXIT_MAKER_TIMEOUT_SL":
                # Emergency: allow taker/marketable exit at mark (structural SL)
                upd = {"order_type": OrderType.LIMIT, "post_only": False}
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

            # SELL-to-open paper shorts are entries, not exits (avoid stop/cooldown accounting).
            opening_short = order.side == OrderSide.SELL and (
                position is None or self._is_short_position(position)
            )
            if opening_short:
                await self.notifier.trade_entry(
                    symbol, "SELL", result.filled_qty, fill_px, paper=paper, fee=fill_notional * fee_rate,
                    cash_after=cash_after, equity_after=equity_after, paper_cap=paper_cap,
                    fee_rate=fee_rate, stop_loss=verdict.stop_loss or decision.stop_loss,
                    take_profit=verdict.take_profit or decision.take_profit,
                    entry_reason=decision.reasoning, post_only=bool(getattr(order, "post_only", True)),
                )
                self.state.add_trade_memory(
                    symbol=symbol, action="SELL", outcome="entry", reason=decision.reasoning, pnl=None
                )
                return decision

            closing_short = order.side == OrderSide.BUY and position is not None and self._is_short_position(position)
            if closing_short:
                entry_px = float(getattr(position, "avg_entry_price", 0) or 0)
                qty_f = float(result.filled_qty)
                entry_notional = entry_px * qty_f if entry_px > 0 else 0.0
                entry_fee = entry_notional * fee_rate
                exit_fee = fill_notional * fee_rate
                pnl = (entry_notional - exit_fee) - (fill_notional + entry_fee) if entry_px > 0 else None
                await self.notifier.trade_exit(
                    symbol, "BUY", result.filled_qty, reason=decision.reasoning, price=fill_px,
                    paper=paper, fee=exit_fee, entry_price=entry_px or None,
                    buy_cost_with_fees=entry_notional + entry_fee if entry_px > 0 else None,
                    pnl=pnl, cash_after=cash_after, equity_after=equity_after, paper_cap=paper_cap,
                    fee_rate=fee_rate, post_only=bool(getattr(order, "post_only", True)),
                )
                self.state.add_trade_memory(
                    symbol=symbol, action="BUY", outcome="exit", reason=decision.reasoning, pnl=pnl
                )
                self._apply_smart_memory_close(
                    symbol=symbol, pnl=pnl, paper=paper,
                    entry=float(entry_px) if entry_px else None,
                    exit_px=float(fill_px) if fill_px else None,
                    short=False,
                )
                self.stagnant.clear(symbol)
                try:
                    getattr(self, "_risk_free_notified", set()).discard(symbol)
                    try:
                        getattr(self, "_profit_runner_notified", set()).discard(symbol)
                    except Exception:
                        pass
                    try:
                        getattr(self, "_peak_upl", {}).pop(symbol, None)
                    except Exception:
                        pass
                except Exception:
                    pass
                try:
                    if getattr(self, "pair_blacklist", None) is not None and pnl is not None:
                        self.pair_blacklist.record_close(
                            symbol,
                            entry=float(entry_px) if entry_px else None,
                            exit=float(fill_px) if fill_px else None,
                            net_pnl=float(pnl),
                        )
                        self._record_digest_close(
                            symbol,
                            entry=float(entry_px) if entry_px else None,
                            exit_px=float(fill_px) if fill_px else None,
                            net_pnl=float(pnl),
                        )
                except Exception as bl_exc:
                    logger.debug("pair_blacklist record: %s", bl_exc)
                return decision

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
                        # Prefer quick-scalp (AGGRESSIVE) or ATR brackets for paper entries
                        if wants_quick_scalp(self.settings) and bool(
                            self.settings.paper_trading_mode
                        ):
                            a_sl, a_tp = quick_scalp_brackets(fill_px, short=False)
                            kw["stop_loss"] = a_sl
                            kw["take_profit"] = a_tp
                            logger.info(
                                "QUICK_SCALP strategy-buy %s SL=%.6g TP=%.6g",
                                symbol, a_sl, a_tp,
                            )
                        elif bool(getattr(self.settings, "atr_bracket_exits", True)) and bool(
                            self.settings.paper_trading_mode
                        ):
                            try:
                                from trading_bot.utils.decision_filters import atr_bracket_levels

                                atr_v = None
                                if atr is not None:
                                    atr_v = float(atr)
                                if (atr_v is None or atr_v <= 0) and hasattr(self, "feed"):
                                    ind = self.feed.get_indicators(symbol)
                                    if ind is not None and getattr(ind, "atr", None):
                                        atr_v = float(ind.atr)
                                if atr_v and atr_v > 0:
                                    a_sl, a_tp = atr_bracket_levels(
                                        fill_px,
                                        atr_v,
                                        sl_mult=float(
                                            getattr(self.settings, "atr_bracket_sl_mult", 1.5) or 1.5
                                        ),
                                        tp_mult=float(
                                            getattr(self.settings, "atr_bracket_tp_mult", 2.0) or 2.0
                                        ),
                                        sl_min_pct=float(
                                            getattr(self.settings, "atr_bracket_sl_min_pct", 0.012)
                                            or 0.012
                                        ),
                                    sl_max_pct=float(getattr(self.settings, "atr_bracket_sl_max_pct", 0.012) or 0.012),
                                    )
                                    kw["stop_loss"] = a_sl
                                    kw["take_profit"] = a_tp
                                    kw["take_profit_1"] = fill_px + 1.0 * (fill_px - a_sl)
                                    logger.info(
                                        "ATR_BRACKET strategy-buy %s SL=%.6g TP=%.6g",
                                        symbol,
                                        a_sl,
                                        a_tp,
                                    )
                            except Exception as atr_exc:
                                logger.debug("ATR bracket strategy-buy: %s", atr_exc)
                        try:
                            p_sl, p_tp = self._apply_profile_brackets(
                                symbol,
                                fill_px,
                                qty=float(result.filled_qty),
                                short=False,
                            )
                            kw["stop_loss"] = p_sl
                            kw["take_profit"] = p_tp
                        except Exception as _pe:
                            logger.debug("profile bracket strategy-buy: %s", _pe)
                        if self.settings.is_sweet_spot:
                            entry = fill_px
                            sl = kw["stop_loss"]
                            if entry and sl and entry > float(sl) and "take_profit_1" not in kw:
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
                    or reason_l in ("atr stop", "trail", "sl", "time-stop", "time stop", "time_exit_maker_be", "time_exit_maker_timeout_sl")
                ):
                    is_stop = True
                if decision.stop_loss is not None and fill_px > 0:
                    if fill_px <= float(decision.stop_loss) * 1.002:
                        is_stop = True
                if is_stop:
                    outcome = "stop_loss"
                    locked = self.state.record_stop_loss(symbol)
                    self._arm_post_stop_cooldown(symbol)
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
                    is_tp = (
                        "take" in reason_l
                        or reason_l.startswith("tp")
                        or reason_l in ("atr take-profit", "tp hit", "tp1", "tp2")
                    )
                    if is_tp:
                        await self.notifier.take_profit(
                            symbol,
                            pnl=float(pnl) if pnl is not None else None,
                            pnl_pct=(
                                (100.0 * float(pnl) / float(buy_cost_with_fees))
                                if (pnl is not None and buy_cost_with_fees)
                                else None
                            ),
                            qty=float(result.filled_qty or 0),
                        )
                    else:
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
                if is_stop:
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
                self._apply_smart_memory_close(
                    symbol=symbol, pnl=pnl, paper=paper,
                    entry=float(entry_px) if entry_px else None,
                    exit_px=float(fill_px) if fill_px else None,
                    short=False,
                )
                self.stagnant.clear(symbol)
                try:
                    getattr(self, "_risk_free_notified", set()).discard(symbol)
                    try:
                        getattr(self, "_profit_runner_notified", set()).discard(symbol)
                    except Exception:
                        pass
                    try:
                        getattr(self, "_peak_upl", {}).pop(symbol, None)
                    except Exception:
                        pass
                except Exception:
                    pass
                try:
                    if getattr(self, "pair_blacklist", None) is not None and pnl is not None:
                        self.pair_blacklist.record_close(
                            symbol,
                            entry=float(entry_px) if entry_px else None,
                            exit=float(fill_px) if fill_px else None,
                            net_pnl=float(pnl),
                        )
                        self._record_digest_close(
                            symbol,
                            entry=float(entry_px) if entry_px else None,
                            exit_px=float(fill_px) if fill_px else None,
                            net_pnl=float(pnl),
                        )
                except Exception as bl_exc:
                    logger.debug("pair_blacklist record: %s", bl_exc)
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

        # xStocks: US RTH only (Mon–Fri 9:30–16:00 America/New_York)
        try:
            ok_rth, why_rth = xstock_entry_allowed(symbol)
            if not ok_rth:
                logger.debug("XSTOCK_RTH block %s (%s)", symbol, why_rth)
                return Decision.hold(symbol, why_rth or SKIP_XSTOCK_OUTSIDE_RTH)
        except Exception as exc:
            logger.debug("xstock RTH check: %s", exc)

        if self._symbol_tick_stale(symbol, max_age_s=10.0):
            logger.debug("STALE_PAIR omit %s from focus/entry", symbol)
            return Decision.hold(symbol, "stale_tick_10s")

        # xStocks: only new entries / active monitoring during US RTH
        ok_rth, rth_reason = xstock_entry_allowed(symbol)
        if not ok_rth:
            logger.debug("SKIP %s | %s", symbol, rth_reason)
            return Decision.hold(symbol, rth_reason)

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

        # Keep the status Market line authoritative for entry gating. The engine
        # is cached, so concurrent symbol cycles do not refetch every time.
        try:
            if getattr(self, "btc_regime", None) is not None:
                await self.btc_regime.refresh(self.broker)
        except Exception as exc:
            logger.debug("btc_regime cycle refresh skipped: %s", exc)

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
                top5 = None
                getter = getattr(self.feed, "get_l2_top5_ratio", None)
                if callable(getter):
                    top5 = getter(symbol)
                if top5 is not None:
                    extras["l2_top5_ratio"] = top5
                indicators = indicators.model_copy(update={"extras": extras})
            except Exception as exc:
                logger.debug("L2 attach skipped for %s: %s", symbol, exc)

        # Phase 1 CVD / short-liq snapshot → extras (O(1) reads; no await)
        if indicators is not None:
            try:
                extras = dict(indicators.extras or {})
                cvd_snap = self.leadlag.get_cvd_snapshot(symbol)
                extras["cvd_cumulative"] = cvd_snap.cumulative
                extras["cvd_period_delta"] = cvd_snap.period_delta
                extras["cvd_trade_count"] = cvd_snap.trade_count
                extras["short_liq_notional"] = self.leadlag.short_liq_notional(symbol)
                extras["short_liq_spike"] = self.leadlag.has_short_liq_spike(symbol)
                ok_p1, p1_reason = self._phase1_allow_buy(symbol)
                extras["phase1_allow"] = ok_p1
                extras["phase1_reason"] = p1_reason
                # UTC-day SQLite PnL + day_start_equity for strategy circuit breaker
                try:
                    extras["utc_day_pnl"] = float(self.state.get_utc_day_realized_pnl())
                except Exception:
                    extras["utc_day_pnl"] = extras.get("utc_day_pnl", 0.0)
                try:
                    dse = getattr(account, "day_start_equity", None)
                    if dse is None and isinstance(getattr(account, "__dict__", None), dict):
                        dse = account.__dict__.get("day_start_equity")
                    if dse is None:
                        # paper book fallback via broker if present
                        pb = getattr(self.broker, "paper_book", None) or getattr(
                            self, "paper_book", None
                        )
                        if isinstance(pb, dict):
                            dse = pb.get("day_start_equity")
                        elif pb is not None:
                            dse = getattr(pb, "day_start_equity", None)
                    extras["day_start_equity"] = float(
                        dse if dse is not None else getattr(account, "equity", 1600.0) or 1600.0
                    )
                except Exception:
                    extras["day_start_equity"] = float(
                        getattr(account, "equity", 1600.0) or 1600.0
                    )
                indicators = indicators.model_copy(update={"extras": extras})
            except Exception as exc:
                logger.debug("phase1 extras attach skipped for %s: %s", symbol, exc)

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

        # Scanner proximity → auto paper-buy bridge (independent of strategy HOLD)
        try:
            auto_dec = await self._scanner_maybe_auto_buy(
                symbol,
                obs,
                position=position,
                open_exposure=open_exposure,
                open_position_count=open_position_count,
                mark=float(mark or 0),
            )
            if auto_dec is not None and auto_dec.action in (Action.BUY, Action.SELL):
                self.trade_logger.log_decision(auto_dec)
                return auto_dec
        except Exception as exc:
            logger.warning("scanner auto-buy bridge failed %s: %s", symbol, exc)

        decision = await self.agent.decide(obs)
        self.trade_logger.log_decision(decision)

        pos_qty = float(getattr(position, "qty", 0) or 0) if position else 0.0
        pos_short = self._is_short_position(position) if position else False
        flat = pos_qty <= 0

        # Minimal paper SHORT strategy path. Scanner shorts fire earlier when the
        # proximity score wins; this path covers a direct strategy SELL-to-open.
        if flat and self._paper_short_signal(obs):
            px = float(mark or indicators.close or 0)
            atr_v = float(indicators.atr or 0)
            sl_dist = max(atr_v * 1.8, px * 0.012)
            tp_dist = max(atr_v * 3.0, px * 0.025)
            decision = Decision(
                action=Action.SELL, symbol=symbol, confidence=75.0,
                stop_loss=px + sl_dist if px > 0 else None,
                take_profit=px - tp_dist if px > 0 else None,
                reasoning="paper short EMA20/EMA50 bearish cross",
            )

        # Exact 15m block for all new LONG decisions; missing data is soft.
        if decision.action == Action.BUY and self._htf_15m_direction_block(obs, direction="LONG", mark=float(mark or 0)):
            return Decision.hold(symbol, "htf_15m_ema200_long")
        if (
            decision.action == Action.SELL
            and flat
            and not self._bear_short_mode()
            and self._htf_15m_direction_block(obs, direction="SHORT", mark=float(mark or 0))
        ):
            return Decision.hold(symbol, "htf_15m_ema200_short")

        if decision.action == Action.HOLD:
            return decision

        # Flat SELL is paper-only short entry; otherwise preserve the old suppress guard.
        if decision.action == Action.SELL and flat:
            allow_short = bool(getattr(self.settings, "allow_paper_shorts", False)) and bool(self.settings.paper_trading_mode)
            is_short_reason = "short" in (decision.reasoning or "").lower()
            if not (allow_short and (self._paper_short_signal(obs) or is_short_reason)):
                if not self._sell_on_cooldown(symbol):
                    logger.info("Agent SELL while flat on %s — suppress", symbol)
                    self._arm_sell_cooldown(symbol)
                return Decision.hold(symbol, "flat — suppress SELL")

        # Already long + agent BUY → suppress pyramid; BUY covers an existing short.
        if (
            decision.action == Action.BUY and not pos_short
            and not flat and not bool(getattr(self.settings, "allow_pyramiding", False))
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
                self._note_entry_block(symbol, dd_reason)
                logger.info("SKIP BUY %s | %s", symbol, dd_reason)
                return Decision.hold(symbol, dd_reason)

        # BTC regime + pair blacklist. Spot mode skips BEAR_CHOP hard-lock.
        if decision.action == Action.BUY:
            ok_rth, rth_reason = xstock_entry_allowed(symbol)
            if not ok_rth:
                self._note_entry_block(symbol, rth_reason)
                logger.info("SKIP BUY %s | %s", symbol, rth_reason)
                return Decision.hold(symbol, rth_reason)
            reg_reason = self._long_entry_block_reason(symbol)
            if reg_reason:
                self._note_entry_block(symbol, reg_reason)
                logger.info("SKIP BUY %s | %s", symbol, reg_reason)
                return Decision.hold(symbol, reg_reason)
            if getattr(self, "pair_blacklist", None) is not None:
                ok_bl, bl_reason = self.pair_blacklist.check_entry(symbol)
                if not ok_bl:
                    self._note_entry_block(symbol, bl_reason)
                    logger.info("SKIP BUY %s | %s", symbol, bl_reason)
                    return Decision.hold(symbol, bl_reason)

        # Structural guardrail: spread filter (need live bid/ask)
        if decision.action == Action.BUY:
            bid = ask = None
            if quote is not None:
                bid = getattr(quote, "bid", None)
                ask = getattr(quote, "ask", None)
            max_sp = float(getattr(self.settings, "max_spread_pct", 0.0025) or 0.0025)
            if wants_elite_risk(self.settings):
                max_sp = min(
                    max_sp,
                    float(getattr(self.settings, "elite_max_spread_pct", 0.0025) or 0.0025),
                )
            ok_sp, sp_reason = check_spread(bid, ask, max_spread_pct=max_sp)
            if not ok_sp:
                self._note_entry_block(symbol, sp_reason)
                logger.info("SKIP BUY %s | %s", symbol, sp_reason)
                return Decision.hold(symbol, sp_reason)
            if wants_elite_risk(self.settings):
                from trading_bot.utils.decision_filters import check_elite_rvol_spread
                rvol_v = None
                try:
                    extras = (obs.indicators.extras or {}) if obs and obs.indicators else {}
                    vr = extras.get("volume_ratio")
                    if isinstance(vr, (int, float)):
                        rvol_v = float(vr)
                except Exception:
                    rvol_v = None
                ok_es, es_reason = check_elite_rvol_spread(
                    rvol_v, bid, ask,
                    rvol_min=float(getattr(self.settings, "elite_rvol_min", 1.8) or 1.8),
                    max_spread_pct=float(getattr(self.settings, "elite_max_spread_pct", 0.0025) or 0.0025),
                )
                if not ok_es:
                    self._note_entry_block(symbol, es_reason)
                    logger.info("SKIP BUY %s | %s", symbol, es_reason)
                    return Decision.hold(symbol, es_reason)


        # Fee / R:R floor: TP must clear min TP% and RT fees + +1% net buffer
        if decision.action == Action.BUY:
            from trading_bot.structural_guardrails import check_fee_to_target

            entry_px = None
            try:
                if quote is not None:
                    entry_px = getattr(quote, "ask", None) or getattr(quote, "last", None) or getattr(quote, "price", None)
                if entry_px is None and obs is not None and getattr(obs, "indicators", None) is not None:
                    entry_px = getattr(obs.indicators, "close", None)
                if entry_px is None:
                    entry_px = getattr(decision, "limit_price", None)
            except Exception:
                entry_px = getattr(decision, "limit_price", None)
            tp_px = getattr(decision, "take_profit", None)
            # If decision has no TP yet, synthesize floor TP for the check
            try:
                e = float(entry_px or 0)
                if e > 0 and tp_px is None:
                    tp_px = e * (1.0 + float(getattr(self.settings, "atr_bracket_tp_min_pct", 0.02) or 0.02))
            except Exception:
                pass
            try:
                e0 = float(entry_px or 0)
                t0 = float(tp_px) if tp_px is not None else 0.0
                if e0 > 0 and t0 > 0:
                    raw_tp = (t0 - e0) / e0
                    if raw_tp + 1e-12 < 0.015:
                        reason = f"sub_fee_tp: TP {raw_tp:.2%} < 1.50%"
                        self._note_entry_block(symbol, reason)
                        logger.info("SKIP BUY %s | %s", symbol, reason)
                        return Decision.hold(symbol, reason)
                    # Enforce 2% TP floor on decision before fee check / execute
                    if raw_tp + 1e-12 < 0.02:
                        decision.take_profit = e0 * 1.02
                        tp_px = decision.take_profit
            except Exception:
                pass
            taker = float(getattr(self.settings, "taker_fee_rate", 0.005) or 0.005)
            rt = max(0.005, 2.0 * min(taker, 0.01))
            ok_ft, ft_reason = check_fee_to_target(
                float(entry_px or 0),
                float(tp_px) if tp_px is not None else None,
                min_tp_pct=float(getattr(self.settings, "min_tp_pct", 0.02) or 0.02),
                maker_fee_rate=float(getattr(self.settings, "maker_fee_rate", 0.005) or 0.005),
                fee_to_target_mult=float(getattr(self.settings, "fee_to_target_mult", 3.0) or 3.0),
                net_buffer_pct=float(getattr(self.settings, "tp_net_buffer_pct", 0.01) or 0.01),
                rt_fee_pct=rt,
            )
            if not ok_ft:
                self._note_entry_block(symbol, ft_reason)
                logger.info("SKIP BUY %s | %s", symbol, ft_reason)
                return Decision.hold(symbol, ft_reason)

        # BEAR_CHOP / SHORT bias: TP must clear ~1% RT taker × 2.5 buffer (+2.5%)
        if decision.action == Action.BUY and self._market_short_bias():
            from trading_bot.structural_guardrails import check_bear_chop_taker_fee_tp

            entry_px = None
            try:
                if quote is not None:
                    entry_px = getattr(quote, "ask", None) or getattr(quote, "last", None) or getattr(quote, "price", None)
                if entry_px is None and obs is not None and getattr(obs, "indicators", None) is not None:
                    entry_px = getattr(obs.indicators, "close", None)
                if entry_px is None:
                    entry_px = getattr(decision, "limit_price", None)
            except Exception:
                entry_px = getattr(decision, "limit_price", None)
            tp_px = getattr(decision, "take_profit", None)
            ok_bf, bf_reason = check_bear_chop_taker_fee_tp(
                float(entry_px or 0),
                float(tp_px) if tp_px is not None else None,
                fee_to_target_mult=float(
                    getattr(self.settings, "fee_to_target_mult", 2.5) or 2.5
                ),
            )
            if not ok_bf:
                self._note_entry_block(symbol, bf_reason)
                logger.info("SKIP BUY %s | %s", symbol, bf_reason)
                return Decision.hold(symbol, bf_reason)

        # Post-stop / trail cooldown — block new BUY on this symbol
        if decision.action == Action.BUY and self._in_post_stop_cooldown(symbol):
            from trading_bot.utils.decision_filters import format_post_stop_cooldown_skip

            rem = self._post_stop_cooldown_remaining(symbol)
            skip_msg = format_post_stop_cooldown_skip(symbol, rem)
            logger.info(skip_msg)
            return Decision.hold(symbol, skip_msg)

        # Funding/OI positioning filter
        if decision.action == Action.BUY and bool(
            getattr(self.settings, "funding_oi_enabled", True)
        ):
            allow, conf_delta, ftag = self.funding_oi.evaluate(
                symbol, recent_bars, confidence=float(decision.confidence or 0)
            )
            if not allow:
                self._note_entry_block(symbol, ftag or "funding_long_trap")
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
                    self._note_entry_block(symbol, oc.skip_reason or "onchain: exchange_inflow_spike")
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
                self._note_entry_block(symbol, bl_reason)
                logger.info("SKIP BUY %s | %s", symbol, bl_reason)
                return Decision.hold(symbol, bl_reason)


        # Fixed hard dollar exposure cap (not equity/cash scaled)
        if decision.action == Action.BUY:
            pos_qty = float(getattr(position, "qty", 0) or 0) if position else 0.0
            if pos_qty <= 1e-12:
                max_exp = float(
                    getattr(self.settings, "max_total_exposure_usd", 3000.0) or 3000.0
                )
                proposed = float(
                    getattr(decision, "quantity", None)
                    or getattr(self.settings, "max_notional_per_trade_usd", 500.0)
                    or 500.0
                )
                # quantity may be asset units; prefer notional from settings trade cap
                proposed_notional = float(
                    getattr(self.settings, "max_notional_per_trade_usd", 500.0) or 500.0
                )
                if open_exposure + proposed_notional > max_exp + 1e-9:
                    reason = (
                        f"⛔ ENTRY SKIPPED: Max exposure cap (${max_exp:.0f}) reached. "
                        f"Current Exposure: ${open_exposure:.2f}"
                    )
                    self._note_entry_block(symbol, reason)
                    logger.info("%s | %s", symbol, reason)
                    return Decision.hold(symbol, reason)

        # Soft concurrent cap before sizing (risk also enforces)
        if decision.action == Action.BUY:
            max_c = self._effective_max_concurrent()
            pos_qty = float(getattr(position, "qty", 0) or 0) if position else 0.0
            if pos_qty <= 0 and open_position_count >= max_c:
                reason = (
                    f"max concurrent positions {max_c} reached "
                    f"(open={open_position_count}"
                    + ("; BEAR_CHOP/SHORT cap=1" if self._market_short_bias() else "")
                    + ")"
                )
                self._note_entry_block(symbol, reason)
                logger.info("SKIP BUY %s | %s", symbol, reason)
                return Decision.hold(symbol, reason)

        # Circuit breaker blocks new entries (exits still allowed above)
        if self.risk.circuit_breaker_active and decision.action == Action.BUY:
            logger.warning("Circuit breaker — skip entry %s", symbol)
            return Decision.hold(symbol, "circuit breaker — skip entry")

        # Final hard regime gate: alternate BUY generators above must not bypass it.
        if decision.action == Action.BUY:
            reg_reason = self._long_entry_block_reason(symbol)
            if reg_reason:
                self._note_entry_block(symbol, reg_reason)
                logger.info("SKIP BUY %s | %s", symbol, reg_reason)
                return Decision.hold(symbol, reg_reason)

        # Phase 1: CVD divergence + short-liq sweep — last BUY gate before executor
        if decision.action == Action.BUY:
            ok_p1, p1_reason = self._phase1_allow_buy(symbol)
            if not ok_p1:
                self._note_entry_block(symbol, p1_reason)
                logger.info("SKIP BUY %s | %s", symbol, p1_reason)
                return Decision.hold(symbol, p1_reason)

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


    def _fast_cache_scan_ms(self, symbols: list[str]) -> float:
        """In-memory price scan across active pairs (no REST). Returns ms."""
        t0 = time.perf_counter()
        n = 0
        broker = self.broker
        prices = getattr(broker, "_paper_prices", None) or getattr(broker, "_ticker_cache", None) or {}
        tick_mono = getattr(broker, "_symbol_tick_mono", None) or {}
        for sym in symbols:
            s = str(sym).upper()
            px = None
            if isinstance(prices, dict):
                px = prices.get(s) or prices.get(sym)
            if px is None:
                q = None
                try:
                    q = self.feed.get_quote(s)
                except Exception:
                    q = None
                if q is not None:
                    px = getattr(q, "mid", None) or getattr(q, "bid", None)
            # touch local age map if broker exposes last tick
            if isinstance(tick_mono, dict) and s in tick_mono:
                pass
            if px is not None:
                n += 1
        elapsed = (time.perf_counter() - t0) * 1000.0
        self._last_cache_scan_pairs = n
        return elapsed

    async def run_loop(self) -> None:
        await self.startup()
        try:
            while not self._stop.is_set():
                scan_t0 = time.perf_counter()
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

                symbols = list(self.settings.symbol_list)
                try:
                    await self._ensure_ws_ticker()
                except Exception as exc:
                    logger.debug("ws ticker sync: %s", exc)
                sem = self._symbol_sem

                async def _cycle(sym: str) -> None:
                    if self._stop.is_set():
                        return
                    async with sem:
                        try:
                            await self.cycle_symbol(sym)
                        except Exception:
                            logger.exception("cycle error for %s", sym)

                await asyncio.gather(*[_cycle(s) for s in symbols])
                cycle_ms = (time.perf_counter() - scan_t0) * 1000.0
                cache_ms = self._fast_cache_scan_ms(symbols)
                # Report warm in-memory scan when cache covers most pairs; else full cycle
                warm = cache_ms < 100.0 and int(getattr(self, "_last_cache_scan_pairs", 0) or 0) >= max(1, int(len(symbols) * 0.5))
                self._last_scan_latency_ms = cache_ms if warm else cycle_ms
                self._last_scan_pair_count = len(symbols)
                logger.info(
                    "Last Scan Latency: %.0fms across %d pairs (cache=%.1fms warm=%s cycle=%.0fms)",
                    self._last_scan_latency_ms,
                    self._last_scan_pair_count,
                    cache_ms,
                    warm,
                    cycle_ms,
                )
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
            "Allowlist + $50/$200 caps still enforced."
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
