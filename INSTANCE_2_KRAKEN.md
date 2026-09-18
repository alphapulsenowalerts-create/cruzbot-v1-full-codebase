# Instance #2 — Kraken paper (isolated)

Second CruzBot process for **Kraken spot paper**. Instance #1 stays on Coinbase
(`BROKER=coinbase`, `deploy/cruzbot.service`). Do not share SQLite, paper book,
or optimizer files between the two checkouts.

Strategy stack is unchanged: 5m low-volume retest / volume sweet spot, EMA 1h/4h
confluence, L2 imbalance, ADX/chop, ATR sizing ($50 trade cap, $200 book max),
Binance/Bybit lead-lag feed, `POST_ONLY` maker defaults.

## Paper-only rules

- Keep `PAPER_TRADING_MODE=true` in the Instance #2 `.env`.
- Default examples never arm live. Live still requires the existing
  `/mode live` + `/confirm_live` Telegram flow (or `PAPER_TRADING_MODE=false`
  **and** `python main.py --live`).
- When paper is on, `KrakenBroker` **never** calls Kraken `AddOrder` /
  `CancelOrder` / `CancelAll`. Fills are local simulations (`kr-paper-…`).
- `POST_ONLY=true` rejects market orders (`market_orders_disabled`) and, on a
  live submit path, sends Kraken `oflags=post`.

## Isolated paths

| Resource | Instance #1 | Instance #2 |
|----------|-------------|-------------|
| Working dir | existing Coinbase checkout | `cruzbot_instance_2` (sibling checkout) |
| Env file | `.env` (`BROKER=coinbase`) | `.env` from `.env.instance2.example` (`BROKER=kraken`) |
| SQLite | `data/trading_bot.db` | `data/trading_bot_2.db` |
| Paper book | `data/paper_book.json` | `data/paper_book_2.json` |
| Optimizer params | `data/active_params.json` | `data/active_params_2.json` |
| systemd | `cruzbot.service` | `cruzbot2.service` |

Copy `.env.instance2.example` → `.env` in the Instance #2 checkout. Fill
`KRAKEN_API_KEY` / `KRAKEN_API_SECRET` and `TELEGRAM_BOT_TOKEN` /
`TELEGRAM_CHAT_ID`. Leave Coinbase keys empty on this instance.

## Symbol mapping

CruzBot keep allowlist pairs as `BTC-USD`. Kraken names are resolved in
`trading_bot/brokers/kraken.py`:

| Helper | `BTC-USD` / `BTC/USD` becomes |
|--------|--------------------------------|
| `to_kraken_pair` (REST `pair=`) | `XBTUSD` |
| `to_kraken_wsname` (WS v1) | `XBT/USD` |
| `to_kraken_altname` (OHLC/Ticker keys) | `XXBTZUSD` |
| `from_kraken_pair` | back to `BTC-USD` |

BTC is always **XBT** on Kraken. See the module docstring for the full
allowlist table (ETH, SOL, XRP, LINK, AVAX, SUI, ADA).

## Start / stop

From the Instance #2 checkout (venv activated):

```bash
cp .env.instance2.example .env   # once; then add Kraken + Telegram secrets
python main.py --once            # one paper cycle
python main.py                   # continuous paper loop
```

systemd (`cruzbot2`, not `cruzbot`):

```bash
mkdir -p ~/.config/systemd/user
cp deploy/cruzbot2.service ~/.config/systemd/user/
# edit WorkingDirectory / ExecStart / EnvironmentFile / User to cruzbot_instance_2
systemctl --user daemon-reload
systemctl --user enable --now cruzbot2
systemctl --user status cruzbot2
systemctl --user stop cruzbot2
```

Dry-run (no Kraken credentials, mock broker):

```bash
python main.py --dry-run --once
```

## Tests

```bash
python -m pytest -q tests/test_kraken_broker.py
python -m pytest -q
```

Tests mock REST/WS. Do not put live API credentials in the repo or CI.
