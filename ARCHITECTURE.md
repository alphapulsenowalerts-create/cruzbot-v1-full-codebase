# Architecture

## Directory tree

```
agentic-day-trading-bot/
├── README.md
├── ARCHITECTURE.md
├── requirements.txt
├── pyproject.toml
├── .env.example
├── main.py                 # entry: config, loop, kill-switch, broker factory
├── data/                   # SQLite DB (gitignored contents)
├── tests/
│   ├── test_risk_manager.py
│   ├── test_agent_decision.py
│   ├── test_advanced_guardrails.py
│   ├── test_smoke.py
│   └── test_coinbase_broker.py
└── trading_bot/
    ├── __init__.py
    ├── config.py           # pydantic-settings from env
    ├── models.py           # Decision, Order*, Bar, Quote, HTF, TradeMemory, …
    ├── data_feed.py        # 1m (+5m) bars → indicators; 1h HTF EMA200/S/R
    ├── agent_core.py       # hybrid pre-filter → rules | LLM on setup only
    ├── risk_manager.py     # sizing, 2% cap, 3% DD breaker
    ├── executor.py         # limit submit, slippage, idempotency
    ├── logger.py           # console + SQLite (+ optional Postgres)
    ├── macro_calendar.py   # pluggable econ calendar; pause ±N min major news
    ├── state_store.py      # revenge lockouts + rolling trade memory (SQLite)
    ├── notifier.py         # Discord/Telegram webhooks (asyncio httpx; no-op if unset)
    ├── brokers/
    │   ├── base.py         # BrokerAdapter ABC
    │   ├── alpaca.py       # REST + WS (US stocks paper/live)
    │   ├── coinbase.py     # Advanced Trade spot (SDK / JWT; paper-gated)
    │   ├── ib_stub.py      # interface only
    │   └── mock.py         # dry-run
    └── utils/
        ├── retry.py        # exponential backoff / 429
        └── indicators.py   # VWAP, RSI, MACD, EMA, ATR, EMA200 slope, swings
```

## Dependency map (who imports whom)

```
main.py
  ├─ config.Settings
  ├─ logger.setup_logging, TradeLogger
  ├─ brokers.{mock,alpaca,coinbase,ib_stub}  →  brokers.base, models, utils.retry
  ├─ data_feed.DataFeed             →  brokers.base, models, utils.indicators, utils.retry
  ├─ agent_core.AgentCore           →  models (SetupPreFilter gate)
  ├─ risk_manager.RiskManager       →  config, models
  ├─ executor.Executor              →  brokers.base, config, models, logger, notifier?
  ├─ macro_calendar.MacroGuard      →  models (pluggable CalendarAdapter)
  ├─ state_store.BehavioralStateStore → models (SQLite lockouts + memory)
  └─ notifier.Notifier              → httpx (optional)

data_feed ──► brokers.base, utils.indicators, utils.retry, models, config
agent_core ──► models
risk_manager ──► models, config
executor ──► brokers.base, models, config, logger
macro_calendar ──► models (+ httpx for HttpCalendarAdapter)
state_store ──► models (sqlite3)
notifier ──► httpx
logger ──► models
brokers.* ──► brokers.base, config, models, utils.retry
utils.indicators ──► pandas / numpy (/ pandas-ta optional)
```

## Broker selection

`BROKER=alpaca|coinbase|mock|ib` (CLI `--dry-run` forces `mock`).

- **Alpaca** — best for US equity **paper** (`paper-api.alpaca.markets`).
- **Coinbase Advanced Trade** — crypto spot for Al’s path; when `PAPER_TRADING_MODE=true`, `submit_order` / cancel / liquidate never hit live create endpoints (simulated fills).
- **Robinhood** — not integrated.

## Runtime flow

```
SIG handlers ──► notify kill-switch ──► cancel orders [+ optional liquidate] ──► disconnect

loop:
  for symbol in SYMBOLS:
    account ← broker
    risk.update_drawdown (trip 3% → lock day + notify)
    if macro.pause (±MACRO_PAUSE_MINUTES around NFP/CPI/FOMC/…) → HOLD + notify
    if revenge_lockout(symbol) → HOLD
    indicators ← feed.ensure_fresh (1m) + ensure_htf (1h EMA200/S/R) + optional 5m
    memory ← last 5 trades (SQLite)
    decision ← agent.decide:
        PREFILTER_SKIP → HOLD (no LLM)
        PREFILTER_PASS → rules engine  OR  LLM_INVOKED (only if USE_LLM + hook)
    if not HOLD and risk approves:
      result ← executor.submit
      on fill → notifier + trade_memory; stop_loss → consecutive-stop / lockout
    persist events → SQLite
  sleep AGENT_POLL_SECONDS
```

## Hybrid execution (req 6)

Deterministic `SetupPreFilter`: price within `PREFILTER_VWAP_BOUNDARY_PCT` of VWAP **and** `volume_ratio >= PREFILTER_VOLUME_SPIKE_MULT` (default 2×). Logs `PREFILTER_PASS` / `PREFILTER_SKIP` / `LLM_INVOKED`. LLM is never called every tick.

## Extensibility

- **New broker**: implement `BrokerAdapter` in `brokers/`, select via `BROKER=…`
- **LLM agent**: implement `LLMHook.complete`, set `USE_LLM=true` and pass hook to `AgentCore`
- **Calendar**: inject `CalendarAdapter` into `MacroGuard` / `build_macro_guard`
- **Indicators**: extend `utils/indicators.compute_indicators`


## Al locked production controls (Video 2 / handoff)

- **Hard allowlist** (CLI/env cannot add others): BTC-USD, ETH-USD, SOL-USD, XRP-USD, LINK-USD, AVAX-USD, SUI-USD, ADA-USD
- **Absolute caps**: $50 max notional per trade; $200 max total live exposure; `ACCOUNT_EQUITY` default $200
- **Stops**: SL 1.0× ATR, TP 1.5× ATR, trailing 1.5× ATR
- **Behavioral**: 2 consecutive stop-outs → 30m coin lockout; 3% daily DD → day kill; ±15m macro freeze plumbing
- **Sizing**: fractional crypto via notional/base_size + `QTY_PRECISION` (no whole-coin `int()` rounding)
- **Notifier**: quiet mode — fills, stop/TP, daily P&L digest; routine macro/scan chatter gated
- **Paper default**: `PAPER_TRADING_MODE=true`; live only via explicit env/`--live`
