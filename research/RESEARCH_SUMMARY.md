# Research Summary — Taker

Condensed, dated view of [RESEARCH_LOG.md](RESEARCH_LOG.md). Newest at the
bottom. One bullet per concrete thing done, tried, found, or broken.

---

## 20/07/2026

* Got the repo running on Windows — it was POSIX-only, so `.venv/bin` shims, `ln -sfn` fallbacks and pid handling all needed patching.
* Found the dashboard's liveness probe used `os.kill(pid, 0)`, which on Windows calls `TerminateProcess` — it would have killed the bot every 2 seconds once fed a real pid.
* Pulled **13,914 live @bonereaper fills** (18–20 Jul) instead of trusting an existing analysis.
* Found **all four** shipped strategy parameters were wrong: entry window 35s (his median entry is t−104s), 1 fill per market (he does ~24), flat 5 shares (he scales with price), price cap 0.98 (excluded his biggest volume bucket).
* Implemented the documented-but-unused fee model: `taker_fee = shares × 0.07 × p × (1−p)`; breakeven is `p + fee` (0.90 → 90.63%, 0.98 → 98.14%).
* Backtested a **Binance spot gate** over 584 windows: the book's favoured side wins **81.3%** alone, **96.0%** [92.0, 98.1] when filtered to ≥5bps agreement. Signal decays with time remaining, independently justifying the 120s window.
* Wired the gate fail-closed — a stale feed means no trade, never an ungated one.
* Caught `size_scale=0.035` pushing every ladder tier below the 5-share minimum, silently reverting to flat sizing. Fixed to 0.283 (5/9/17/57).
* Fixed the resolver: `/markets?slug=` returns empty once a market ages out of that index, so positions hung unresolved forever. `/events?slug=` is reliable (584/584). Same bug existed separately in the dashboard.
* Moved dashboard pollers off the event loop — worst-case `/api/*` latency went from 10s to 51ms.

## 21/07/2026

* Found `/api/state` ran ~5 uncached full-table scans per poll — **1.09 billion rows read in a day**, which tripped the hosted DB's free tier. Replaced with one shared 15s snapshot and migrated to a volume with no read quota.
* Added instrumentation the strategy could not be tuned without: `spot_bps` and `loser_ask` as queryable columns (the gate value previously existed only inside a text string).
* Added a bird's-eye KPI pane: market-level win rate vs payoff-implied breakeven, expectancy, profit factor, drawdown, and a P&L histogram showing the negative skew.
* Collapsed the decision log's consecutive duplicates — 93.2% of rows were repeats; ~15× fewer rows with counts preserved exactly.
* Split the repo: taker and maker now live in separate repos under `AI Trading/`, with identical structure (`strategy/`, `server/`, `research/`, `deploy/`).
* **Live result at 74 settled markets:** win rate 91.9% [83.4–96.2] vs 94.2% breakeven, net −$345.75. Only the 0.80–0.90 band has positive edge. Live gate audit shows ≥10bps 93.3% (n=15) vs <10bps 91.5% (n=59) — much weaker separation than the backtest. **Inconclusive, ~126 more markets needed.**
* Removed `maker/` from this repo — the maker now runs from its own repo and its own Railway service, verified live before deletion so a working copy always existed.

## 22/07/2026

* At **110 settled markets** the taker has crossed above its breakeven line for the first time: win rate **94.5%** [88.6–97.5] against 94.3% needed, net **+$62.85**, profit factor 1.06. CI still straddles the bar, so this is a hint with the right sign, not a result. ~90 more markets to a 90% call.
* Edge by entry price is now positive in three of four bands — 0.80–0.90 **+2.4**, 0.90–0.95 +0.4, 0.95–0.98 +0.9 — and negative only in 0.98–1.01 (**−0.3**), which is what the fee curve predicts.
* **The spot gate shows no live separation:** ≥10bps wins 94.4% (n=18) versus <10bps at 94.6% (n=92). The backtest measured 96.0% vs 81.3% at ≥5bps over 584 windows and that gap has not reproduced. Either the backtest overfit or the effect is far smaller than it looked. **Verdict: OPEN** — this is now the main open question for the taker, since the gate is the entire thesis.
* **Gate retest on fresh data (Option B):** built a read-only `strategy/backtest_gate.py` reconstructing the spot-signal mechanism historically over a non-overlapping 474-window range (2026-07-21→07-23, excl. the 584-window backtest set). Reproduces the backtest's shape but weaker: ungated spot-direction **77.0%** (n=474) → gated |≥5bps| **92.7%** (n=191, coverage 40.3%). Lift is real (+15.7pts) but gated accuracy is **below the 94.3% fee breakeven**, so the gate does not clear the bar standalone. The original 81→96 was the *book-favoured-side* combo (forward collection only, impossible offline). **Verdict: PARKED** — signal real, not dead, but insufficient to justify the gate until the forward 81→96 test lands. Two harness bugs fixed (series_slug ordering; wrong `btc-up-or-down` slug → 404s).
* **Forward gate-collector built (Session 3):** `strategy/collect_gate.py` runs in the same Railway container as a 2nd supervised subprocess, writing to a SEPARATE `COLLECTOR_DB` (`/data/collector.db`, not `trades.db`) — avoids the two-writer clash. Snapshots CLOB book + Binance spot_bps at t-120s, resolves winners via gamma, derives `hit_book`/`hit_gate`. Dashboard gains `/collector` (kanban-style WATCH→GATE→FIRE→HOLD→SETTLE flow), `/api/collector-state`, `/api/deploy-hook` (Discord relay), and a deploy footer (git SHA + Railway ID) on every page. Verified end-to-end locally against live Polymarket/Binance. Collecting ~300 windows ≈ 25h to settle whether the book-favoured-side + gate combo hits ~96% forward. **Verdict: LIVE.**
* **Collector page self-documenting + SPA nav:** `/collector` now carries an explainer (objective / what we watch / investigate / expected / verdict / scenarios / indicators / time-to-verdict) and a `GATE GAP` KPI (gate_acc − book_acc). The classic SPA `TopBar` gained the LIVE/KANBAN/COLLECTOR nav (was missing — only kanban/collector had it). **Verdict: LIVE (UX only).**
