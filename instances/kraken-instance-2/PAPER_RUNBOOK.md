# CruzBot Paper Runbook (pre-live)

## Mode
- `PAPER_TRADING_MODE=true` always for this phase
- Natural VWAP/volume signals only (no forced demo fills unless Al asks)
- Caps: $50 / trade, $200 total; 8-coin allowlist

## Fees (checked 2026-09-17 via CDP)
Coinbase returned a VIP-looking tier in one field, but **spot volume on this key is 0** and the non-promo tier is **Intro**:
- Intro **taker 0.90%** / maker **0.50%**
- Round-trip taker on a $50 trade ≈ **$0.90** fees alone
- That is hostile to tiny VWAP scalps; paper ledger must net **after fees**
- Config assumption for paper P&L: `TAKER_FEE_RATE=0.009`, `MAKER_FEE_RATE=0.005`, `SLIPPAGE_BPS=5`
- Prefer limit/maker-style sims when the bot posts limits; treat marketable limits as taker

If live later, re-query `get_transaction_summary` before arming — do not assume VIP.

## Kill switch
- `Ctrl+C` / SIGTERM → cancel opens, notify, exit
- Optional flatten: `python main.py --liquidate-on-kill` (paper-sim only while paper mode on)
- Daily 3% DD halt + revenge lockouts remain on

## Quiet alerts
- In-app / routine pings on BUY/SELL/stop/TP/daily digest only
- Optional: set `DISCORD_WEBHOOK_URL` or Telegram token+chat in `.env`

## Macro freeze
- Enabled ±15m around NFP/CPI/FOMC/etc. when `MACRO_CALENDAR_URL` is set
- Without URL, mock/empty calendar (no live news pause) — still safe, just weaker

## Commands
```bash
cd agentic-day-trading-bot && source .venv/bin/activate
python main.py --once                 # one natural paper cycle
python scripts/paper_session_status.py
python main.py                        # continuous paper loop
```

## Success bar before live
Several natural round-trips, net ≥ flat after fees, no allowlist/cap violations.

## Telegram ops commands (user-initiated)
Set `TELEGRAM_COMMANDS_ENABLED=true` with bot token + chat id. Long-poll replies only when you send:
- `/status` — cash/equity, positions, pause, strategy, last tick age, pid
- `/pause` / `/resume` — skip or allow new buys (exits still managed)
- `/pnl` — day performance report
- `/kill` — cancel, flatten allowlist paper positions, stop loop
- `/mode` — `MODE: PAPER` or `MODE: LIVE` (+ cash/equity)
- `/mode live` — does **not** switch yet; replies to type `/confirm_live` (pending ~90s TTL)
- `/confirm_live` — only if pending valid: runtime `paper_trading_mode=False` + high-priority alert
- `/mode paper` — immediate runtime PAPER restore + high-priority alert; cancels pending live confirm

**Runtime only:** mode flips mutate the in-memory Settings flag used by broker/executor. `.env` is not rewritten. Restart always returns to `.env` `PAPER_TRADING_MODE` (safe default). LIVE still respects allowlist, $50/$200 caps, and post-only. Paper book is preserved across runtime flips.

Fill/exit alert templates unchanged. No unsolicited status spam (except high-priority mode-change alerts).

## Stale tick / reconnect
`STALE_TICK_SECONDS=15` — if quote/L2/bars fetch age exceeds 15s, log warning and re-init client with exponential backoff (1s→60s).

## systemd (optional daemon)
```bash
# from project root
mkdir -p ~/.config/systemd/user
cp deploy/cruzbot.service ~/.config/systemd/user/
# edit WorkingDirectory / ExecStart / EnvironmentFile / User as needed
systemctl --user daemon-reload
systemctl --user enable --now cruzbot
# or system-wide:
# sudo cp deploy/cruzbot.service /etc/systemd/system/ && sudo systemctl enable --now cruzbot
```
