# MASTER_SYSTEM_ARCHIVE.md

> **Product (customer-facing):** Apex Signals Now  
> **GitHub path:** `instances/kraken-instance-2/`  
> **Internal checkout:** `cruzbot_instance_2` (Kraken paper Instance #2)  
> **Purpose:** Self-contained cold-start archive so a new LLM/operator can reconstruct behavior from real code.  
> **CRITICAL:** Never put real API keys, Telegram tokens, passwords, or secrets in this file. Use placeholders only.  
> **Tier-1 lock date:** 2026-09-20 (CT) — Winning Formula institutional stack below is authoritative.

---

## 0. TIER-1 LOCKED STACK (CURRENT — READ FIRST)

When `WINNING_FORMULA=true` (default ops mode), these knobs are **locked in code** (`execute_set_winning_formula` + `main.py` hard-bracket constants). Do not invent older fee/threshold values.

| Knob | Value | Where |
|------|-------|--------|
| **Fees** | **0.80% taker / 0.40% maker** (RT **1.20%**) | `TAKER_FEE_RATE=0.008`, `MAKER_FEE_RATE=0.004` |
| **HWM fee floor** | Peak gross UPL **≥ +1.20%** → SL floor **+1.25%** (never trail below) | `_HWM_PEAK_ARM_PCT=0.012`, `_HWM_FLOOR_PCT=0.0125`; `TRAIL_FEE_BUFFER_PCT=0.0125`, `ELITE_FEE_LOCK_ARM_PCT=0.012` |
| **Time exit** | Maker limit **only if gross ≥ +1.25%** at `entry×1.0125`; wait **5 min** | `_TIME_EXIT_FEE_CUSHION_PCT=0.0125`, `_TIME_EXIT_MAKER_WAIT_SEC=300` |
| **Partials** | **`TP1_FRACTION=0`** — full exits only on ~$500 tickets | WF apply + hard brackets |
| **Entry threshold** | **BULL 60% / BEAR 65%** | WF sets `ENTRY_THRESHOLD=60`; `_bear_spot_long_threshold` floors BEAR to 65 |
| **Profit-runner trail** | Arm at **≥ +2.50%** UPL **or 75%** progress; trail **1.00%** behind peak | `_TRAIL_RUNNER_ARM_PCT=0.025`, `_TRAIL_RUNNER_PROGRESS=75`, `_TRAIL_RUNNER_OFFSET_PCT=0.01` |
| **Circuit breaker** | **3** consecutive losses → pause + **~45m** gated auto-resume; `/circuity_breaker_manually on\|off\|status` | `CIRCUIT_BREAKER_ENABLED`; OpsState CB |
| **Weekly Digest 101** | `/weekly_digest_101 paper\|live` — paper expectancy wipeable; **live never wiped**; **return-only** (no double Telegram send) | `main._cmd_weekly_digest_101` |
| **Wallet B4** | Shown on `/status` as `💳 Wallet B4=$…` | paper `broker.paper_wallet_b4()` or live bankroll |
| **Live gate** | Paper-first; live only `/mode live` then `/confirm_live` (+ `PAPER_TRADING_MODE=false` + `python main.py --live`) | Telegram + CLI |
| **Product name** | Customer-facing **Apex Signals Now**; internal path `cruzbot_instance_2` | `/status` header |

**WF BEAR SL clamp (unchanged):** max SL **−1.25%** and **≤ $6 risk on $500** ref notional; TP ≈ `|sl|×1.5 + 0.80%` fee buffer. Max **1** concurrent position in BEAR/chop.

**Activated reply (authoritative numbers):** fees 0.80%/0.40%, HWM +1.20%→+1.25%, time-exit maker ≥+1.25%, thresh **65% BEAR / 60% BULL**, circuit 3 losses · 45m gated auto-resume, TP1=0, post-only maker.

---

## 1. EXECUTIVE ARCHITECTURE OVERVIEW

### What this bot is
Apex Signals Now Instance #2 is a **Python asyncio** paper/live trading engine focused on **Kraken spot** (`BROKER=kraken`), with **Telegram long-poll command control**, strategy mode **`volume_sweet_spot`**, and hard dollar risk caps.

- **Paper (default):** `PAPER_TRADING_MODE=true`. `KrakenBroker` simulates fills locally (`kr-paper-…`); it does **not** call Kraken `AddOrder` / `CancelOrder` / `CancelAll` while paper is on.
- **Live:** Requires `PAPER_TRADING_MODE=false` **and** `python main.py --live`, plus Telegram `/mode live` + `/confirm_live`.
- **Public market data / WS:** Kraken REST + WS ticker used for marks; optional Binance/Bybit lead-lag / CVD / liq feeds for gates.

### Stack (modules)
| Role | Real path |
|------|-----------|
| Entry / loop / hard brackets / Telegram wiring | `main.py` (~6k+ lines) |
| Settings (pydantic-settings from `.env`) | `trading_bot/config.py` |
| Kraken adapter | `trading_bot/brokers/kraken.py` |
| Bars / indicators | `trading_bot/data_feed.py` |
| Strategy | `trading_bot/strategy_volume_sweet_spot.py` + `agent_core.py` |
| BTC regime | `trading_bot/market_regime.py` |
| Macro ATR+ADX regime | `trading_bot/strategy.py` (`TRENDING`/`RANGING`/`HIGH_VOLATILITY`) + `optimizer.py` |
| Risk / sizing / exposure | `trading_bot/risk_manager.py` + `utils/decision_filters.py` |
| Telegram commands | `trading_bot/telegram_commands.py` |
| Smart memory / quick-scalp | `trading_bot/adaptive_scalp.py` |

### Strategy + regimes (as implemented — do not invent names)
1. **Strategy:** `STRATEGY_MODE=volume_sweet_spot` — 5m low-volume retest / volume sweet spot, maker `POST_ONLY`, fee-to-target, structural guardrails.
2. **BTC spot regime** (`market_regime.py`):
   - `BEAR_CHOP` if BTC 15m close < EMA(50)
   - else `BULL_OK`
   - `dump_30m` if BTC dropped > ~1.2% in ~30m → treated as **SHORT bias** with BEAR_CHOP
3. **Macro optimizer regime** (`strategy.normalize_market_regime`): `TRENDING` | `RANGING` | `HIGH_VOLATILITY`  
   - Alias `RANGE` → `RANGING`. There is **no** string `BULL_TREND` in code; bullish BTC state is **`BULL_OK`**.
4. **SHORT bias effects (spot long-only, `ALLOW_PAPER_SHORTS=false`):**
   - Max concurrent positions → **1** (`_effective_max_concurrent`)
   - Spot still allows LONG evaluation (quality gate), not hard freeze
   - Entry threshold floored / raised (see §0 / §3)
   - WF BEAR SL clamp (see §0 / §3)

### Hard risk (code + instance)
- **`MAX_TOTAL_EXPOSURE_USD=3000`** — **fixed dollar cap**, never equity-scaled (`risk_manager.py`).
- **`MAX_NOTIONAL_PER_TRADE_USD`** — per-trade cap from Settings (WF ops often **~$500** tickets; profiles may use 1000). WF BEAR **risk reference** uses **`$500`** notional for ≤`$6` dollar clamp.
- Circuit breaker: **3** consecutive losses if `WINNING_FORMULA` (else **5**) → pause + **~45m** gated auto-resume; Telegram `/circuity_breaker_manually`; clear pause with `/resume`.
- Also: UTC-day **−3%** SQLite PnL circuit in strategy (`check_daily_drawdown_circuit`).

### Key paths (Instance #2)
```
cruzbot_instance_2/
  main.py
  trading_bot/…
  .env                          # secrets live here — never archive values
  data/paper_book_2.json        # paper positions/cash
  data/trading_bot_2.db         # SQLite (instance-isolated)
  data/active_params_2.json     # optimizer knobs
  data/trade_memory.json        # smart-memory outcomes
  data/trades.db                # paper expectancy / blacklist (wipeable)
  data/trades_live.db           # live fills history (never wiped by paper wipe)
```

### Locks / single poller
- **Telegram getUpdates single-poller lock (REAL):**  
  `/tmp/cruzbot_tg_{bot_id}.lock` where `bot_id = token.split(":")[0]`  
  Implemented in `TelegramCommandListener.run()` via `fcntl.flock(LOCK_EX|LOCK_NB)`. If lock held → listener stays **idle**.
- Ops may use PID files such as `/tmp/cruzbot2_main.pid`; do not invent a paper flock that isn’t in code.

### GOTCHA — shell env overrides `.env`
`Settings` uses pydantic-settings with `env_file=.env`. **Process environment variables override `.env` file values.**  
WF `execute_set_winning_formula` and bare `/winning_formula` (while ON) explicitly force `os.environ["ENTRY_THRESHOLD"]="60"` to beat leftovers (e.g. `/aggressive` left `35`).

### Product naming
- Telegram `/status` header: **`Apex Signals Now PAPER|LIVE status`**
- Internal dirs/logs may say `cruzbot_instance_2` / CruzBot — keep customer copy as Apex Signals Now.

---

## 2. COMPLETE TELEGRAM COMMAND REGISTRY

Source of truth: `KNOWN_COMMANDS` + `BOT_COMMAND_SPECS` in `trading_bot/telegram_commands.py`. Wired in `main.py` → `_wire_telegram_commands()`.

### Full command list (core)
| Command | Description |
|---------|-------------|
| `/status` | PAPER/LIVE snapshot (incl. Wallet B4, circuit breaker line) |
| `/pause` | Skip new buys |
| `/resume` | Re-enable new buys |
| `/pnl` | Day P&L report |
| `/kill` | Flatten + stop loop |
| `/mode` | Show/switch PAPER/LIVE |
| `/confirm_live` | Confirm LIVE switch |
| `/set_limit` | Update size caps |
| `/set_threshold` / `/set_threshold_custom` | Entry threshold 15–95 |
| `/set_spread` | Max bid-ask spread % |
| `/tod_custom` | TOD gate on/off |
| `/stop_loss` | SL profile tight/medium/free |
| `/winning_formula` | Winning formula on/off/status |
| `/circuity_breaker_manually` | CB on/off/status (3-loss / 45m) |
| `/weekly_digest_101` | 7-day expectancy `paper\|live` |
| `/aggressive` `/medium` `/low` `/profile` | Trade profiles |
| `/test_trade` | Paper ~$100 BUY |
| `/reset_paper` | Wipe paper book |
| `/wipe_paper` / `/factory_reset` | Full paper scratch |
| `/set` | Set knobs / profile alias |
| `/ping` `/positions` `/balance` `/history` | Ops views |
| `/grok` `/regime` `/logs` | Info |
| `/universe` `/universe_all` `/universe_stocks` `/symbols` | Universe |
| `/close` `/clear_positions` | Manual exits |
| `/help` | Command list |

Listener: only messages from `TELEGRAM_CHAT_ID`; long-poll `getUpdates`; `setMyCommands` registers menu.

---

### Deep dive: `/status`
Built by `format_status_reply()` — **layout order (do not reorder):**

1. `Apex Signals Now {PAPER|LIVE} status`
2. blank
3. `Last Scan Latency: {ms} across {N} pairs` (if available)
4. `last_tick_age=…`
5. blank
6. `(⚠️☣️circuit breaker ☣️⚠️) ON|OFF · N consecutive loss(es)`
7. blank ×2
8. `cash=$…` / `equity=$…` / **`💳 Wallet B4=$…`** / `WR …% · nW/nL` (paper)
9. `pause=PAUSED…` **only if paused**
10. blank
11. `Market: …` (+ `bias=SHORT` when BEAR/dump)
12. `caps=$X/trade $Y exposure`
13. blank
14. Profile block: `winning_formula` / Profile / tod_custom / stop_loss / Threshold / Spread
15. blank
16. `Target Setup: LONG (Spot Mode)` | `WAIT`
17. `Entry Proximity: [bar] score%`
18. `focus=SYM @ $px` optional `[BLOCKED: …]`
19. blank
20. `positions: (none)` or per-position blocks
21. blank
22. `Universe: …` + Symbols expand footer

---

### Deep dive: `/winning_formula [on|off|status]`
**Persistence keys when ON:** `WINNING_FORMULA=true`, `ENTRY_THRESHOLD=60`, `MAX_CONCURRENT_POSITIONS=3`, `MIN_TP_PCT≈0.0305`, `TRAIL_FEE_BUFFER_PCT=0.0125`, `ELITE_FEE_LOCK_ARM_PCT=0.012`, `TP1_FRACTION=0`, `TAKER_FEE_RATE=0.008`, `MAKER_FEE_RATE=0.004`, `CIRCUIT_BREAKER_ENABLED=true`, and forces `/stop_loss medium`.

**Core apply** (`execute_set_winning_formula`):

```python
# trading_bot/telegram_commands.py — Tier-1 (Sep 2026)
object.__setattr__(settings, "entry_threshold", 60.0)
os.environ["ENTRY_THRESHOLD"] = "60"
object.__setattr__(settings, "max_concurrent_positions", 3)  # BEAR → 1 live
object.__setattr__(settings, "min_tp_pct", 0.0305)
object.__setattr__(settings, "trail_fee_buffer_pct", 0.0125)   # +1.25% floor
object.__setattr__(settings, "elite_fee_lock_arm_pct", 0.012)  # arm +1.20%
object.__setattr__(settings, "tp1_fraction", 0.0)
object.__setattr__(settings, "taker_fee_rate", 0.008)          # 0.80%
object.__setattr__(settings, "maker_fee_rate", 0.004)          # 0.40%
object.__setattr__(settings, "circuit_breaker_enabled", True)
execute_set_stop_loss(settings, "medium", …)
```

**Runtime effects (`main.py`):**
- Auto SL → MEDIUM; BEAR clamp **max −1.25%** and **≤$6 on $500**
- Threshold floors: **60% BULL / 65% BEAR**
- HWM: peak ≥ **+1.20%** → SL floor **+1.25%**
- Time-exit maker only if gross ≥ **+1.25%**; 5m wait
- Profit-runner trail: ≥ **+2.50%** or **75%** progress, **1.00%** offset
- Full exits (`TP1=0`); circuit **3** losses / **45m** gated auto-resume
- Max **1** position in BEAR/chop
- Bare `/winning_formula` while ON **re-applies** full formula + rebracket

**Status / activated lines** document Tier-1 fees, HWM, time-exit, thresh, circuit (see §0). Note: one activated-string variant may still say “50% BULL” in an older format helper — **runtime + status helper use 60%**; prefer `/status` Threshold and env `ENTRY_THRESHOLD=60`.

Boot: `_enforce_winning_formula_sl()` forces MEDIUM if WF already true; WF path also re-asserts HWM/trail/time-exit constants.

---

### Deep dive: `/circuity_breaker_manually [on|off|status]`
- **on:** enable CB (`CIRCUIT_BREAKER_ENABLED=true`); if already at ≥limit losses, pause now + arm ~45m gated auto-resume
- **off:** disable auto-pause (losses still counted); clear CB auto-resume arm; `/resume` if paused
- **status:** `circuity_breaker_manually: ON|OFF · N consecutive losses · …`
- Gated auto-resume: cooldown elapsed **and** (win since trip **or** `BULL_OK`)

---

### Deep dive: `/weekly_digest_101 [paper|live]`
- **paper:** 7-day expectancy from wipeable paper DBs (`trades.db` / `trading_bot_2.db` memory). Cleared by `/wipe_paper`.
- **live:** real fills via `broker.fetch_live_trades_history` / `trades_live.db` — **never wiped** by paper factory wipe.
- Handler **returns body only** — TelegramCommandListener already replies; do **not** also `notifier.send` (no double send).

---

### Deep dive: `/stop_loss tight|medium|free`
**Presets (REAL):**

```python
STOP_LOSS_PRESETS = {
    "tight":  {"sl_pct": 0.0075, "tp_pct": 0.0115, "atr_mult": 1.0, "emoji": "🔴", "label": "TIGHT"},
    "medium": {"sl_pct": 0.015,  "tp_pct": 0.0225, "atr_mult": 1.5, "emoji": "🟡", "label": "MEDIUM"},
    "free":   {"sl_pct": 0.025,  "tp_pct": 0.0375, "atr_mult": 2.5, "emoji": "🟢", "label": "FREE"},
}
```

Retroactive rebracket via `main._cmd_stop_loss` → `_apply_profile_brackets`.

---

### Deep dive: `/resume` (and `/pause`)
```python
async def _cmd_pause(...):
    self.ops.set_pause(True)
    return "Paused: new buys skipped; exits/brackets still managed."

async def _cmd_resume(...):
    self.ops.set_pause(False)
    return "Resumed: new buys enabled."
```
Use `/resume` after circuit-breaker pause. Exits keep running while paused.

---

### Profile presets (REAL)
```python
TRADE_PROFILE_PRESETS = {
  "aggressive": {entry_threshold:35, max_spread_pct:0.005, disable_tod_gate:True,
                 rvol_breakout_mult:1.0, max_concurrent_positions:3,
                 agent_poll_seconds:12, max_notional_per_trade_usd:1000,
                 max_total_exposure_usd:3000},
  "medium":     {entry_threshold:65, max_spread_pct:0.0025, disable_tod_gate:False,
                 rvol_breakout_mult:2.0, max_concurrent_positions:2,
                 agent_poll_seconds:30, caps 1000/3000},
  "low":        {entry_threshold:80, max_spread_pct:0.0015, disable_tod_gate:False,
                 rvol_breakout_mult:2.5, max_concurrent_positions:2,
                 agent_poll_seconds:60, caps 1000/3000},
}
```
Note: `/aggressive` **turns WF off** (`WINNING_FORMULA=false`).

---

## 3. STRATEGY, REGIME & GUARDRAIL SPECIFICATIONS

### Fee-aware trailing / HWM / fee_lock (Tier-1)
Constants (`main.py` + `utils/decision_filters.py`):
- Tier-1 RT hurdle **1.20%** (0.80% + 0.40%); buffer/floor **1.25%**
- `DEFAULT_TRAIL_FEE_BUFFER_PCT = 0.0125`
- WF sets `trail_fee_buffer_pct=0.0125`, `elite_fee_lock_arm_pct=0.012`

```python
# HWM (main._check_hard_brackets)
_HWM_PEAK_ARM_PCT = 0.012   # peak ≥ +1.20%
_HWM_FLOOR_PCT = 0.0125     # SL floor +1.25%
_HWM_PROGRESS_ARM = 60.0

# Profit-runner (tight trail delayed)
_TRAIL_RUNNER_ARM_PCT = 0.025      # +2.50%
_TRAIL_RUNNER_PROGRESS = 75.0
_TRAIL_RUNNER_OFFSET_PCT = 0.01    # 1% behind peak
```

`maybe_fee_lock_sl`: arm when UPL ≥ buffer, progress ≥ 60%, or peak ≥ +1.20%; floor never trails below +1.25%.

### TIME_EXIT maker (Tier-1)
When `max_hold_minutes` exceeded:
- Maker time-exit **only if** gross UPL **≥ +1.25%** (`_TIME_EXIT_FEE_CUSHION_PCT`)
- Limit at `entry * 1.0125`; wait **300s (5 min)**
- Reasons: `TIME_EXIT_MAKER_BE` / `TIME_EXIT_MAKER_TIMEOUT_SL` / wait logs

### BEAR / SHORT bias threshold floors
```python
def _bear_spot_long_threshold(self, base: float) -> float:
    if spot_long_only and short_bias:
        floor = 65.0 if winning_formula else 50.0
        return max(b * 1.10, floor)
    if winning_formula:
        return max(b, 50.0)  # WF sets base 60 → effective BULL 60%
    return b
```

### WF BEAR SL clamp
```python
_WF_BEAR_SL_MAX = 0.0125          # −1.25%
_WF_BEAR_MAX_RISK_USD = 6.0
_WF_BEAR_REF_NOTIONAL = 500.0
# tp ≈ |sl| * 1.5 + 0.008   # 1.5 R:R + 0.80% fee buffer
```

### Circuit breaker
```python
limit = 3 if winning_formula else 5
# trip → ops pause + ~45m gated auto-resume
# /circuity_breaker_manually on|off|status
```
Plus strategy daily DD: day SQLite PnL ≤ −3% of day-start equity → block new entries.

### Exposure / concurrent
- `max_total = settings.max_total_exposure_usd` (**3000** fixed).
- `_effective_max_concurrent()` → **1** if SHORT bias else `max_concurrent_positions` (WF sets 3, BEAR forces 1).

### Partial TP disabled under WF
- WF sets `tp1_fraction=0`.
- Hard brackets allow TP1 only if `tp1_fraction > 0` **and** notional > `PARTIAL_TP_MAX_NOTIONAL_USD`. Otherwise full exit.

### BTC regime vs macro regime
| Layer | States | File |
|-------|--------|------|
| BTC 15m EMA | `BEAR_CHOP`, `BULL_OK` (+ `dump_30m`) | `market_regime.py` |
| Optimizer ATR+ADX | `TRENDING`, `RANGING`, `HIGH_VOLATILITY` | `strategy.py` / `optimizer.py` |

---

## 4. FULL CODEBASE BLUEPRINT

### Repo tree (key files under `cruzbot_instance_2`)
```
cruzbot_instance_2/
├── main.py
├── requirements.txt
├── pyproject.toml
├── .env / .env.example / .env.instance2.example
├── MASTER_SYSTEM_ARCHIVE.md / README.md / ARCHITECTURE.md
├── PAPER_RUNBOOK.md / INSTANCE_2_KRAKEN.md / HANDOFF.md
├── EXPORT_SYSTEM_INSTRUCTIONS.md / STATUS.md
├── data/          # runtime only — do not ship DBs in share zips
├── trading_bot/   # package + brokers/ + utils/
├── tests/
├── scripts/
└── deploy/        # systemd units, helpers (no runtime logs)
```

### Critical code blocks
- **STOP_LOSS_PRESETS / execute_set_winning_formula / format_status_reply** — `telegram_commands.py`
- **HWM / profit-runner / time-exit / CB** — `main.py` (`_check_hard_brackets`, `_cmd_circuity_breaker_manually`, `_cmd_weekly_digest_101`)
- **Exposure** — `risk_manager.py` (fixed dollar cap)
- **Telegram lock** — `TelegramCommandListener.run()` flock on `/tmp/cruzbot_tg_{bot_id}.lock`

### Dependencies (`requirements.txt`)
```
aiohttp>=3.9.0
websockets>=12.0
pydantic>=2.5.0
pydantic-settings>=2.1.0
pandas>=2.1.0
numpy>=1.26.0
pandas-ta>=0.3.14b
python-dotenv>=1.0.0
pytest>=7.4.0
pytest-asyncio>=0.23.0
coinbase-advanced-py>=1.8.0
httpx>=0.27.0
```

### Scrubbed `.env` template (Instance #2 — Tier-1)
```bash
# --- Mode ---
PAPER_TRADING_MODE=true
DRY_RUN=false
BROKER=kraken
ACCOUNT_EQUITY=1600
SYMBOLS=BTC-USD,ETH-USD,SOL-USD,XRP-USD,LINK-USD,AVAX-USD,SUI-USD,ADA-USD,DOGE-USD,DOT-USD,ATOM-USD,LTC-USD,UNI-USD,NEAR-USD
SYMBOL_MODE=ALLOWLIST
UNIVERSE_STOCKS=false
ALLOW_PAPER_SHORTS=false

# --- Kraken (PLACEHOLDERS ONLY) ---
KRAKEN_API_KEY=YOUR_KRAKEN_API_KEY
KRAKEN_API_SECRET=YOUR_KRAKEN_API_SECRET
KRAKEN_BASE_URL=https://api.kraken.com
KRAKEN_WS_URL=wss://ws.kraken.com/v2

COINBASE_API_KEY=
COINBASE_API_SECRET=

# --- Isolated Instance #2 paths ---
SQLITE_PATH=data/trading_bot_2.db
PAPER_BOOK_PATH=data/paper_book_2.json
ACTIVE_PARAMS_PATH=data/active_params_2.json
SQLITE_BACKUP_DIR=data/backups_2
TRADES_DB_PATH=data/trades.db

# --- Tier-1 Winning Formula / risk ---
STRATEGY_MODE=volume_sweet_spot
MAX_NOTIONAL_PER_TRADE_USD=500
MAX_TOTAL_EXPOSURE_USD=3000
MAX_CONCURRENT_POSITIONS=3
ENTRY_THRESHOLD=60
TRADE_PROFILE=medium
STOP_LOSS_PROFILE=medium
WINNING_FORMULA=true
TAKER_FEE_RATE=0.008
MAKER_FEE_RATE=0.004
TRAIL_FEE_BUFFER_PCT=0.0125
ELITE_FEE_LOCK_ARM_PCT=0.012
TP1_FRACTION=0
CIRCUIT_BREAKER_ENABLED=true
POST_ONLY=true
ELITE_RISK_ENABLED=true
BTC_REGIME_ENABLED=true
TOD_GATE_ENABLED=true
DISABLE_TOD_GATE=false

# --- Telegram (PLACEHOLDERS) ---
TELEGRAM_BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID=YOUR_TELEGRAM_CHAT_ID
TELEGRAM_COMMANDS_ENABLED=true

# --- Optional LLM / Grok ---
# USE_LLM=false
# XAI_API_KEY=YOUR_XAI_API_KEY

LOG_LEVEL=INFO
```

### How modules connect (runtime)
```
main.TradingApp
  ├─ Settings (.env) ──► RiskManager, Executor, DataFeed, AgentCore
  ├─ KrakenBroker (paper book JSON)
  ├─ BtcRegimeEngine ──► SHORT bias / BEAR_CHOP
  ├─ SymbolUniverse ──► allowlist / discovery / xStocks
  ├─ loop: scan symbols → proximity / strategy → risk → execute
  ├─ _check_hard_brackets (SL/TP/HWM/profit-runner/time-exit)
  └─ TelegramCommandListener (single flock) → _cmd_* handlers
```

---

## OPERATING NOTES (brief)

1. **Duplicate `main.py` PIDs break Telegram** — keep **one** Instance #2 process.
2. **Clear `/tmp/cruzbot_tg_*.lock` on restart** only after confirming no healthy owner PID.
3. **Never leave `ENTRY_THRESHOLD=35` in process env when WF wants 60/65** — shell overrides `.env`.
4. Paper state: `data/paper_book_2.json` — backup before `/wipe_paper`. Live expectancy DB is separate and not wiped.
5. Do not share SQLite/paper book with Instance #1 (Coinbase).
6. Live: `/mode live` → `/confirm_live` only after explicit operator request.

---

## COLD-START REBOOT PROMPT

```
You are rebuilding Apex Signals Now (internal: cruzbot_instance_2) — Kraken paper trading bot.

READ FIRST: MASTER_SYSTEM_ARCHIVE.md §0 Tier-1 locked stack, then real source.
Do not invent features or older fee tables (0.9%/0.5% is obsolete).

CHECKLIST:
1) Tree: main.py, trading_bot/*, requirements.txt, .env.example
2) Scrubbed .env from archive §4:
   - BROKER=kraken, PAPER_TRADING_MODE=true
   - PAPER_BOOK_PATH=data/paper_book_2.json, SQLITE_PATH=data/trading_bot_2.db
   - Placeholders: YOUR_KRAKEN_API_KEY, YOUR_KRAKEN_API_SECRET,
     YOUR_TELEGRAM_BOT_TOKEN, YOUR_TELEGRAM_CHAT_ID
   - Tier-1: WINNING_FORMULA=true, ENTRY_THRESHOLD=60,
     TAKER_FEE_RATE=0.008, MAKER_FEE_RATE=0.004,
     TRAIL_FEE_BUFFER_PCT=0.0125, ELITE_FEE_LOCK_ARM_PCT=0.012,
     TP1_FRACTION=0, CIRCUIT_BREAKER_ENABLED=true,
     MAX_TOTAL_EXPOSURE_USD=3000, STOP_LOSS_PROFILE=medium
3) python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
4) unset ENTRY_THRESHOLD TRADE_PROFILE WINNING_FORMULA STOP_LOSS_PROFILE
5) One main.py only. Stale TG lock: rm -f /tmp/cruzbot_tg_*.lock (after PID check)
6) Paper: python main.py
7) /status → Apex Signals Now PAPER status, Wallet B4, CB line, WF ON, Threshold 60%/65% BEAR
8) Ops: /winning_formula on · /circuity_breaker_manually status · /weekly_digest_101 paper
9) Never commit secrets. Live only via /mode live + /confirm_live.

Reconstruct from telegram_commands.py / main.py / risk_manager.py /
decision_filters.py / market_regime.py / strategy_volume_sweet_spot.py.
```

---

*End of MASTER_SYSTEM_ARCHIVE.md — Tier-1 refresh 2026-09-20 CT from live `cruzbot_instance_2` source. Prefer re-reading code if archive and tree diverge.*
