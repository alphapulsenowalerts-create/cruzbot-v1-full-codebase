# Instance separation (locked 2026-09-18 CT)

**Instance #1 (this repo `main`) — ours / Chief of Staff / Coinbase paper**
- Broker: Coinbase Advanced Trade only
- State: `data/trading_bot.db`, `data/paper_book.json`, `data/active_params.json`
- Do **not** merge PR #1 or any Kraken / Instance #2 branch into `main`

**Instance #2 — other bot / Kraken paper (separate)**
- Lives on branch `cursor/kraken-instance-2-8598` / open PR #1
- Must use its own checkout, `.env`, SQLite, and paper book (`*_2` paths)
- Review / merge only if Al explicitly asks; default is keep forever separate

---

# CruzBot status (handoff 2026-09-17 CT)

- Locks: 8-coin allowlist, $50/$200, ATR 1.0/1.5/1.5, paper default, quiet mode
- CDP account pagination fix: `get_accounts(limit=250)` so USD cash is visible
- Tests: 42 passed (last local run)
- Paper round-trip verified on XRP (~$50), realized ~+$0.02
- Live: not armed
- See HANDOFF.md for full pickup instructions
