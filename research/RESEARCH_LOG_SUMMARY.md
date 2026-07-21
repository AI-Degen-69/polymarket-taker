# Research Log — Bulleted Summary

Condensed, dated view of [RESEARCH_LOG.md](RESEARCH_LOG.md). Newest at the
bottom. One bullet per concrete thing done, tried, found, or broken.

---

## 20/07/2026

* Cloned `poly-trading-bot` and got it running on Windows — the repo is POSIX-only, so `.venv/bin` shims, `ln -sfn` fallbacks and pid handling all needed patching.
* Found the dashboard's liveness probe used `os.kill(pid, 0)`, which on Windows calls `TerminateProcess` — it would have killed the bot every 2 seconds once fed a real pid. Replaced with a `ctypes` `OpenProcess` check.
* Pulled **13,914 of @bonereaper's live BTC-5m fills** (18–20 Jul) instead of trusting the repo's May analysis.
* Found **all four** shipped strategy parameters were wrong: entry window 35s (his median entry is t−104s), 1 fill per market (he does ~24), flat 5 shares (he scales with price), price cap 0.98 (excluded his biggest volume bucket).
* Rebuilt `bot/strategy.py` and `bot/config.py` to the measured values; added a side-lock so the bot can't buy both outcomes of one market.
* Implemented the fee model the repo documented but never applied: `taker_fee = shares × 0.07 × p × (1−p)`; breakeven is `p + fee` (0.90 → 90.63%, 0.98 → 98.14%).
* Backtested a **Binance spot gate** over 584 resolved windows: the book's favoured side wins **81.3%** alone, **96.0%** [92.0, 98.1] when filtered to ≥5bps agreement with spot. Signal decays with time remaining (88.2% at t−180s), which independently justifies the 120s window.
* Wired the gate fail-closed — a stale feed means no trade, never an ungated one.
* Built the $5,000 paper-account ledger: real cash constraints, positions marked to the live book, per-price-bucket scoring against breakeven.
* Caught that `size_scale=0.035` pushed every ladder tier below Polymarket's 5-share minimum, silently reverting the strategy to flat sizing. Fixed to 0.283 (5/9/17/57 shares).
* Fixed the resolver: it queried `/markets?slug=`, which returns empty once a 5-min market ages out of that index, so positions hung unresolved forever and P&L never realised. `/events?slug=` is reliable (584/584). Same bug existed separately in the dashboard.
* Moved all dashboard pollers off the event loop (`asyncio.to_thread`) — blocking HTTP inside async loops was stalling `/api/*` for seconds; worst-case latency went from 10s to 51ms.
* Deployed to **Railway + Turso** with a preflight that fails loudly on the two silent killers: a US region (Binance 451s US IPs, so the gate dies while the healthcheck stays green) and missing persistent storage.
* Made Turso usable: per-call reconnect cost **1,421 ms/write** against a ~4/sec requirement because every call re-ran the schema. Connection reuse + batched decision writes → **0.05 ms/call**.
* Fixed a `libsql` quirk that blanked the deployed dashboard: it upper-cases SQL keywords in `cursor.description`, so `action` arrived as `ACTION`, the UI read `undefined`, called `.startsWith()`, and React unmounted to a black page.
* Started the **copy-trade tracker** (`follow/`) shadowing @powerwinner, @bonereaper and @Anon; first capture 23:07.

---

## 21/07/2026

* Tracker ran **14.1 hours unattended** overnight with no gaps — 27,354 fills, 414 resolutions.
* Overnight mirror results (at *their* prices): **@powerwinner +$198.68** over 317 markets (62% win rate), **@bonereaper +$84.20** over 284 markets (54%). The 54% matches the 55.4% public sources report for that wallet, a good sign the tracker measures correctly.
* Dropped **@Anon** — 454 fills but **zero** resolved markets; it trades longer-dated markets (BTC-150k-by-December) and produces no P&L signal on any useful timescale.
* Noted the sharp contrast: the real @bonereaper profits while **our replica of him loses**, on the same markets.
* Measured real detection latency across 25,062 stored fills: **median 10.9s**, mean 23.5s, p90 30s. Price moves **median 4.58¢** in a ~13s gap — on a 0.95 entry the whole edge is 5¢.
* Built the **follower simulation**: on detecting each of their fills, price against the **live CLOB book**, walking the ladder as a real taker order would.
* Caught my own bogus measurement — the first shadow run reported 33–105s "latency", which was actually it grinding through a 7,976-fill backlog. Added a 45s age gate so a processing queue can never masquerade as follow latency.
* Caught a worse one: the shadow read **top-of-book only**, so it reported "10% fill rate, no liquidity". The live book actually had **460 shares at the touch and 7,625 within two ticks** — 203 of 204 "no depth" rejections were really our own 2.5-share orders falling under the 5-share minimum. Fixed to walk the full ladder at VWAP; fill rate went to ~100%.
* Discovered **four tracker instances** were running simultaneously against one database — `pkill -f` silently no-ops on Windows, so every "restart" stacked another. This had duplicated rows and resurrected the removed @Anon. Wrote `follow_start.sh` / `follow_stop.sh` that match the real Windows command line and refuse to double-launch.
* Fixed the poller backfilling ~500 historical trades on first run, which gave THEIRS hundreds of pre-resolved markets while the FOLLOWER had none. Added a stored epoch so both sides start at the same instant.
* **Result over 25 shared markets, same clock, same $5,000:** THEIRS **+$30.39** (72% of markets profitable) vs FOLLOWER **−$229.98** (20%).
* Corrected that number honestly: **~$158 of the gap was my own bug** (the follower was charged taker fees while THEIRS was computed gross). Genuine execution cost is **~$89**. Fee model now applied to both sides.
* Identified the mechanism as **adverse selection**, and it is asymmetric: we pay **+4.72¢** on the winning token and save only **−1.64¢** on the losing one. Saving on a ticket that expires at $0 is worth nothing; paying up on the one that pays $1 comes straight out of the payout. The lag costs you precisely on the trades that were going to work.
* **Found why the whole thing fails:** @powerwinner's average entry is **0.608 on the winning token and 0.376 on the losing token — summing to 0.984** — with near-balanced share counts (5,560 vs 5,415). **He buys both sides of the same market for less than $1.00.** 40% of his two-sided markets are outright locked (guaranteed payout > cost).
* Concluded copy-trading is **structurally impossible, not merely slow**: the edge is a 1–2¢ spread below par, so paying 4.72¢ more on one leg *inverts* the arithmetic rather than shaving it. That's why the win rate collapsed 72% → 20% instead of degrading gently.
* Noted a likely second structural barrier: at an average price near 0.50 he would pay the **maximum** taker fee (1.75%), which should erase his margin — so he is probably earning **maker rebates** by posting rather than taking. A follower is a taker by definition.
* Retired the follower: `follow/` and its scripts deleted, preserved in git at commit `053a4cf`, 4.6 MB of data archived to `archive/follow_final_*.db`.
* Wrote this log and [RESEARCH_LOG.md](RESEARCH_LOG.md).

---

## Open

* **Two-sided spread, sourced independently** — find markets where `UP_ask + DOWN_ask < $1.00` and take both legs ourselves. Same edge as @powerwinner, no follow lag. Open question: how often such windows exist **after** paying `0.07·p·(1−p)` on each leg, and whether they persist long enough to fill the second leg. Read-only to measure.
* **`maker/`** — passive quoting instead of chasing, built from 56,768 of @powerwinner's fills. Running with data (115 quotes, 50 fills, 5 resolutions) but **not yet documented in this log**.
* **Main bot sizing** — at n=20 markets the 95% CI on win rate straddled breakeven. Needs ~200 resolved markets before the bucket table can return a verdict. Deliberately left unchanged so the sample stays clean.
