# Research Log — Taker (Polymarket BTC 5-min)

Running lab notebook. Newest entries at the bottom. Each entry:
**Question → Method → Result → Verdict**. Negative results are kept, not
deleted; most of what we have learned came from things that did not work.

**Conventions**
- Numbers here are measured, not estimated. If a figure is an estimate it says so.
- "Verdict" is a decision, not a summary: DEAD / PARKED / LIVE / OPEN.
- Instrumentation bugs get their own entries. On this project they have
  repeatedly been the difference between a real finding and a fake one.

The maker strategy lives in a separate repo (`polymarket-maker`) with its own
log. This file covers the taker only.

---

## Session 1 — 2026-07-20 → 07-21

**Context.** Starting point was a working paper-trading bot for Polymarket's
5-minute "Bitcoin Up or Down" market, whose README honestly reported losing
~$10 over 54 fills. Goal: find out whether any version of this is profitable,
and get it running unattended.

### Are the shipped strategy parameters what the reference trader actually does?

**Method.** Pulled 13,914 of @bonereaper's live BTC-5m fills (18-20 Jul) from
the data-api activity feed rather than trusting an existing write-up.

**Result.** All four shipped parameters were wrong:

| parameter | shipped | measured |
|---|---|---|
| entry window | last 35s | volume-weighted median entry at t-104s |
| fills per market | 1 | ~24 (median 20) |
| size | flat 5 shares | scales with price; 0.98+ is 3.6% of trades but 35.4% of dollars |
| price cap | 0.98 | his largest volume bucket is 0.98-0.99 |

**Verdict.** DEAD - rebuilt `strategy/strategy_rules.py` and `strategy/config.py`
to the measured values. Added a side-lock so the bot cannot hold both outcomes
of one market.

### Does the fee model change the answer?

**Method.** Implemented the fee the market spec documents but the code never
applied: `taker_fee = shares * 0.07 * p * (1-p)`.

**Result.** Breakeven is `p + fee`: 0.90 -> 90.63%, 0.98 -> 98.14%. Ignoring it
overstates edge by ~6-7%, enough to invert the sign.

**Verdict.** LIVE - every fill is graded against the breakeven its own price demands.

### Can a Binance spot signal beat the order book?

**Method.** Backtested over 584 resolved windows: compare the book's favoured
side against the sign of BTC's move since the window open.

**Result.**

| decision point | threshold | coverage | hit rate | 95% CI |
|---|---|---|---|---|
| t-60s | none | 100% | 81.3% | [78.0, 84.3] |
| t-60s | >=5bps | 30% | 96.0% | [92.0, 98.1] |
| t-120s | >=5bps | 25% | 95.1% | [90.2, 97.6] |
| t-180s | >=5bps | 20% | 88.2% | [81.2, 92.9] |

Signal decays with time remaining, independently justifying the 120s window.

**Verdict.** LIVE - gate wired fail-closed: a stale feed means no trade, never
an ungated one.

### Instrumentation bugs found (each would have faked a result)

- `os.kill(pid, 0)` as a liveness probe calls `TerminateProcess` on Windows -
  it would have killed the bot every 2 seconds once fed a real pid.
- `size_scale=0.035` pushed every ladder tier below the 5-share minimum,
  silently reverting the strategy to flat sizing. Fixed to 0.283 (5/9/17/57).
- The resolver queried `/markets?slug=`, which returns empty once a 5-min
  market ages out of that index, so positions hung unresolved forever and P&L
  never realised. `/events?slug=` is reliable (584/584). Same bug existed
  separately in the dashboard.
- Dashboard pollers ran blocking HTTP inside async loops; worst-case `/api/*`
  latency went from 10s to 51ms after moving them to `asyncio.to_thread`.
- `/api/state` ran ~5 uncached full-table scans per poll. On a per-row-billed
  database that read 1.09 BILLION rows in a day and tripped the free tier.
  Replaced with a single shared 15s snapshot; migrated to a volume with no
  read quota.

**Verdict.** All DEAD (fixed). Recorded because each produced plausible numbers
while being wrong.

### Live result so far

At 74 settled markets: win rate 91.9% [83.4-96.2] against a payoff-implied
breakeven of 94.2%. Net -$345.75. Per-entry-price edge is positive only in the
0.80-0.90 band (+1.8 points); every band above it is negative. Live spot-gate
audit shows >=10bps at 93.3% (n=15) versus <10bps at 91.5% (n=59) - far weaker
separation than the backtest, though the sample is small.

**Verdict.** OPEN - inconclusive, ~126 more markets needed for a 90% call.
