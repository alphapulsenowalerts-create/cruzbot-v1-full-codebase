# Kraken Instance #2 (Apex Signals Now)

Peer Coinbase / Agentic bots: **this folder is the Instance #2 trading stack.**

| What | Where |
|------|--------|
| Trading code | `instances/kraken-instance-2/` (`main.py`, `trading_bot/`, `scripts/`, `tests/`, `deploy/`) |
| Tier-1 archive | [MASTER_SYSTEM_ARCHIVE.md](MASTER_SYSTEM_ARCHIVE.md) |
| Paper env templates | [`.env.example`](.env.example) and [`.env.instance2.example`](.env.instance2.example) |

**Paper-first.** Copy `.env.example` → `.env` (or `.env.instance2.example` for isolated `*_2` paths). Never commit `.env`, API keys, Telegram tokens, or SQLite DBs. Secrets stay local.

```bash
cd instances/kraken-instance-2
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill placeholders only
python main.py --dry-run --once
```

Do not mix state with repo-root Instance #1 (Coinbase): separate `.env`, `data/trading_bot_2.db`, `data/paper_book_2.json`.

---

# Agentic Day Trading Bot

Production-oriented **paper** day-trading bot for a **$200** portfolio. Asyncio I/O, pydantic schemas, modular broker adapters (Alpaca paper; **Coinbase Advanced Trade** for crypto; IB stub; Mock for dry-run), ReAct-style agent loop, and hard risk controls.

> **Default: `PAPER_TRADING_MODE=True` everywhere.** Do not disable unless you intentionally accept live risk. Coinbase has no true retail paper portfolio — when `PAPER_TRADING_MODE=true`, the Coinbase adapter **never** submits live orders (logs + simulated fills only).

## Strategy: VWAP crypto scalps on Coinbase

Al’s locked focus is **VWAP momentum scalps** on Coinbase spot pairs (default engine: `VwapMomentumScalpEngine`).

- **BUY scalp**: price reclaims/holds **above VWAP** (hard filter — no BUY if close &lt; VWAP) + rising volume (`volume_ratio` &gt; 1.2 when present) + MACD hist positive/flipping + EMA fast &gt; slow (or fresh bullish cross). Prefers momentum continuation over deep RSI oversold mean-reversion.
- **SELL / exit**: price loses VWAP + weak volume or bearish EMA/MACD. Flat-suppression keeps the bot from shorting by default.
- **Bars / loop**: `1Min` bars, `AGENT_POLL_SECONDS=3`, tighter scalp ATR stops (`1.0×` SL / `1.5×` TP), `MAX_POSITION_PCT=0.20`, risk still **1.5% / 2% ceiling** with a **3%** daily drawdown breaker.

Suggested `.env` for Al:

```bash
PAPER_TRADING_MODE=true
BROKER=coinbase
SYMBOLS=BTC-USD,ETH-USD,SOL-USD,XRP-USD,LINK-USD,AVAX-USD,SUI-USD,ADA-USD
BAR_TIMEFRAME=1Min
AGENT_POLL_SECONDS=3
STOP_LOSS_ATR_MULT=1.0
TAKE_PROFIT_ATR_MULT=1.5
MAX_POSITION_PCT=0.20
MAX_RISK_PER_TRADE_PCT=0.015
MAX_RISK_PER_TRADE_PCT_CEILING=0.02
DAILY_DRAWDOWN_LIMIT_PCT=0.03
MAX_NOTIONAL_PER_TRADE_USD=100
MAX_TOTAL_EXPOSURE_USD=1000
ACCOUNT_EQUITY=1600
```

Keep paper mode on until the loop, sizing, and kill-switch are verified end-to-end. Do not place live orders until you intentionally accept that risk.

## Which broker?

| Broker | Best for | Notes |
|--------|----------|--------|
| **Coinbase Advanced Trade** | **Al’s default: crypto VWAP scalps** | Default `BROKER=coinbase`. Use CDP API keys (`COINBASE_API_KEY` / `COINBASE_API_SECRET`). Keep `PAPER_TRADING_MODE=true` until verified. Spot only in this adapter. |
| **Alpaca** | **US stock paper testing** | Best stock paper environment (`paper-api.alpaca.markets`). Set `BROKER=alpaca` when practicing equities. |
| **Robinhood** | **Not suitable** | No proper retail algorithmic/API trading surface for agentic bots. Equities under $25k also face PDT restrictions. Prefer Coinbase (crypto) or Alpaca (stock paper). |
| **Mock** | Offline dry-run / CI | `--dry-run` — no credentials, synthetic bars/fills. |

**Recommended path for Al ($1k book, Coinbase):**

1. Defaults already set: `BROKER=coinbase`, `SYMBOLS=BTC-USD,ETH-USD,SOL-USD,XRP-USD,LINK-USD,AVAX-USD,SUI-USD,ADA-USD`, `PAPER_TRADING_MODE=true`.
2. Run dry cycles (`--dry-run --once`) until you trust logs and risk sizing.
3. Only then consider flipping paper off for tiny live size — never invent or trust fake live P&L from paper sims.

## Features

- **Broker abstraction** — Alpaca REST + WebSocket; Coinbase Advanced Trade (SDK + JWT fallback); Mock for credential-free dry-run; IB stub for future swap
- **Data feed** — OHLCV frames, VWAP / RSI / MACD / EMA / ATR (pandas-ta with pure-pandas fallback), stale-data detection + reconnect backoff
- **Agent** — observe → reason → act; emits only validated JSON decisions; **VWAP momentum scalp** rule engine by default; LLM hook ready
- **Risk** — 1–2% max risk per trade, ATR/stop sizing, **3% daily drawdown circuit breaker**, bracket SL/TP suggestions
- **Executor** — limit orders with slippage protection, idempotent `client_order_id`, partial-fill handling
- **Logging** — structured console + SQLite (`data/trading_bot.db`); optional Postgres DSN
- **Kill-switch** — SIGINT/SIGTERM cancels open orders; optional liquidate via `--liquidate-on-kill`
- **Hybrid pre-filter** — deterministic VWAP-boundary + volume-spike gate; LLM only on setup (`PREFILTER_*`, `USE_LLM`)
- **Multi-timeframe** — 1m/5m entry data + 1h EMA200 slope / swing S/R in observations
- **Macro pause** — pluggable calendar; hold ±15m around NFP/CPI/FOMC (`MACRO_PAUSE_*`)
- **Revenge lockout** — 2 consecutive stop-losses → 30m symbol cooldown; last-5 trade memory in SQLite
- **Notifier** — Discord / Telegram webhooks via asyncio httpx (no-op if unset)

## Install

```bash
cd /workspace/agentic-day-trading-bot
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Copy `.env.example` → `.env` and fill keys (never commit `.env`):

```bash
cp .env.example .env
```

| Variable | Default | Notes |
|----------|---------|--------|
| `PAPER_TRADING_MODE` | `true` | Hard preference for paper; **required** to block live Coinbase submits |
| `ACCOUNT_EQUITY` | `1000` | Starting equity assumption / mock / Coinbase paper book |
| `BROKER` | `coinbase` | `alpaca` \| `coinbase` \| `mock` \| `ib` |
| `SYMBOLS` | `BTC-USD,ETH-USD,SOL-USD,XRP-USD,LINK-USD,AVAX-USD,SUI-USD,ADA-USD` | Coinbase product IDs |
| `MAX_RISK_PER_TRADE_PCT` | `0.015` | Clamped to 1–2% |
| `DAILY_DRAWDOWN_LIMIT_PCT` | `0.03` | Circuit breaker |
| `MAX_POSITION_PCT` | `0.20` | Scalp position cap |
| `STOP_LOSS_ATR_MULT` / `TAKE_PROFIT_ATR_MULT` | `1.0` / `1.5` | Tighter scalp brackets |
| `BAR_TIMEFRAME` / `AGENT_POLL_SECONDS` | `1Min` / `3` | Scalp loop cadence |
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | empty | Paper keys only in `.env` |
| `COINBASE_API_KEY` / `COINBASE_API_SECRET` | empty | CDP key name + private key PEM |
| `LIQUIDATE_ON_KILL` | `false` | Flatten on signal |
| `USE_LLM` / `PREFILTER_*` | `false` / `0.002` + `2.0` | Hybrid gate; LLM only when setup fires |
| `MACRO_PAUSE_ENABLED` / `MACRO_PAUSE_MINUTES` | `true` / `15` | Pause around major news |
| `REVENGE_LOCKOUT_MINUTES` / `REVENGE_STOP_COUNT` | `30` / `2` | Anti-revenge cooldown |
| `DISCORD_WEBHOOK_URL` / `TELEGRAM_*` | empty | Notifier no-op when unset |

## Modes (paper is default)

```bash
# Dry-run (mock broker, no credentials)
python main.py --dry-run --once

# Paper (default) — Coinbase market data allowed; no live orders
PAPER_TRADING_MODE=true python main.py

# Explicit live opt-in (still allowlist + $50/$200 caps)
PAPER_TRADING_MODE=false python main.py --live
```

Hard allowlist only: BTC ETH SOL XRP LINK AVAX SUI ADA (-USD). Non-allowlist holdings are invisible to the executor (never auto-sold).

## Run dry-run (no credentials)

```bash
python main.py --dry-run --once
# or continuous mock loop:
python main.py --dry-run
```

Uses `MockBroker` with synthetic bars/quotes and simulated fills.

## Run Coinbase (crypto, paper-gated)

1. Create a CDP API key at [Coinbase Developer Platform](https://portal.cdp.coinbase.com/)
2. Put `COINBASE_API_KEY` / `COINBASE_API_SECRET` in `.env`
3. Defaults already match Al’s focus (`BROKER=coinbase`, crypto symbols, paper on). Or set explicitly:

```bash
BROKER=coinbase
PAPER_TRADING_MODE=true
SYMBOLS=BTC-USD,ETH-USD,SOL-USD,XRP-USD,LINK-USD,AVAX-USD,SUI-USD,ADA-USD
```

4. Run:

```bash
python main.py --once
python main.py
```

With paper mode on, intended orders are logged and filled in a local simulated book — **no real Advanced Trade submits**.

## Run Alpaca paper (US stocks)

1. Create a paper account at [Alpaca](https://alpaca.markets/)
2. Put keys in `.env`, `BROKER=alpaca`, stock `SYMBOLS`
3. Run:

```bash
python main.py --once          # one cycle
python main.py                 # continuous agent loop
```

## Kill-switch

- **Ctrl+C / SIGTERM** → cancel all open orders, clean asyncio shutdown
- Add `--liquidate-on-kill` (or `LIQUIDATE_ON_KILL=true`) to also flatten positions

## Tests

```bash
python -m pytest -q
```

## Risk defaults (summary)

| Control | Value |
|---------|--------|
| Trading book (ACCOUNT_EQUITY) | $1600 |
| Max notional per trade | $50 |
| Max total live exposure | $200 |
| Risk per trade | 1.5% (ceiling 2%) |
| Daily drawdown lock | 3% |
| Max position notional | 20% of equity |
| Stop / TP | 1.0× / 1.5× ATR |
| Slippage protection | 5 bps on limits |

## Project layout

See [ARCHITECTURE.md](ARCHITECTURE.md) for the dependency map.

## Whop sales webhook

`python deploy/whop_webhook.py` — aiohttp on port **5001**; Telegram alert brand: **Apex Signals Now**. Secrets from env only.
