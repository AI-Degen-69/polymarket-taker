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
