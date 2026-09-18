"""Application configuration loaded from environment / .env."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Al locked production allowlist — hard-enforced (CLI/env cannot add others).
HARD_SYMBOL_ALLOWLIST: frozenset[str] = frozenset(
    {
        "BTC-USD",
        "ETH-USD",
        "SOL-USD",
        "XRP-USD",
        "LINK-USD",
        "AVAX-USD",
        "SUI-USD",
        "ADA-USD",
    }
)


class Settings(BaseSettings):
    """Runtime settings. Secrets come only from env / .env — never hardcoded."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Mode
    paper_trading_mode: bool = Field(default=True, alias="PAPER_TRADING_MODE")
    dry_run: bool = Field(default=False, alias="DRY_RUN")

    # Account — Al locked $200 trading book (Coinbase crypto VWAP scalps)
    account_equity: float = Field(default=200.0, alias="ACCOUNT_EQUITY")
    symbols: str = Field(
        default="BTC-USD,ETH-USD,SOL-USD,XRP-USD,LINK-USD,AVAX-USD,SUI-USD,ADA-USD",
        alias="SYMBOLS",
    )
    # Absolute live caps (not merely % of book)
    max_notional_per_trade_usd: float = Field(default=50.0, alias="MAX_NOTIONAL_PER_TRADE_USD")
    min_notional_usd: float = Field(default=10.0, alias="MIN_NOTIONAL_USD")
    max_total_exposure_usd: float = Field(default=200.0, alias="MAX_TOTAL_EXPOSURE_USD")
    qty_precision: int = Field(default=8, alias="QTY_PRECISION")
    quiet_notifier: bool = Field(default=True, alias="QUIET_NOTIFIER")
    # Max hold before hard time-stop exit (minutes). Sweet-spot default 30.
    max_hold_minutes: int = Field(default=30, alias="MAX_HOLD_MINUTES")
    # Default OFF: reject new BUY when already long that symbol
    allow_pyramiding: bool = Field(default=False, alias="ALLOW_PYRAMIDING")
    paper_book_path: str = Field(
        default=str(PROJECT_ROOT / "data" / "paper_book.json"),
        alias="PAPER_BOOK_PATH",
    )
    # Cooldown after rejected SELL to avoid log/Telegram spam (seconds)
    sell_reject_cooldown_seconds: float = Field(
        default=60.0, alias="SELL_REJECT_COOLDOWN_SECONDS"
    )

    # Risk (scalp-friendlier position / ATR; keep 1.5%/2% risk + 3% DD)
    max_risk_per_trade_pct: float = Field(default=0.015, alias="MAX_RISK_PER_TRADE_PCT")
    max_risk_per_trade_pct_ceiling: float = Field(default=0.02, alias="MAX_RISK_PER_TRADE_PCT_CEILING")
    daily_drawdown_limit_pct: float = Field(default=0.03, alias="DAILY_DRAWDOWN_LIMIT_PCT")
    max_position_pct: float = Field(default=0.25, alias="MAX_POSITION_PCT")
    trailing_stop_atr_mult: float = Field(default=1.5, alias="TRAILING_STOP_ATR_MULT")
    take_profit_atr_mult: float = Field(default=1.5, alias="TAKE_PROFIT_ATR_MULT")
    stop_loss_atr_mult: float = Field(default=1.0, alias="STOP_LOSS_ATR_MULT")

    # Alpaca
    alpaca_api_key: str = Field(default="", alias="ALPACA_API_KEY")
    alpaca_secret_key: str = Field(default="", alias="ALPACA_SECRET_KEY")
    alpaca_base_url: str = Field(
        default="https://paper-api.alpaca.markets",
        alias="ALPACA_BASE_URL",
    )
    alpaca_data_url: str = Field(
        default="https://data.alpaca.markets",
        alias="ALPACA_DATA_URL",
    )
    alpaca_ws_url: str = Field(
        default="wss://stream.data.alpaca.markets/v2/iex",
        alias="ALPACA_WS_URL",
    )

    # Coinbase Advanced Trade (CDP API key name + private key PEM)
    coinbase_api_key: str = Field(default="", alias="COINBASE_API_KEY")
    coinbase_api_secret: str = Field(default="", alias="COINBASE_API_SECRET")
    # Optional override; production Advanced Trade host is api.coinbase.com
    coinbase_base_url: str = Field(
        default="https://api.coinbase.com",
        alias="COINBASE_BASE_URL",
    )

    # Kraken spot (Instance #2). Secret is the base64 API-Sign key from Kraken.
    kraken_api_key: str = Field(default="", alias="KRAKEN_API_KEY")
    kraken_api_secret: str = Field(default="", alias="KRAKEN_API_SECRET")
    kraken_base_url: str = Field(
        default="https://api.kraken.com",
        alias="KRAKEN_BASE_URL",
    )
    kraken_ws_url: str = Field(
        default="wss://ws.kraken.com",
        alias="KRAKEN_WS_URL",
    )

    # Broker selection: alpaca | coinbase | kraken | mock | ib
    broker: str = Field(default="coinbase", alias="BROKER")

    # Feed / agent loop — 1m bars, faster poll for scalps
    bar_timeframe: str = Field(default="1Min", alias="BAR_TIMEFRAME")
    lookback_bars: int = Field(default=100, alias="LOOKBACK_BARS")
    agent_poll_seconds: float = Field(default=3.0, alias="AGENT_POLL_SECONDS")
    stale_data_seconds: float = Field(default=120.0, alias="STALE_DATA_SECONDS")
    slippage_bps: float = Field(default=5.0, alias="SLIPPAGE_BPS")
    taker_fee_rate: float = Field(default=0.009, alias="TAKER_FEE_RATE")
    maker_fee_rate: float = Field(default=0.005, alias="MAKER_FEE_RATE")
    # Fee-clearance cushion (legacy VWAP+ATR mode only). Sweet-spot uses MIN_TP_PCT.
    fee_clear_mult: float = Field(default=2.0, alias="FEE_CLEAR_MULT")

    # --- Volume Sweet Spot strategy (Option B — replaces VWAP+ATR scalp exits) ---
    # STRATEGY_MODE=volume_sweet_spot | vwap_scalp (legacy)
    strategy_mode: str = Field(default="volume_sweet_spot", alias="STRATEGY_MODE")
    rvol_breakout_mult: float = Field(default=2.0, alias="RVOL_BREAKOUT_MULT")
    pullback_vol_frac: float = Field(default=0.5, alias="PULLBACK_VOL_FRAC")
    min_tp_pct: float = Field(default=0.025, alias="MIN_TP_PCT")
    min_clear_to_resistance_pct: float = Field(
        default=0.02, alias="MIN_CLEAR_TO_RESISTANCE_PCT"
    )
    swing_sl_buffer_pct: float = Field(default=0.002, alias="SWING_SL_BUFFER_PCT")
    post_only: bool = Field(default=True, alias="POST_ONLY")
    # Structural guardrails
    # Block duplicate BUY attempts on same symbol within this window (seconds)
    buy_dedupe_seconds: float = Field(default=300.0, alias="BUY_DEDUPE_SECONDS")
    # Max bid-ask spread as fraction of mid (0.001 = 0.1%)
    max_spread_pct: float = Field(default=0.001, alias="MAX_SPREAD_PCT")
    # Min TP distance >= max(MIN_TP_PCT, FEE_TO_TARGET_MULT * 2 * MAKER_FEE_RATE)
    fee_to_target_mult: float = Field(default=2.5, alias="FEE_TO_TARGET_MULT")
    tp1_rr: float = Field(default=1.0, alias="TP1_RR")
    tp2_rr: float = Field(default=2.5, alias="TP2_RR")
    tp1_fraction: float = Field(default=0.5, alias="TP1_FRACTION")
    rvol_exhaustion_mult: float = Field(default=3.0, alias="RVOL_EXHAUSTION_MULT")
    volume_sma_period: int = Field(default=20, alias="VOLUME_SMA_PERIOD")
    # BUY entry quality
    min_confidence: float = Field(default=62.0, alias="MIN_CONFIDENCE")
    rsi_buy_cap: float = Field(default=72.0, alias="RSI_BUY_CAP")
    # After stop/trail exit, block new BUY on that symbol (minutes)
    post_stop_cooldown_min: int = Field(default=20, alias="POST_STOP_COOLDOWN_MIN")
    # Cap concurrent open positions on the $200 book
    max_concurrent_positions: int = Field(default=2, alias="MAX_CONCURRENT_POSITIONS")

    # Hybrid LLM pre-filter (do NOT call LLM every tick)
    use_llm: bool = Field(default=False, alias="USE_LLM")
    prefilter_vwap_boundary_pct: float = Field(default=0.002, alias="PREFILTER_VWAP_BOUNDARY_PCT")
    prefilter_volume_spike_mult: float = Field(default=2.75, alias="PREFILTER_VOLUME_SPIKE_MULT")

    # Multi-timeframe
    htf_timeframe: str = Field(default="1Hour", alias="HTF_TIMEFRAME")
    htf_lookback_bars: int = Field(default=250, alias="HTF_LOOKBACK_BARS")
    entry_timeframe_5m: str = Field(default="5Min", alias="ENTRY_TIMEFRAME_5M")

    # --- Intelligence upgrades (L2 / regime / MTF / ATR sizing) ---
    # 1) L2 order book depth imbalance filter (bid/ask vol within band of mid)
    l2_imbalance_enabled: bool = Field(default=True, alias="L2_IMBALANCE_ENABLED")
    l2_imbalance_band_pct: float = Field(default=0.005, alias="L2_IMBALANCE_BAND_PCT")
    l2_imbalance_min_ratio: float = Field(default=1.2, alias="L2_IMBALANCE_MIN_RATIO")
    # 2) Market regime: ADX + Choppiness on entry timeframe
    regime_filter_enabled: bool = Field(default=True, alias="REGIME_FILTER_ENABLED")
    adx_period: int = Field(default=14, alias="ADX_PERIOD")
    adx_min: float = Field(default=25.0, alias="ADX_MIN")
    chop_period: int = Field(default=14, alias="CHOP_PERIOD")
    chop_max: float = Field(default=60.0, alias="CHOP_MAX")  # Al: CI > 60 blocks BUY
    # 3) Multi-timeframe trend alignment (price > 1h EMA200 AND 4h EMA200)
    mtf_align_enabled: bool = Field(default=True, alias="MTF_ALIGN_ENABLED")
    htf_ema_period: int = Field(default=200, alias="HTF_EMA_PERIOD")
    htf_4h_timeframe: str = Field(default="4Hour", alias="HTF_4H_TIMEFRAME")
    htf_cache_seconds: float = Field(default=300.0, alias="HTF_CACHE_SECONDS")
    # 4) Dynamic ATR position sizing
    # Formula: notional = base_notional * (price * ATR_REF_PCT / ATR)
    # clipped to [MIN_NOTIONAL_USD, MAX_NOTIONAL_PER_TRADE_USD] — hard caps unchanged
    atr_sizing_enabled: bool = Field(default=True, alias="ATR_SIZING_ENABLED")
    atr_sizing_period: int = Field(default=14, alias="ATR_SIZING_PERIOD")
    atr_ref_pct: float = Field(default=0.01, alias="ATR_REF_PCT")

    # --- Lead-lag / funding / optimizer / failure blacklist (PAPER intel v2) ---
    perp_leadlag_enabled: bool = Field(default=True, alias="PERP_LEADLAG_ENABLED")
    perp_sweep_mult: float = Field(default=3.0, alias="PERP_SWEEP_MULT")
    perp_sweep_window_sec: float = Field(default=5.0, alias="PERP_SWEEP_WINDOW_SEC")
    perp_leadlag_venues: str = Field(default="binance,bybit", alias="PERP_LEADLAG_VENUES")
    perp_liq_window_sec: float = Field(default=10.0, alias="PERP_LIQ_WINDOW_SEC")
    perp_liq_min_cluster: int = Field(default=5, alias="PERP_LIQ_MIN_CLUSTER")
    perp_signal_ttl_sec: float = Field(default=30.0, alias="PERP_SIGNAL_TTL_SEC")

    funding_oi_enabled: bool = Field(default=True, alias="FUNDING_OI_ENABLED")
    funding_oi_poll_seconds: float = Field(default=60.0, alias="FUNDING_OI_POLL_SECONDS")
    funding_block_threshold: float = Field(default=0.0003, alias="FUNDING_BLOCK_THRESHOLD")
    funding_boost_threshold: float = Field(default=-0.0001, alias="FUNDING_BOOST_THRESHOLD")
    funding_oi_surge_pct: float = Field(default=0.02, alias="FUNDING_OI_SURGE_PCT")
    funding_stagnant_bars: int = Field(default=3, alias="FUNDING_STAGNANT_BARS")

    optimizer_enabled: bool = Field(default=True, alias="OPTIMIZER_ENABLED")
    optimizer_lookback_days: int = Field(default=14, alias="OPTIMIZER_LOOKBACK_DAYS")
    active_params_path: str = Field(
        default=str(PROJECT_ROOT / "data" / "active_params.json"),
        alias="ACTIVE_PARAMS_PATH",
    )

    failure_blacklist_enabled: bool = Field(default=True, alias="FAILURE_BLACKLIST_ENABLED")
    failure_lookback_hours: float = Field(default=72.0, alias="FAILURE_LOOKBACK_HOURS")
    failure_block_minutes: float = Field(default=45.0, alias="FAILURE_BLOCK_MINUTES")

    # --- PAPER intel v3: on-chain / reslice / cointegration / sweep-fade ---
    onchain_guards_enabled: bool = Field(default=True, alias="ONCHAIN_GUARDS_ENABLED")
    onchain_poll_seconds: float = Field(default=120.0, alias="ONCHAIN_POLL_SECONDS")
    onchain_cache_ttl_sec: float = Field(default=180.0, alias="ONCHAIN_CACHE_TTL_SEC")
    onchain_inflow_spike_mult: float = Field(default=2.5, alias="ONCHAIN_INFLOW_SPIKE_MULT")
    stablecoin_mint_boost: float = Field(default=3.0, alias="STABLECOIN_MINT_BOOST")
    onchain_flow_url: str = Field(default="", alias="ONCHAIN_FLOW_URL")
    onchain_stable_url: str = Field(default="", alias="ONCHAIN_STABLE_URL")
    onchain_api_key: str = Field(default="", alias="ONCHAIN_API_KEY")
    onchain_mock_mode: bool = Field(default=False, alias="ONCHAIN_MOCK_MODE")

    order_reslice_enabled: bool = Field(default=True, alias="ORDER_RESLICE_ENABLED")
    order_reslice_stall_sec: float = Field(default=10.0, alias="ORDER_RESLICE_STALL_SEC")
    order_reslice_max_times: int = Field(default=3, alias="ORDER_RESLICE_MAX_TIMES")

    cointegration_enabled: bool = Field(default=True, alias="COINTEGRATION_ENABLED")
    coint_z_entry: float = Field(default=2.0, alias="COINT_Z_ENTRY")
    coint_pairs: str = Field(
        default="SOL-USD/AVAX-USD,ETH-USD/LINK-USD,BTC-USD/ETH-USD",
        alias="COINT_PAIRS",
    )
    coint_window: int = Field(default=96, alias="COINT_WINDOW")
    coint_min_corr: float = Field(default=0.5, alias="COINT_MIN_CORR")

    sweep_fade_enabled: bool = Field(default=True, alias="SWEEP_FADE_ENABLED")
    sweep_fade_lookback: int = Field(default=50, alias="SWEEP_FADE_LOOKBACK")
    sweep_fade_absorption_ratio: float = Field(
        default=1.5, alias="SWEEP_FADE_ABSORPTION_RATIO"
    )

    # Macro event pause
    macro_pause_enabled: bool = Field(default=True, alias="MACRO_PAUSE_ENABLED")
    macro_pause_minutes: int = Field(default=15, alias="MACRO_PAUSE_MINUTES")
    macro_calendar_url: str = Field(default="", alias="MACRO_CALENDAR_URL")
    macro_calendar_api_key: str = Field(default="", alias="MACRO_CALENDAR_API_KEY")

    # Anti-revenge lockout + trade memory
    revenge_lockout_minutes: int = Field(default=30, alias="REVENGE_LOCKOUT_MINUTES")
    revenge_stop_count: int = Field(default=2, alias="REVENGE_STOP_COUNT")
    trade_memory_size: int = Field(default=5, alias="TRADE_MEMORY_SIZE")

    # Notifier (Discord / Telegram) — empty = no-op
    discord_webhook_url: str = Field(default="", alias="DISCORD_WEBHOOK_URL")
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")
    telegram_commands_enabled: bool = Field(
        default=True, alias="TELEGRAM_COMMANDS_ENABLED"
    )
    # Tick/L2 heartbeat: reconnect if market-data fetch age exceeds this (seconds)
    stale_tick_seconds: float = Field(default=15.0, alias="STALE_TICK_SECONDS")

    # Kill switch
    liquidate_on_kill: bool = Field(default=False, alias="LIQUIDATE_ON_KILL")

    # Persistence
    sqlite_path: str = Field(
        default=str(PROJECT_ROOT / "data" / "trading_bot.db"),
        alias="SQLITE_PATH",
    )
    postgres_dsn: str = Field(default="", alias="POSTGRES_DSN")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # Stabilization: memory/stream maintenance + SQLite backups
    maintenance_interval_hours: float = Field(
        default=6.0, alias="MAINTENANCE_INTERVAL_HOURS"
    )
    sqlite_backup_keep: int = Field(default=7, alias="SQLITE_BACKUP_KEEP")
    sqlite_backup_dir: str = Field(
        default=str(PROJECT_ROOT / "data" / "backups"),
        alias="SQLITE_BACKUP_DIR",
    )

    @field_validator("max_risk_per_trade_pct")
    @classmethod
    def clamp_risk(cls, v: float) -> float:
        if v < 0.01:
            return 0.01
        if v > 0.02:
            return 0.02
        return v

    @staticmethod
    def normalize_symbol(symbol: str) -> str:
        return symbol.strip().upper().replace("/", "-").replace("_", "-")

    def is_allowlisted(self, symbol: str) -> bool:
        return self.normalize_symbol(symbol) in HARD_SYMBOL_ALLOWLIST

    @property
    def symbol_list(self) -> List[str]:
        """Symbols to trade — hard-intersected with HARD_SYMBOL_ALLOWLIST."""
        requested = [self.normalize_symbol(s) for s in self.symbols.split(",") if s.strip()]
        allowed = [s for s in requested if s in HARD_SYMBOL_ALLOWLIST]
        rejected = [s for s in requested if s not in HARD_SYMBOL_ALLOWLIST]
        if rejected:
            # Import-safe: avoid circular logging; stderr via print is fine for config load
            import logging

            logging.getLogger(__name__).warning(
                "Rejecting non-allowlist symbols (ignored): %s", ",".join(rejected)
            )
        # Preserve allowlist order for stable scans when request empty after filter
        if not allowed:
            return list(HARD_SYMBOL_ALLOWLIST)
        # De-dupe preserving order
        out: List[str] = []
        for s in allowed:
            if s not in out:
                out.append(s)
        return out

    @property
    def required_min_tp_pct(self) -> float:
        """Stricter of MIN_TP_PCT and fee-to-target floor."""
        from trading_bot.structural_guardrails import required_min_tp_pct as _req

        return _req(self.min_tp_pct, self.maker_fee_rate, self.fee_to_target_mult)

    @property
    def is_sweet_spot(self) -> bool:
        return (self.strategy_mode or "").strip().lower() in (
            "volume_sweet_spot",
            "sweet_spot",
            "vss",
        )

    @property
    def effective_broker(self) -> str:
        if self.dry_run:
            return "mock"
        return (self.broker or "coinbase").lower()


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()
