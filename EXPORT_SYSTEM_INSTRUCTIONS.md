# CruzBot Operational System Instructions (export 2026-09-18)

This document is the **trading engine** playbook to rebuild CruzBot.  
It is **not** the Grok Bot host/assistant system prompt (that cannot be exported).

## Mode
- **PAPER ONLY** until Al explicitly approves live.
- Telegram: **fills / exits / requested P&L only** (no kill-switch, shutdown, status spam, or "If LIVE" chatter).
- Caps: **$50 max per trade**, **$200 max total paper exposure**.
- Allowlist only: BTC-USD, ETH-USD, SOL-USD, XRP-USD, LINK-USD, AVAX-USD, SUI-USD, ADA-USD. Never trade other bags.

## Strategy mode: `volume_sweet_spot`
(Replaces ATR trail scalps.)

### Entry (all required)
1. **RVOL breakout:** relative volume > **2.0×** vs 20-period volume average; price breaks/holds **above VWAP**.
2. **Sweet-spot retest:** do **not** buy the spike candle; wait for pullback to VWAP or EMA9/21 with pullback volume **< 50%** of breakout candle volume.
3. **Fee clearance:** take-profit path must be ≥ **2.5%** price (and ≥ **2.5×** round-trip maker fee hurdle).
4. **Spread filter (required):** skip if `(ask-bid)/mid > 0.1%`.
5. **Dedupe:** max **1 open position per symbol**; no second buy on same symbol within **5 minutes**.

### Execution
- **Post-only limit (maker) only.** Reject market / taker orders.
- Paper simulates post-only limits; live remains off.

### Exits (no ATR trail)
- **SL:** setup swing low **− 0.2%**.
- **TP1:** 50% at **1:1** R:R vs SL distance; then stop → breakeven + fee cushion.
- **TP2:** remaining 50% at **2.5:1** R:R or RVOL > 3× exhaustion.
- **Time stop:** hard flat at **30 minutes**.

### Circuit breakers (retained)
- 2 stop-outs on a coin → 30m lockout for that coin.
- 3% daily drawdown → halt new entries for the day.
- Macro freeze window when calendar says so (logs only; no Telegram spam).

## Telegram templates
### BUY
```
🔵 BUY FILLED | [SYMBOL]
------------------------------------
• Entry Price: $[Price]
• Total Spent: $[Cost] ([Qty] coins)
• Target TP: $[TP Price]
• Stop Loss: $[SL Price]
• Order Type: Limit Maker
```

### SELL
```
🟢 PROFIT TAKE | [SYMBOL]   # or 🔴 STOPPED OUT
------------------------------------
• Entry Price: $[Price]
• Exit Price: $[Price]
• Reason: [TP Hit / SL Hit / Time Exit]
• NET P&L: [+$X.XX / -$X.XX] (Fees Deducted)
• Current Cash: $[Cash]
• Current Equity: $[Equity]
```

### On-demand / daily report
```
📊 CRUZBOT PERFORMANCE REPORT
Date: [MM/DD/YYYY]
...
• Total Trades / Win Rate / Day Net P&L
• Paper Bankroll: cash | equity
```

Bankroll lines may also show live Coinbase cash/equity for reference; **sizing uses paper book**, not full live equity.

## Key env knobs (see `.env.example`)
- PAPER_TRADING_MODE=true
- STRATEGY_MODE=volume_sweet_spot
- POST_ONLY=true
- MAX_NOTIONAL_PER_TRADE_USD=50
- MAX_TOTAL_EXPOSURE_USD=200
- RVOL_BREAKOUT_MULT=2.0
- PULLBACK_VOL_FRAC=0.5
- MIN_TP_PCT=0.025
- FEE_TO_TARGET_MULT=2.5
- SWING_SL_BUFFER_PCT=0.002
- MAX_HOLD_MINUTES=30
- MAX_SPREAD_PCT=0.001
- BUY_DEDUPE_SECONDS=300
- MAKER_FEE_RATE=0.005
- TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (secrets — not in this export)

## Rebuild steps
1. Python 3.11+, `python -m venv .venv && .venv/bin/pip install -r requirements.txt`
2. Copy `.env.example` → `.env`; add Coinbase CDP + Telegram secrets locally.
3. `PAPER_TRADING_MODE=true` then `.venv/bin/python main.py`
4. Run tests: `.venv/bin/pytest -q`
