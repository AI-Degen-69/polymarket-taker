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

### Make the collector verdict show its uncertainty, and fix the 200-window stats cap

**Method.** Follow-up to the gate-accuracy fix (above). Added a Wilson 95% CI
helper to `server/dashboard.py` and returned `book_ci` / `gate_ci` (as
percentages) in the `stats` block; `server/collector_page.py` now renders
`[lo–hi]` under the BOOK/GATE KPI tiles and inside the verdict flames, so the
uncertainty is visible before a flame can honestly fire. While reading the stats
code, found a second latent flaw: `stats` were derived from the same
`ORDER BY snap_ts DESC LIMIT 200` rows that feed the lane payload, so once the
collector passes 200 windows (it is built for ~300) every verdict number would
silently track a 200-row *tail* rather than the full sample. Replaced that with
a separate full-table aggregate `SELECT … FROM collector_windows` that feeds
`n`, `gate_n`, `hit_book`, `hit_gate`.

**Result.** CIs now surface live (e.g. at n=94: book 71.3% [61.5–79.5], gated
87.5% [71.9–95.0] n=32). Unit-tested the full-aggregate path on a 250-row
fixture: `stats` returns `n=250` (the whole table), not the ~200 a tail-limited
query would yield — so the verdict will stay honest past 200 windows.

**Verdict.** LIVE — UX/integrity only; no research conclusion changed. The CI
display makes the wide [70–95] gated band explicit, which is exactly why neither
flame should light yet at the current sample size.

## Session 5 — 2026-07-23 (forward verdict at n=313 + open-count display bug)

### The forward collector has now cleared the 150-window verdict threshold — what does the book-favoured-side + spot-gate combo actually deliver?

**Method.** Pulled `/api/collector-state` live from Railway
(`claude-poly-bot-production.up.railway.app`). Recomputed every verdict metric
*by hand* from the raw aggregates (`n`, `hit_book`, `gate_n`, `hit_gate`) rather
than trusting the rendered %, and cross-checked the dashboard's arithmetic
against an independent Wilson-95% computation. The collector has been running
24/7 since Session 3; `bot_running` + `collector_running` both True, `/data`
volume keeps `collector.db` across redeploys. This is the decisive sample the
Session-3 design called for (~300 windows).

**Result — decisive, and it kills the thesis:**

| metric | value | 95% Wilson CI | bar it must clear |
|---|---|---|---|
| windows resolved (`n`) | **313** | — | ≥150 needed ✔ (3.4× the 584-window backtest) |
| book-favoured-side acc (ungated) | **78.0%** | [73.0–82.2] | 81% (backtest book baseline) ❌ |
| gated acc (`\|spot_bps\|≥5` AND spot_fav==winner) | **88.3%** | [81.4–92.9] | 94% (taker fee breakeven) ❌ |
| gate coverage | 38.3% (120/313) | — | — |
| GATE GAP (gate − book) | **+10.3 pts** | — | >0 to earn its place (✓ sign, ✗ magnitude) |

The forward combo lands at **78 / 88**, not the backtest's claimed **81 / 96**.
Both flames are cold: BOOK HEAT needs ≥81% (78.0 misses, CI tops out at 82.2),
GATE HEAT needs ≥94% (88.3 misses by a wide margin, CI [81–93] never reaches
94). This is the *third* independent estimate of the same signal and they now
cluster tightly: backtest 81→96, Session-2 fresh retest 77→93, forward 78→88.
The lift is real (+10 pts) but the gated subset sits ~6 pts below the fee
breakeven the taker must clear to be profitable. The original 81→96 number was
the *book-favoured-side + gate combo* collected forward — it does not
reproduce at the sample size that matters.

**Verdict.** PARKED → **DEAD (as a standalone win-rate improver).** The gate
is not load-bearing: it consumes 38% coverage and still leaves the taker below
breakeven. Do not gate the live strategy on this signal. The taker's *only*
profitable band ever found was 0.80–0.90 entry price (Session 1/2) — that, not
the spot gate, is where any edge lives. Close the collector (it has answered
its question) unless we want to keep it as a negative-result archive.

### Instrumentation bug: `open` count rendered negative (`open: -113`)

**Method.** The live `stats.open` came back as **-113** — an impossible value
for a count. Traced it to `server/dashboard.py:_collector_state`: `open` was
computed as `len(windows) - n`, but `windows` is the **200-row payload tail**
(`ORDER BY snap_ts DESC LIMIT 200`) while `n` is the **full-sample** resolved
count (313). Once resolved rows exceed 200, `len(windows)` is capped at 200 and
`open` goes negative. The page renders this raw in `collector_page.py:197`
(`K('OPEN', st.open||0, ...)`) — so the dashboard was showing a negative
"awaiting resolve" count live.

**Result.** Fixed: the aggregate query now also returns `COUNT(*) AS total`
(all rows, resolved+open), and `open = total - n`. Verified with a synthetic
250-row DB (240 resolved + 10 open, snap_ts-ordered so the LIMIT-200 payload is
all-resolved — the exact trigger): the old formula gave `-40`, the new gives
`10` (correct). The accuracy/CI math was confirmed correct and unchanged
(book 78.0, gate 88.3 match the hand recompute), so this is a display-only fix;
it does not move the verdict.

**Verdict.** DEAD (fixed) — display bug only; same class as the Session-4
`gate_acc` and 200-row-cap bugs, and exactly why we recompute verdict metrics
independently instead of trusting the rendered number.

### Retire the forward collector (gate question answered)

**Method.** With the gate thesis DEAD, the 24/7 `strategy.collect_gate` observer
is no longer earning its keep (it writes to `collector.db` but never affects the
bot). Gated the collector supervisor thread in `deploy/run_service.py` behind
`COLLECTOR_ENABLED` (unset = stopped), so a Railway redeploy simply does not
start it. The bot, dashboard, and prune threads are untouched — only the second
subprocess is dropped. Also made the dashboard's `collector_running` poll respect
the same flag and remove any stale `collector.pid`, so the UI reports
`collector_running=False` instead of a false "alive" inherited from the prior
deploy.

**Result.** `collector.db` (313 resolved windows) stays frozen on the Railway
`/data` volume as the negative-result archive — the sample is preserved, just no
longer appended to. Set `COLLECTOR_ENABLED=1` in the host Variables to revive the
observer without a code change. Push triggers a Railway auto-redeploy; the bot
keeps trading on the 0.80–0.90 band analysis.

**Verdict.** LIVE (governance) — no research conclusion changed; the experiment
is closed cleanly.

### `/api/collector-state?full=1` — complete per-window export

**Method.** The default `/api/collector-state` payload was `LIMIT 200` windows,
so only 200 of the ~316 forward-test detail rows were reachable for offline
analysis. Added an opt-in `full` query param to `_collector_state()` (default
off, so the dashboard keeps its small payload); `full=1` drops the limit and
returns every `collector_windows` row. Stats were already computed over the full
table, independent of the payload limit, so the verdict numbers are unchanged by
this toggle.

**Result.** `curl ".../api/collector-state?full=1"` now returns all 316 rows.
Pulled into `exports/collector_state_full_live.json` for offline verification of
the verdict (every window independently reconcilable against `stats`).

**Verdict.** LIVE (UX/integrity) — closes the last gap in making the forward
sample fully exportable.

---

## Session 6 — 2026-07-23 (per-band edge at n=7995 — the last standing thesis is DEAD)

### At full sample (n=7995), is the "0.80-0.90 was the only profitable band" claim still true?

**Question.** Session 5's verdict named the 0.80-0.90 entry band as "the
taker's *only* profitable band ever found" (Sessions 1/2, n=110: +2.4 pts
vs breakeven). The whole remaining case for the strategy hung on that
band. But that was n=110; the live paper bot has since run for ~3 more
days and filled 7,885 more orders. The honest question is whether the
+2.4 pt reading was a real edge or a small-sample artifact.

**Method.** Pulled `/api/state` from the live Railway deploy
(`claude-poly-bot-production.up.railway.app`); the `sim.buckets` block
is computed in `strategy/store.py:sim_report` by joining `orders` to
`resolutions` on `condition_id`, grouping by `price` into four fixed
buckets (`0.80-0.90`, `0.90-0.95`, `0.95-0.98`, `0.98-1.01`),
computing each bucket's win rate and mean breakeven
(`p + 0.07·p·(1-p)`), and reporting `edge_pts = wr − breakeven`.
Independently recomputed `edge_pts` from the published `wins`/`n` and
`breakeven` (row arithmetic) — every band matches the published
`edge_pts` to displayed precision, so the dashboard isn't lying. Then
computed the Wilson 95% CI on each band's win rate and tested whether
the bucket's breakeven sits inside that CI.

**Result — every band is negative, including 0.80-0.90:**

| band | n | wins | WR | breakeven | edge_pts | WR 95% CI | breakeven in CI? |
|---|---|---|---|---|---|---|---|
| 0.80-0.90 | 1252 | 1059 | 84.58% | 86.31% | **−1.73** | [82.5%, 86.5%] | yes (barely — upper bound 86.48 vs BE 86.31) |
| 0.90-0.95 | 2086 | 1904 | 91.28% | 92.90% | **−1.63** | [90.0%, 92.4%] | **no** (upper bound 92.42 < BE 92.90) |
| 0.95-0.98 | 2335 | 2236 | 95.76% | 96.37% | **−0.61** | [94.9%, 96.5%] | yes (upper bound 96.53 > BE 96.37) |
| 0.98-1.01 | 2322 | 2271 | 97.80% | 98.65% | **−0.85** | [97.1%, 98.3%] | **no** (upper bound 98.32 < BE 98.65) |

Totals: n=7995, wins=7470 (93.43%), realized PnL −$1708.51, return
−34.17%, gross spent $188,695.72, total fees $378.59. The 0.80-0.90
band that the prior summary called "the only profitable one" is now
−$108 in realized P&L at 84.58% vs the 86.31% breakeven that those
prices demand.

**Interpretation.**

- The 0.80-0.90 +2.4 pt claim from n=110 was a small-sample artifact.
  The 95% CI at n=110 was roughly ±4 pts wide — a swing of 4.1 pts
  (from +2.4 to −1.73) across 11× more data is exactly what sampling
  noise looks like in this region. At n=1252 the CI is ±2 pts and the
  sign of the edge is pinned negative.
- The 0.90-0.95 and 0.98-1.01 bands are **dead with 95% confidence** —
  the breakeven lies *above* the WR's upper CI bound. Not borderline.
- The 0.80-0.90 and 0.95-0.98 bands are *technically* inside the CI
  (the point estimate is below breakeven, but the upper CI bound
  brushes it). The realized P&L in both bands is negative
  (0.80-0.90: −$108; 0.95-0.98: −$229), so the economic verdict is
  the same even if the statistical one has a hair of daylight.
- The 0.98-1.01 band is the single biggest dollar loser (−$1,073, 60%
  of the total P&L) despite having the highest win rate, because it
  carries 68% of the gross spend — exactly the volume profile the
  Bonereaper intel file predicted, minus the edge that intel attributed
  to latency arb. Without that infrastructure edge, the high-price
  band is a fee incinerator.
- The gate's 78/88 forward result (Session 5) and the per-band
  −1.73/−1.63/−0.61/−0.85 result here are *the same finding* at two
  scales: no entry band + no signal combination the taker has access
  to (entry-band filter, gate filter, size ladder) clears the
  payoff-implied breakeven with statistical confidence at n=7995.

**Verdict — taker project: DEAD (whole).**

- The 0.80-0.90 band: **DEAD.** Retracts the Session-1/2 "only
  profitable band" line. The edge was sampling noise.
- The 0.90-0.95 band: **DEAD** with 95% confidence.
- The 0.95-0.98 band: **DEAD** at the point estimate; CI just barely
  contains breakeven but the realized P&L is negative.
- The 0.98-1.01 band: **DEAD** with 95% confidence.
- The whole taker strategy (entry-band 0.80-0.99, last 120s, scaling
  ladder, spot-gate filter): **DEAD.** The remaining live paper trade
  is not informative — it just adds data to a closed question. **Stop
  the live bot.** This is the moment the project was designed to
  detect; running longer accumulates losses (paper) for a question
  the data has already answered.
- The collector, the live bot, and the dashboard can stay up only as
  a frozen negative-result archive. The right operational move is:
  set `BOT_ENABLED=0` (analogous to the `COLLECTOR_ENABLED` flag
  already wired in `deploy/run_service.py`) so the bot stops filling,
  while `collector.db` and `trades.db` remain on the `/data` volume as
  the dataset that killed the thesis.

### Instrumentation / methodology notes (for the next analyst)

- The bucket key in `/api/state` is the literal `f"{lo:.2f}-{hi:.2f}"`
  string from `sim_report` — when parsing, don't reconstruct from the
  bucket label, just match the key.
- `breakeven` in each bucket is the *mean* of per-row
  `breakeven_win_rate(price)`, not `breakeven_win_rate(mean_price)`.
  They differ when a bucket's prices are skewed (the 0.95-0.98 bucket's
  `breakeven=0.9637` is slightly higher than `breakeven(0.965)≈0.9633`
  because of the convexity of `p(1-p)` near 0.5; the per-row mean is
  the right number).
- `pending` rows (orders with no resolution yet) are excluded from
  the bucket counts. At the time of pull, `pending=25` — ~0.3% of the
  7995 resolved; not material to the verdict.
- The `edge_pts` field's sign is the right test. The bucket WR CI vs
  the bucket breakeven is a secondary check that says "how confident
  are we the edge is really negative" — and two bands (0.90-0.95 and
  0.98-1.01) are *already* past that bar.

**Verdict.** DEAD — both for the four-band hypothesis and for the
overall strategy. No further data collection is informative; the next
session's job is to wire the kill switch, not to keep sampling.
