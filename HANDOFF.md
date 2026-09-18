# CruzBot Handoff — pick up here

**Owner:** Al Gre (America/Chicago)  
**Bot name:** CruzBot / agentic-day-trading-bot  
**Handoff date:** 2026-09-17 (CT)  
**Goal for next agent:** Continue paper trading on Coinbase; only missing piece from a fresh machine is CDP API keys in `.env`.

---

## What this zip is

A complete, locked Python asyncio day-trading bot. Unzip → venv → copy `.env.example` → paste Coinbase CDP keys → run paper. Do **not** reinvent architecture or unlock risk limits unless Al explicitly changes them.

---

## Locked parameters (DO NOT loosen without Al)

| Setting | Value |
|--------|--------|
| Broker | Coinbase Advanced Trade (CDP) |
| Mode default | `PAPER_TRADING_MODE=true` |
| Live arming | Requires `PAPER_TRADING_MODE=false` **and** `python main.py --live` |
| Hard allowlist ONLY | `BTC-USD, ETH-USD, SOL-USD, XRP-USD, LINK-USD, AVAX-USD, SUI-USD, ADA-USD` |
| Never sell / never trade | All other Coinbase holdings (QNT, PUMP, PNUT, DEGEN, ATOM, etc.) |
| Trading book / exposure cap | `$200` total |
| Max per trade | `$50` notional |
| Stop loss | `1.0 × ATR` |
| Take profit | `1.5 × ATR` |
| Trailing stop | `1.5 × ATR` |
| Revenge lockout | 2 stop-outs → 30 min symbol lock |
| Daily kill | `3%` drawdown → halt for day |
| Macro freeze | 15 min around major events (needs calendar URL; optional) |
| Strategy | VWAP / volume-spike scalps; hybrid LLM prefilter optional (off by default) |
| Alerts | Quiet Chief-of-Finance mode: fills, stops/TPs, daily P&L only |

Code enforces allowlist + `$50`/`$200` in `trading_bot/config.py`, `risk_manager.py`, `executor.py`, and Coinbase liquidate paths.

---

## Account snapshot (as of handoff)

- Coinbase app total ≈ **$6,276.51** (crypto ≈ $4,912.77 + cash ≈ **$1,363.74**)
- CDP API sees Default portfolio cash **$1,363.74 USD**
- Bot allowlist holdings ≈ **$0** (buys use USD cash)
- Non-allowlist bags must remain untouched

### CDP key setup (already done once on prior machine)

- Portal: https://portal.cdp.coinbase.com/ (personal login, not Business/EIN)
- Nickname: CruzBot
- Permissions: **View + Trade**; Transfer off
- Algorithm: **ECDSA**
- Opt out of IP allowlisting
- `COINBASE_API_KEY` = full `organizations/.../apiKeys/...` path  
- `COINBASE_API_SECRET` = full PEM (`BEGIN EC PRIVATE KEY` … `END`)

**Do not ask Al to paste keys in chat** — use secure secret cards / local `.env` only.

---

## Critical bugfix included

Coinbase `get_accounts()` without `limit=250` **omits the USD cash wallet** (returns ~49 accounts). This build calls `get_accounts(limit=250)`. If paper says cash=$0 but the app shows cash, check this first.

---

## Paper verification already done

1. Auth OK with CDP keys  
2. Paper scan (`main.py --once`): all 8 pairs live-priced; often HOLD when not near VWAP  
3. Forced plumbing round-trip (not a strategy signal):
   - BUY XRP-USD ~37.96 @ $1.3191 (~$50) → `cb-paper-1269e25b597a`
   - SELL XRP-USD @ $1.3196 → `cb-paper-3ff09442bd19`
   - Realized ≈ **+$0.02**  
4. Natural quiet scan: HOLD across allowlist when off VWAP

Coinbase has **no true retail paper account**. Paper mode simulates fills locally while reading live market data.

---

## Fresh machine — only steps needed

```bash
unzip CruzBot-handoff.zip
cd agentic-day-trading-bot
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env — set ONLY:
#   COINBASE_API_KEY=organizations/.../apiKeys/...
#   COINBASE_API_SECRET=-----BEGIN EC PRIVATE KEY-----\n...\n-----END EC PRIVATE KEY-----\n
# Leave PAPER_TRADING_MODE=true
python main.py --dry-run --once    # mock, no keys
python main.py --once              # Coinbase paper cycle
python main.py                     # paper loop (Ctrl+C = kill switch)
```

Live later (only if Al explicitly says go live):

```bash
# still keep tiny caps
PAPER_TRADING_MODE=false python main.py --live
```

---

## Project map

```
main.py                 # entry, --once/--live/--dry-run, kill switch
trading_bot/
  config.py             # hard allowlist + $50/$200 defaults
  data_feed.py          # bars/quotes/indicators
  agent_core.py         # ReAct + VWAP/volume prefilter
  risk_manager.py       # ATR sizing, DD kill, revenge lock
  executor.py           # submit + allowlist gate
  logger.py / notifier.py
  brokers/coinbase.py   # CDP Advanced Trade + paper gate
  macro_calendar.py     # optional 15m freeze
tests/                  # 42 tests expected green
.env.example            # template (no secrets)
HANDOFF.md              # this file
README.md / ARCHITECTURE.md / STATUS.md
```

---

## Next-agent priorities

1. Confirm keys in `.env`, run `python main.py --once` (paper)  
2. Prefer **natural** VWAP signals over forced fills  
3. Keep quiet alerts; optional Discord/Telegram webhooks in `.env`  
4. Do not raise caps or expand allowlist without Al  
5. Optional: wire `MACRO_CALENDAR_URL` for real news freezes  
6. Optional: quiet paper scan routine hourly if host supports routines  

---

## Explicit non-goals unless Al asks

- Origin/GitHub hosting  
- Robinhood  
- Selling non-allowlist bags  
- Raising above $200 / $50  
- Live trading without explicit “go live”

---

## Owner prefs

- Ops/Finance tone; quiet updates  
- This chat lineage is CruzBot Continuity while another Grok trading bot account may be usage-capped  
- Al’s name: Al Gre; timezone America/Chicago  
