# Research Log — Polymarket BTC 5-min

Running lab notebook. Newest entries at the bottom. Each entry: **Question →
Method → Result → Verdict**. Negative results are kept, not deleted; most of
what we've learned so far came from things that didn't work.

**Conventions**
- Numbers quoted here are measured, not estimated. If a figure is an estimate
  it says so.
- "Verdict" is a decision, not a summary: DEAD / PARKED / LIVE / OPEN.
- Instrumentation bugs get their own entries. On this project they have
  repeatedly been the difference between a real finding and a fake one.

---

## Session 1 — 2026-07-20 → 07-21

**Context.** Starting point was `AI-Degen-69/poly-trading-bot`: a working
paper-trading bot for Polymarket's 5-minute "Bitcoin Up or Down" market, whose
README honestly reported losing ~$10 over 54 fills. Goal for the session was to
find out whether *any* version of this is profitable, and to get it running
unattended.

---

### E1 — Replicate bonereaper from measured behaviour

**Question.** The repo's strategy was reverse-engineered from trader
`@bonereaper` (`0xeebd…ba30`) in May. Does the shipped configuration actually
match what he does now?

**Method.** Pulled 13,914 of his live BTC-5m fills (2026-07-18..20) from
`data-api.polymarket.com/activity` and re-derived the parameters instead of
trusting the May write-up.

**Result.** Four of the shipped settings were wrong:

| parameter | shipped | measured |
|---|---|---|
| entry window | last 35s | vol-weighted median entry at t−104s |
| fills per market | 1 | ~24 (median 20) |
| sizing | flat 5 shares | scales with price; 3.6% of trades = 35.4% of his dollars |
| price cap | 0.98 | trades to 0.99 — the cap excluded his largest bucket |

Rebuilt to match. Also built the fee model the repo documented but never
applied: `taker_fee = shares × 0.07 × p × (1−p)`, so breakeven is `p + fee`
(0.90 → 90.63%, 0.98 → 98.14%).

**Verdict.** PARKED. Deployed and collecting, but see E5 — the replica loses
while the original profits, and E6 explains why.

---

### E2 — Binance spot gate

**Question.** These markets resolve on `close ≥ open` of Chainlink BTC/USD.
The outcome is therefore already determined by real price movement. Can we read
that ourselves and refuse trades where the book disagrees with spot?

**Method.** Backtested 584 resolved windows against Binance 1m klines, checking
hit-rate by decision time and bps threshold.

**Result.**

| decision | threshold | coverage | hit rate | 95% CI |
|---|---|---|---|---|
| t−60s | none | 100% | 81.3% | [78.0, 84.3] |
| t−60s | ≥5bps | 30% | **96.0%** | [92.0, 98.1] |
| t−120s | ≥5bps | 25% | 95.1% | [90.2, 97.6] |
| t−180s | ≥5bps | 20% | 88.2% | [81.2, 92.9] |

81% → 96% is real. Signal decays with time remaining, which independently
justifies the 120s window from E1.

**Verdict.** LIVE. Implemented fail-closed (stale feed = no trade). But note
the ceiling: no threshold demonstrates 98.14% with confidence, so the top of
the price ladder (0.98+) is unsupported by the gate — it fixes *direction*, not
*price*.

---

### E3 — Unattended deployment

**Question.** Run without the PC on, reachable from anywhere.

**Method.** Docker + Railway + Turso, with a preflight that fails loudly rather
than running blind.

**Result.** Working. Two failure modes were designed against specifically:
- **Region.** Binance 451s US IPs. In a US region the gate fails closed and the
  bot collects zero fills *while the healthcheck stays green*. Preflight now
  exits non-zero instead.
- **Storage.** No `TURSO_URL` → writes to container-local disk → wiped on every
  redeploy.

Turso needed two fixes to be usable: per-call reconnect cost 1,421 ms/write
(0.7 writes/sec against a ~4/sec requirement) because every call re-ran the
schema; and `libsql` upper-cases SQL keywords in `cursor.description`, so
`action` arrived as `ACTION` and blanked the React dashboard. Connection reuse
plus batched decision writes took it to 0.05 ms/call.

**Verdict.** LIVE.

---

### E4 — Copy-trade follower

**Question.** Three accounts (`@powerwinner`, `@bonereaper`, `@Anon`) are
visibly profitable. Can we follow them?

**Method.** Two parallel simulations over the *same* markets, same accounting,
same $5,000 bankroll, same clock:
- **THEIRS** — their fills at their prices (measures the account's own edge)
- **FOLLOWER** — on detecting each fill, price against the **live CLOB book**,
  walking the ladder as a real taker order would

**Result (70 min, 25 shared markets).**

```
                    THEIRS      FOLLOWER
realized P&L       +$30.39     -$229.98
markets won/lost      18/7          5/20
win rate             72.0%        20.0%
```

Caveat on that gap: ~$158 of it was my own bug (follower charged taker fees,
THEIRS computed gross — fixed). The genuine execution cost is ~$89.

The mechanism is **adverse selection**, and it is not symmetric:

| | their px | our px | slippage |
|---|---|---|---|
| winning token | 0.608 | 0.655 | **+4.72¢** |
| losing token | 0.376 | 0.359 | −1.64¢ |

In the ~10s before we can act, price drifts toward the truth. We pay up on the
side that pays $1 and "save" on the side that expires at $0 — saving on a
worthless ticket is worth nothing. The lag doesn't cost randomly; it costs
precisely on the trades that were going to work.

**Verdict.** DEAD, and structurally so. Code removed (recoverable at commit
`053a4cf`), data archived to `archive/follow_final_*.db`.

---

### E5 — Why the replica loses while the original profits

**Question.** E1's replica is unprofitable; the real bonereaper and powerwinner
are profitable on the same markets. What's different?

**Result.** powerwinner's average entry was **0.608 on the winning token and
0.376 on the losing token — summing to 0.984** — with near-balanced share
counts (5,560 vs 5,415). Testing for it directly: **40% of his two-sided
markets are outright locked** (guaranteed payout > cost); the rest sit
fractionally above.

He is not making directional bets. He is **buying both sides of the same market
for less than $1.00**. The entire margin is 1–2¢ of combined cost below par.

That reframes E4's failure: paying 4.72¢ more on one leg doesn't shave the
edge, it **inverts the arithmetic**. Hence a 72%→20% collapse rather than a
gentle degradation.

Also unresolved but likely material: at an average price near 0.50 he would pay
the *maximum* taker fee (1.75%), which should erase his margin entirely. He is
probably earning **maker rebates** by posting rather than taking. A follower is
a taker by definition.

**Verdict.** Explains E1 and E4. Motivates E6.

---

### E6 — Instrumentation errors (meta)

Kept because on this project the measurement has been wrong more often than the
strategy, and every one of these produced a confident, false conclusion first.

| bug | false conclusion it produced |
|---|---|
| `os.kill(pid, 0)` on Windows calls `TerminateProcess` | would have killed the bot every 2s once fed a real pid |
| resolver used `/markets?slug=` (empty once a market ages out) | positions "stuck open" forever; P&L never realised |
| `size_scale` scaled below the 5-share minimum | silently reverted the ladder to flat sizing |
| shadow read **top-of-book only** | "no liquidity, 10% fill rate" — the book actually had 460 shares at touch, 7,625 within 2 ticks; real rate is ~100% |
| rejected our own sub-minimum orders as `no_depth` | 203 of 204 "liquidity" rejections were 2.5-share intents |
| shadow processed a 7,976-fill backlog | reported 100s "follow latency"; real figure is ~10s |
| follower charged fees, THEIRS gross | 60% of the apparent copy-trade gap |
| `pkill -f` silently no-ops on Windows | **four** tracker instances writing one DB; duplicated rows, resurrected a removed account |
| `tsc --noEmit` doesn't build project references | UI typechecks passed vacuously for hours |

**Standing rule adopted:** before reporting any number as a finding, check
whether the pipeline could have produced it artefactually. Prefer measurements
that can be reconciled two independent ways.

---

## Open threads

1. **Two-sided spread, sourced independently.** *(next up)* Rather than
   following powerwinner into his spread, find markets where
   `UP_ask + DOWN_ask < $1.00` and take both legs ourselves. Same edge, no
   follow lag. Open question: how often such windows exist **after fees**
   (`0.07·p·(1−p)` on each leg), and whether they survive the time it takes to
   fill the second leg. Read-only to measure.
2. **`maker/`** — passive quoting instead of chasing, built from 56,768 of
   powerwinner's fills. Parallel work; the natural successor to E4/E5 given the
   maker-rebate hypothesis.
3. **Main bot sizing.** Still open from E1. At n=20 markets the 95% CI on win
   rate straddled breakeven — needs ~200 resolved markets before the bucket
   table can return a verdict. Deliberately unchanged so the sample stays clean.

## Current state

| component | status |
|---|---|
| `bot/` + Railway deploy | LIVE, collecting |
| `maker/` | in progress (parallel) |
| `follow/` | removed — commit `053a4cf`, data in `archive/` |
| local dashboards | stopped |
