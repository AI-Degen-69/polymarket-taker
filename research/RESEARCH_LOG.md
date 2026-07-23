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

## Session 2 — 2026-07-22 (gate retest, fresh data)

### Does the Binance spot gate earn its place on fresh, non-overlapping data?

**Method.** Built a read-only harness (`strategy/backtest_gate.py`) that reconstructs the gate's *mechanism* historically: for each 5-min window on a fresh range (2026-07-21 → 2026-07-23, excluding the 584-window backtest set), resolve the outcome via `/events?slug=btc-updown-5m-<open_ts>`, and compare it to the sign of Binance BTCUSDT's move from window open to t-120s (the live gate's decision point). `ungated` = spot-direction accuracy over all windows; `gated` = accuracy restricted to windows where |move| ≥ 5bps; `coverage` = fraction of windows the gate passes. 474 windows resolved (07-23 is future, skipped), 0 spot-data gaps.

**Result.**

| condition | n | accuracy | coverage |
|---|---|---|---|
| ungated (all windows) | 474 | 77.0% | 100% |
| gated (\|bps\| ≥ 5) | 191 | 92.7% | 40.3% |

Reproduces the SHAPE of the original backtest (which claimed 81.3% ungated → 96.0% gated over 584 windows) but at lower absolute levels: fresh gated accuracy is 92.7%, not 96.0%. The lift is real (+15.7 pts) but the gated subset sits BELOW the taker's ~94.3% fee breakeven, so the gate as a standalone win-rate improver does not clear the bar on fresh data.

Caveat: this harness audits the SPOT-DIRECTION signal only. The original 81→96 number was the BOOK-FAVOURED-SIDE + gate combo, which cannot be reconstructed offline (CLOB `/book` is live-only, no timestamp param). That combo still needs a forward collector (~300 windows ≈ 25h).

**Verdict.** PARKED — the spot signal is not dead (it separates 77→93 on brand-new data), but its standalone magnitude is below breakeven, so it does not by itself justify keeping the gate active. The decisive book-favoured-side combo is still untested; revisit only after the forward collector lands. Until then the gate is not load-bearing.

### Instrumentation bugs in the harness (each would have faked the result)

- `/events?series_slug=` returns 2025-era windows oldest-first and is unreliable for recent history → switched to direct per-window slug lookup `btc-updown-5m-<open_ts>` (the resolver's proven path).
- The per-window market slug is `btc-updown-5m` (no "or"); using the series string `btc-up-or-down-5m` made every lookup 404 (resolved=0).
- **Verdict.** DEAD (fixed) — both would have produced a fake zeros / overfit result.

## Session 3 — 2026-07-22 (forward gate-collector built)

### Can we collect the live book-favoured-side + spot-gate combo forward?

**Method.** Built `strategy/collect_gate.py`: a read-only 24/7 observer that runs
in the SAME Railway container as the bot but writes to a **separate** SQLite
file (`COLLECTOR_DB`, default `/data/collector.db`) — never `trades.db`, so it
avoids the documented two-writer clash. At `t_remaining == 120s` it snapshots
the CLOB top-of-book for both sides (`bid_up/bid_down/ask_up/ask_down`),
computes `book_favored`, and records `spot_bps` (Binance REST move vs window
open) + `spot_favored`. At resolution it records the winner via the same
gamma lookup the resolver uses, then derives `hit_book` (book_favored == winner)
and `hit_gate` (|spot_bps|≥5 AND spot_favored == winner) — exactly the
ungated-vs-gated comparison the backtest measured, now forward.

Supervised as a second `run_service.py` subprocess (restart-on-death, same
pattern as the bot). Plumbed into the dashboard as `/collector` — a
server-rendered kanban-style flow (WATCH→GATE→FIRE→HOLD→SETTLE) reading
`/api/collector-state`, plus a `/api/deploy-hook` that relays Railway deploy
events to Discord and a deploy footer (git SHA + Railway deploy ID) on every
page.

**Verified locally** (real Polymarket/Binance from dev host): live market +
book + Binance ticker all fetch; a forced snapshot row writes
(`book_fav=DOWN, spot_bps=-16.9, spot_fav=DOWN`); a known-resolved window
resolves to `winner=DOWN, hit_book=0, hit_gate=0, status=RESOLVED`. Schema bug
(`hit_book`/`hit_gate` columns missing) found and fixed during the test.

**Result.** Collector is LIVE in the container, collecting ~300 windows ≈ 25h.
No findings yet — the sample is being built. The dashboard now shows
ungated book-accuracy vs gated accuracy building up window-by-window.

**Verdict.** LIVE — design complete and shipped; awaiting the fresh sample to
settle the OPEN question from Session 2 (does the book-favoured-side + gate
combo actually hit ~96% forward?).

### Collector page made self-documenting + SPA nav wired

**Method.** Added an explainer block to `/collector` (objective, what we watch,
what we investigate, expected results, verdict shape, scenarios, indicators,
time-to-verdict) and a `GATE GAP` KPI (gate_acc − book_acc). Added a
LIVE/KANBAN/COLLECTOR nav to the classic SPA `TopBar` (it was missing — only
the kanban/collector pages had it), so every view can switch.

**Verdict.** LIVE — no research conclusion changed; documentation/UX only.

### Discord deploy webhook removed; collector liveness flag added

**Method.** Removed the `/api/deploy-hook` Discord relay (unnecessary — the
deploy footer already shows sha + railway ID, and you do not want the webhook).
The collector now writes `collector.pid` and the dashboard polls it into
`/api/state.collector_running` (mirrors the bot's liveness pattern), so the
collector's health is directly observable instead of inferring it from DB
writes.

**Verdict.** LIVE — collector is observable end-to-end; nothing else blocks
letting it run ~25h to reach a verdict.

### Collector info panel rewritten in plain language (two-flame heat model)

**Method.** Rewrote the `/collector` explainer + KPI tiles for readability: a
glossary (book favourite / CLOB / bps / gate / 81→96), per-tile meaning, a
**two-flame** verdict strip (🔥 BOOK HEAT at book_acc ≥81%, 🔥 GATE HEAT at
gate_acc ≥94% = gate validated; both = KEEP, else PARKED), and corrected the
"bars at the top" wording to "tiles" (the page renders tiles, not chart bars).
Green now means *meets the benchmark* (book ≥81%, gate ≥94%, gap >0), not 100%.

**Verdict.** LIVE — documentation/UX only; thresholds are explicit and match
the backtest claim (81→96) and the taker fee breakeven (~94%).

### Session 3 closeout + 24h handoff

**Status at wrap (2026-07-22 ~23:40).** All three deliverables shipped and
verified live; collector is self-healing in the Railway cloud (not on the
user's PC — a local PC restart does not affect it):
- commit `7e0d248` == origin/main; `DEPLOY_SHA=7e0d248`; live deploy `a41c8c8e`.
- `bot_running=True`, `collector_running=True` (paper).
- collector.db intact, **33 windows resolved** (was 19 earlier same session);
  last snapshot ~170s before check → actively collecting.
- `/collector` renders glossary + two-flame verdict strip (BOOK HEAT ≥81%,
  GATE HEAT ≥94%).

**What user does in ~24h:** open `/collector`. Read `WINDOWS RESOLVED` and the
two flames:
- ≥150 resolved + 🔥🔥 both flames (BOOK HEAT ≥81%, GATE HEAT ≥94%) → gate
  VALIDATED (KEEP).
- flames not both lit / GATE GAP ≤ 0 at n≥150 → PARKED (gate not proven).
- below ~150 → still collecting, ignore the %s.

Nothing else to do. Collector needs ~150 resolved windows (≈11-13h at current
rate) for a trustworthy verdict.

## Session 4 — 2026-07-23 (24h collector check + gate-accuracy bug)

### At the 24h mark, what does the forward collector say, and is the dashboard reporting it correctly?

**Method.** Pulled `/api/collector-state` from the live Railway deploy
(`DEPLOY_SHA=7e0d248`). Computed book accuracy (`hit_book / n`) and gated
accuracy two ways: (a) the shipped dashboard metric `hit_gate / n`, and (b) the
backtest-aligned `hit_gate / gated` where `gated` = windows where the gate
actually fired (`|spot_bps| ≥ 5`). Cross-checked the DB-wide `stats` object
against the window payload and the collection span (oldest snap 07-22 18:52 →
newest 07-23 02:33, 7.7h continuous). Confirmed liveness via `/api/state`
(`bot_running` + `collector_running` both True) and `/api/health` (200), and
that the `/data` volume (91 MB) keeps `collector.db` across redeploys.

**Result.**

- `n = 91` resolved (was 33 at Session 3 wrap) — still **below the 150 verdict
  threshold**, so no final call is possible yet; the experiment must keep
  collecting.
- Book-favoured-side accuracy (ungated): **71.4%** [61.4–79.7] (Wilson 95%),
  n=91 — *below* the 81% benchmark the backtest implied.
- Gated accuracy: the dashboard shipped `hit_gate / n` = 26/91 = **28.6%**
  (wrong denominator). The correct figure is `hit_gate / gated` = 26/30 =
  **86.7%** [70.3–94.7]. `GATE GAP` as-shipped rendered **❄️ −42.8 pts**; the
  true gap is **🔥 +15.2 pts**.
- Root cause confirmed in `server/dashboard.py:806`: `gate_acc` divided by `n`
  (all windows) instead of the gated subset. The flame logic
  (`gateHot = gate_acc >= 94`) is therefore miscalibrated — with the wrong
  metric it is capped at the ~33% gate coverage and can *never* light, so the
  page was actively misreporting a live, positive gate signal as cold/failing.

**Verdict.** Two distinct outcomes:

- **Experiment: OPEN (not enough sample).** At n=91 the forward book-favoured
  side (71.4%) is below the 81% it must clear, and gated accuracy (86.7%) sits
  below the 94% flame bar with a wide CI [70–95]. Neither flame can honestly
  light. The current lean — forward combo weaker than the backtest's 81→96 —
  is consistent with Session 2's fresh-data retest (77→93). Continue to ≥150
  windows, then revisit.
- **Instrumentation bug: DEAD (fixed).** Corrected `gate_acc` to divide by
  gated windows, added `gate_n` to the stats payload, and added a gated-sample
  floor (`gate_n ≥ 20`) to `GATE HEAT` so the flame only fires on a meaningful
  base, with `n` shown in the pill. Folded the uncommitted `server/kanban.py`
  LIVE/KANBAN/COLLECTOR nav + deploy footer into the same commit (it had been
  dangling since Session 3, leaving `/kanban` the only view without the nav).
  Redeployed volume-preserving (Railway auto-deploys on push; `/data` volume
  keeps the 91-window sample). Verified live after deploy: `/api/collector-state`
  returns the corrected `gate_acc` (86.7%) and `gate_n` (30).
