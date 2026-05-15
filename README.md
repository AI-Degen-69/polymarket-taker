# polybot_beginner

Your first trading bot on **Polymarket**. It trades the 5-minute "Bitcoin Up
or Down" market on Polygon and comes with a real-time
Bloomberg-terminal-style dashboard for watching it work.

The strategy is **late-window convergence scalping**: when the market has
clearly chosen a winner in the last few seconds of a 5-minute window, the
bot takes the winning side's offer for a small residual edge, then holds to
resolution. The pattern is reverse-engineered from a profitable Polymarket
trader — see [`research/bonereader_analysis.md`](research/bonereader_analysis.md).

This repo is built for learning. Real-money parts are kept small, every
risky action has a kill switch, and every setup step is a one-line script.

---

## Quick orientation — what you'll do

```
1. install deps                       (5 min)
2. create a new account in MetaMask   (30 sec)
3. log into polymarket.com with it    (1 min)  -- deposit wallet deploys
4. deposit USDC.e via Polymarket UI   (5 min)
5. export private key from MetaMask
   and import into the bot's .env     (30 sec)
6. derive API credentials             (10 sec)
7. verify setup                       (5 sec)
8. launch dashboard + bot             (10 sec)
```

End-to-end: ~15 minutes if everything goes smoothly.

> **Want to try it without funding a wallet?** Run
> `scripts/run_paper.sh` — the bot watches live markets and logs decisions
> but never places real orders. Useful for learning what the bot does
> before risking money.

## Heads up before you start

- **Polymarket is geo-restricted.** The CLOB is unavailable in many
  jurisdictions (US, UK, France, others). If polymarket.com blocks you, the
  bot won't work either.
- **This is real money on a public blockchain.** Setup costs ~$0.50 in
  Polygon gas for a handful of transactions. Trading risks whatever you
  fund the wallet with — start with $30 to learn.
- **The strategy as shipped is not consistently profitable.** Read the
  ["Expectations"](#expectations) section before you commit any real
  capital. This is a starting point you'll want to tune.

---

## Prerequisites

| | version | install |
|---|---|---|
| Python | 3.11+ | macOS: `brew install python@3.11` · Linux: `apt install python3.11 python3.11-venv` · Windows: [python.org installer](https://www.python.org/downloads/) |
| Node | 18+ | macOS: `brew install node` · Linux: [nodesource setup](https://github.com/nodesource/distributions) · Windows: [nodejs.org](https://nodejs.org/) |
| MetaMask | latest | https://metamask.io/ (browser extension) |

You'll also need a way to get **MATIC** and **USDC.e** onto your wallet on
the Polygon network. The easiest path for beginners:

1. Open an account on Coinbase, Kraken, Binance, or any exchange that
   supports Polygon withdrawals.
2. Buy MATIC and USDC. Keep amounts small to start — ~$30 USDC, $1–2 MATIC.
3. Withdraw to the wallet address you'll generate in step 2 below. **On the
   network selector, choose Polygon (not Ethereum).**

If you already have USDC.e in a wallet on another chain, you can bridge it
via [app.polygon.technology](https://app.polygon.technology/).

> **USDC.e vs USDC.** Polymarket uses USDC.e (the older "bridged" USDC).
> Most exchanges send the newer "native" USDC by default — make sure the
> network is Polygon and the token symbol is **USDC.e** (or just "USDC"
> when the network is Polygon — exchanges often label it that way).

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/<your-user>/polymarket_bot_beginner
cd polymarket_bot_beginner

# Python venv + deps
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

# UI deps
cd ui && npm install && cd ..
```

### 2. Create a new account in MetaMask

You'll do the wallet creation, the Polymarket registration, **and** the
funding all inside MetaMask + polymarket.com — no terminal yet.

1. Open MetaMask → account menu (top-right circle) → **Add account or
   hardware wallet** → **Add a new account**.
2. Name it something obvious like *polybot*.
3. Switch to that new account in MetaMask.

> **Why a dedicated account?** Trading bots should run with a wallet that
> holds nothing else. If the bot has a bug or the key leaks later, only
> the bot's small balance is at risk — not your main wallet.

### 3. Sign into polymarket.com with the new account

1. **Log out** of any existing polymarket.com session first (avatar →
   Logout) so the wrong account doesn't auto-connect.
2. Open polymarket.com → **Log In** → choose **MetaMask**.
   If you don't have a Polymarket account yet, sign up via my referral
   link — it helps support this project at no cost to you:
   [polymarket.com/?r=allaboutai](https://polymarket.com/?r=allaboutai).
3. **Verify** the MetaMask popup shows the *polybot* account you just
   created, then sign the auth message.
4. Polymarket auto-deploys a **deposit wallet** for you (no gas needed —
   they sponsor it). This is the smart-contract wallet your bot will
   trade from.

### 4. Deposit funds via the Polymarket UI

Easiest path for beginners — Polymarket's UI handles the on-chain plumbing
(USDC.e → pUSD wrap, deposit-wallet routing) for you.

1. On polymarket.com → avatar → **Deposit**.
2. Pick **Polygon · USDC.e** as the source asset and follow the
   instructions to send from your exchange. Start with **$30+**.
3. **On the exchange's withdrawal form:**
   - **Destination address:** the one Polymarket showed you in the
     deposit modal.
   - **Network:** **Polygon** (not Ethereum, not BSC).
   - **Token:** USDC.e (sometimes labeled just "USDC" on Polygon).
4. The deposit usually shows up on Polymarket within 1–3 minutes.

> You do **not** need MATIC in this wallet. Polymarket sponsors gas for the
> deposit wallet's trades. (If you ever want to *withdraw* directly
> on-chain you'll need a tiny bit, but you can grab that later.)

### 5. Import the wallet into the bot

Now extract the private key from MetaMask and hand it to the bot. The bot
needs the private key to sign orders; MetaMask was just for setup.

1. In MetaMask, with the *polybot* account selected → **⋮** menu (account
   details) → **Show private key**. Enter your MetaMask password, copy
   the key (starts with `0x`, 64 hex chars).
2. Run:
   ```bash
   .venv/bin/python scripts/import_wallet.py
   ```
   The script will prompt you to paste the key — input is hidden, so the
   key won't appear on screen or in your shell history. Press Enter to
   submit.

   It then:
   - validates the key, derives the address,
   - **scans the on-chain DepositWalletFactory** to find the deposit
     wallet Polymarket deployed for you in step 3,
   - writes `.env` with `SIGNATURE_TYPE=3` and `FUNDER_ADDRESS=<deposit>`.

> **Security note:** once `.env` has the key, you can safely remove the
> *polybot* account from MetaMask if you like. The bot reads everything
> from `.env`.

### 6. Derive L2 API credentials

```bash
.venv/bin/python scripts/derive_api_creds.py
```

The bot uses these to authenticate every order. Stored in `.env`.

### 7. Verify the whole setup

```bash
.venv/bin/python scripts/verify_setup.py
```

Walks through every check (wallet, balance, deposit wallet ownership,
allowances, API auth) and prints `[ OK ]` / `[FAIL]` for each. If anything
fails, the script tells you exactly what to do.

---

### Alternative path: terminal-only

If you'd rather not use MetaMask at all and prefer to run everything from
the terminal:

1. `scripts/generate_wallet.py` — creates a fresh random wallet in `.env`.
2. Fund the EOA with USDC.e **and** ~1 MATIC for gas.
3. Import the same key into MetaMask **just to register on polymarket.com**
   (steps 3 & 4 above, but use *Import account* with the key from `.env`).
4. `scripts/wrap_to_pusd.py` — wraps your USDC.e to pUSD.
5. `scripts/migrate_to_deposit_wallet.py 0xYOUR_DEPOSIT_ADDRESS` — moves
   pUSD into the deposit wallet you got from polymarket.com.
6. Continue from step 6 above.

---

## Running

### Dashboard

```bash
scripts/run_dashboard.sh
```

Opens FastAPI on port 8787 and the Vite UI on port 5173. **Open
http://127.0.0.1:5173 in your browser.**

| panel | shows |
|---|---|
| Top bar | bot status (RUNNING / STOPPED / LOCKED), API health, last-trade flash |
| Wallet / Equity | pUSD cash + open position value + total equity |
| P&L 24h | realized PnL, win/loss count, win rate |
| Strategy | current entry caps + risk thresholds |
| Live Market | active 5-min market, countdown, Up/Down book (winner ask highlighted amber when in buy zone) |
| Decision Log | every decision the bot makes, live-streaming |
| Open Positions | current CTF holdings |
| Orders | recent order outcomes |

The dashboard works fine even when the bot isn't running — useful for
watching markets before going live.

### Bot: paper vs live

The bot has two modes. **Always start in paper.** When the dashboard shows
green decisions on real markets for a while and you understand what it's
doing, switch to live.

```bash
# PAPER — no real orders, logs decisions only. Safe.
scripts/run_paper.sh

# LIVE — places real orders against your funded deposit wallet.
scripts/run_live.sh

# Stop either one
scripts/stop_live.sh
```

Both modes use the same script to stop. Only one bot can run at a time
(the launcher refuses if `bot.pid` exists).

The dashboard top bar shows the mode prominently:

- `[ PAPER ]` in green = safe, no real orders
- `[ LIVE ]` in red = real money on the line
- `[ OFFLINE ]` in grey = bot not running

The 5-second screen flash only fires on **real** fills, never paper.

In the decision log and orders tables, paper entries are also flagged
internally (`dry_run=1`) so realized-PnL only counts real fills.

```bash
tail -f logs/bot_current.log    # watch the bot in real time
```

### Stopping everything

```bash
scripts/stop_live.sh
scripts/stop_dashboard.sh
```

---

## Tuning

All strategy knobs are in `bot/config.py`:

```python
max_entry_price:      0.98     # only fire when winner-side ask <= this
seconds_before_close: 35       # only fire when t_remaining <= this
min_t_remaining_sec:  8.0      # AND t_remaining >= this (avoid race-to-resolve)
order_size_shares:    5        # min is 5 per Polymarket
max_open_positions:   1
max_daily_loss_usd:   10000.0  # daily loss kill switch (10000 = effectively disabled)
```

And the convergence threshold in `bot/strategy.py`:

```python
LOSER_FLOOR = 0.85   # don't fire unless winner-side ask > this
```

Conceptually: the bot only enters when the market is convinced of a winner
(`winner ask > LOSER_FLOOR`) but hasn't fully converged
(`winner ask <= max_entry_price`). Tighter = fewer trades with more cushion;
looser = more trades with thinner per-trade edge.

Restart the bot after changes:

```bash
scripts/stop_live.sh && scripts/run_live.sh
```

---

## Troubleshooting

**`maker address not allowed, please use the deposit wallet flow`**
Your `SIGNATURE_TYPE` or `FUNDER_ADDRESS` isn't set up for the V2 deposit
wallet. Re-run `scripts/verify_setup.py` — it'll tell you which step to
re-do.

**`balance: 0` from `verify_setup.py` even though the deposit wallet
has pUSD on-chain**
Polymarket's API cache can lag for a minute. Wait 60s and re-run. If it
persists, your `FUNDER_ADDRESS` in `.env` doesn't match the deposit
wallet the CLOB knows about — check polymarket.com → Wallet to confirm.

**`order couldn't be fully filled. FOK orders are fully filled or killed`**
Not an error — that's the bot trying to take an ask that got swept by
someone else first. Normal in fast markets. The bot just moves on.

**Stuck transaction during setup (`wrap_to_pusd.py` or
`migrate_to_deposit_wallet.py` hangs)**
Polygon gas can spike. Run `.venv/bin/python scripts/bump_stuck_tx.py` to
replace the stuck tx with a higher-gas version.

**`RPC 401 Unauthorized` errors**
The default public RPC (`polygon-bor-rpc.publicnode.com`) sometimes
rate-limits. Sign up for a free Alchemy or QuickNode key and replace
`POLYGON_RPC_URL` in `.env`.

**Dashboard shows `BOT LOCKED (LOSS_CAP)`**
Daily loss exceeded `max_daily_loss_usd` in `bot/config.py`. Either wait
24 hours, raise the cap, or restart with a fresh `.env` to reset.

**Polymarket says my country is blocked**
You can't use this bot. Polymarket geo-blocks at the CLOB level — there's
no workaround that doesn't violate ToS.

---

## File map

```
polybot_beginner/
├── .env                # secrets — gitignored, never commit
├── .env.example        # reference: which script writes which field
├── bot/                # trading engine
│   ├── config.py       # all strategy knobs
│   ├── markets.py      # discover the live 5-min BTC market
│   ├── book.py         # CLOB order-book reader
│   ├── strategy.py     # decision logic
│   ├── orders.py       # SDK wrapper for FOK order placement
│   ├── risk.py         # daily-loss + open-positions caps
│   ├── store.py        # SQLite logger (trades.db)
│   ├── resolver.py     # back-fill resolutions for filled orders
│   └── main.py         # event loop
├── server/dashboard.py # FastAPI backend (port 8787)
├── ui/                 # Vite + React + TS frontend (port 5173)
├── scripts/            # setup + launch helpers
│   ├── generate_wallet.py
│   ├── check_balance.py
│   ├── wrap_to_pusd.py
│   ├── bump_stuck_tx.py
│   ├── migrate_to_deposit_wallet.py
│   ├── derive_api_creds.py
│   ├── verify_setup.py
│   ├── run_live.sh / stop_live.sh
│   └── run_dashboard.sh / stop_dashboard.sh
├── research/           # strategy analysis + market spec (MDs + .py fetchers)
└── requirements.txt
```

---

## Expectations

The 5-minute BTC market is competitive. Profitable traders here run
sub-second latency from co-located machines with paid RPC providers and
custom infrastructure. From a laptop on a public Polygon RPC, your fills
will be slower and the late-window edge is razor-thin.

**The strategy as shipped here lost ~$10 across 54 fills in an overnight
run during development.** The win rate was 89% but the breakeven needed
was ~92% at our average entry price. The shape of the problem is
asymmetric payoffs: many tiny wins (~$0.30) outweighed by occasional
full-stake losses (~$4.50).

Things you can try to improve it (in roughly increasing difficulty):

1. **Tighten the entry zone.** Set `LOSER_FLOOR` higher (e.g. 0.93) so the
   bot only takes very high-confidence trades. Fewer fills, better per-trade
   EV.
2. **Add a Binance spot-price gate.** Only fire if Binance's BTC/USDT moves
   in the favored direction by more than ~5 bps within the window. The
   stub for this lives in `bot/config.py` (`min_spot_offset_bps`).
3. **Better RPC.** Sign up for Alchemy/QuickNode for ~50% lower latency on
   reads.
4. **Reduce size when entering near $0.98+.** Asymmetric payoffs mean smaller
   stake at the marginal levels.
5. **Run shadow-mode for a week.** Add a `--dry-run` mode and collect
   thousands of "would have entered" decisions. Recompute simulated PnL
   with current fees to see if your tweaks actually help before risking
   capital.

Use this code to learn. Don't bet the house.

---

## Support

If this repo helped you, you can sign up to Polymarket through my referral
link: [polymarket.com/?r=allaboutai](https://polymarket.com/?r=allaboutai).
No obligation — it just helps me a little. Thanks.
