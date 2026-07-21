# Repo split: taker and maker into two independent repos

**Date:** 2026-07-21
**Status:** approved, ready for implementation planning

---

## 1. Why

One repo currently holds two unrelated trading strategies:

- **taker** — crosses the spread, buys near-certainties at 0.80–0.99, gated on a
  Binance spot signal. Pays fees on every fill.
- **maker** — rests bids on both outcomes, earns the spread, holds to
  resolution. Pays no taker fee.

They share almost nothing. The taker has zero imports from the maker; the maker
imports exactly two taker modules (`config.py`, `markets.py`, ~220 lines). They
have different risk models, different metrics, and different reasons to fail.
Keeping them together means every change risks the other, and neither repo can
be reasoned about — or handed to anyone — on its own.

## 2. Goals

1. Two independent GitHub repos, each self-contained and separately deployable.
2. **Identical structure and naming** between them, so the only visible
   difference is strategy-specific. Same folder names, same file names, same
   dashboard shape.
3. Each repo carries its **own research** in English and Hebrew, kept current
   automatically — including when work happens in a different session or with a
   different agent.
4. Both running online (Railway), reachable when the laptop is off.

## 3. Non-goals

- Migrating maker commit history (see §4).
- Building a shared library package for the ~220 lines of common code.
- Changing either strategy's behaviour. This is a structural migration only;
  any strategy change is a separate piece of work with its own fresh data run.
- Resolving the inherited-research provenance question (§7) — pinned, not
  blocking.

## 4. Decisions

| Decision | Choice | Why |
|---|---|---|
| Existing repo | Becomes the **taker** | It began as the taker; it holds `bot/`, `ui/`, `scripts/`, `deploy/` and the live Railway link. Repurposing avoids any taker downtime. |
| Maker history | **Fresh `git init`** | The maker is ~3 commits old and those commits also touch `bot/`, `server/` and `ui/`. A filtered history would show commits that were mostly about something else. `RESEARCH_LOG.md` already preserves the narrative. |
| Shared code | **Duplicated** into both | `config.py` + `markets.py`, ~220 lines, stable. Versioning a package across two repos costs more than the duplication. |
| Research | **Fully self-contained per repo** | Independence means a repo tells its whole story alone. Only the market spec and fee model are duplicated. |
| Auto-update | **AGENTS.md + pre-commit hook** | Instructions alone degrade silently; the hook does not care which agent is driving. |
| Deployment | **One Railway project, two services** | Shared billing view and one dashboard list, while each service keeps its own repo, volume, domain and env vars. |

## 5. Target structure

Holder folder (not a repo):
`C:\Users\Tiger\Agents\Projects\AI Trading\`

Both repos take the same shape:

```
polymarket-<taker|maker>/
├── strategy/            engine  (was bot/ and maker/ respectively)
│   ├── config.py        knobs + .env loading
│   ├── markets.py       live 5-min market discovery
│   ├── store.py         SQLite logger
│   ├── main.py          event loop
│   └── ...              strategy-specific modules
├── server/
│   ├── dashboard.py     FastAPI app + /api/state
│   └── kanban.py        kanban page
├── research/            see §6
├── deploy/
│   ├── run_service.py   container entrypoint + preflight
│   └── DEPLOY.md
├── scripts/
│   └── setup-hooks.sh   installs .githooks
├── .githooks/pre-commit
├── AGENTS.md
├── Dockerfile
├── railway.toml
└── requirements.txt
```

Strategy-specific contents:

| | taker `strategy/` | maker `strategy/` |
|---|---|---|
| decision | `strategy.py` (entry band, gate) | `quotes.py` (where to rest) |
| execution | `orders.py` (FOK) | `fills.py` (queue-aware model) |
| economics | `fees.py` (taker fee) | `kpi.py` (spread vs adverse selection) |
| signal | `spot.py` (Binance gate) | — |
| risk | `risk.py` (caps, kill switch) | inventory balance in `quotes.py` |

The taker additionally keeps `ui/` (React) and the wallet `scripts/`. The maker
has no React app — its dashboard is self-contained HTML.

**Renaming `bot/` → `strategy/` and `maker/` → `strategy/` is what makes the two
structurally identical.** It rewrites imports in both repos; each must be
verified running afterwards.

## 6. Research model

Five files per repo, all under `research/`:

```
RESEARCH_LOG.md          full lab notebook
RESEARCH_SUMMARY.md      dated bullets, one per concrete thing done
he_RESEARCH_LOG.md       Hebrew mirror of the log
he_RESEARCH_SUMMARY.md   Hebrew mirror of the summary
market_spec.md           market mechanics, duplicated into both repos —
                         WRITTEN FRESH from our own verified measurements,
                         not copied from the inherited file (see §7)
```

`market_spec.md` covers only mechanics we independently confirmed against live
data during this work: the `crypto_fees_v2` fee formula
(`shares × 0.07 × p × (1−p)`, verified against real fills), the 0.01 tick size,
`close >= open` resolution on the Chainlink stream, the ~60–120s resolution lag,
and the `/events?slug=` vs `/markets?slug=` endpoint behaviour (measured
584/584). It does not reuse the inherited document's text.

Existing conventions are kept unchanged:

- Entry shape: **Question → Method → Result → Verdict**
- Verdict is a decision, not a summary: `DEAD` / `PARKED` / `LIVE` / `OPEN`
- Negative results are retained, never deleted
- Numbers are measured, not estimated; estimates say so explicitly
- Instrumentation bugs get their own entries

The current Session 1 log is **split by topic**: taker findings (spot gate, fee
model, sizing ladder, resolver bug) to the taker repo; maker findings
(powerwinner analysis, queue-aware fill model, balance finding) to the maker
repo; shared findings (market spec, fee formula) duplicated into both.

English and Hebrew are updated **in the same commit**. The Hebrew is a mirror of
the English, not an independent document.

## 7. Pinned, not blocking

`research/bonereader_analysis.md`, `bonereader_public_intel.md`,
`btc_5min_market_spec.md` and `fetch_*.py` were inherited from
`AI-Degen-69/poly-trading-bot` and were not authored here. They are **excluded
from both new repos** until attribution/licensing is checked. Options then:
keep with attribution, rewrite from our own measured data, or drop.

Our own measured work — `bonereaper_live_2026-07-20.md`, `RESEARCH_LOG*.md`,
`he_RESEARCH.md`, and the powerwinner analysis — is unaffected and carries over.

## 8. Auto-update mechanism

**`AGENTS.md`** (each repo) states:
- the rule: any change to `strategy/` or `server/` updates `research/` in the
  same commit
- the entry format and verdict vocabulary
- that EN and HE are updated together
- where the hook lives and how to install it

**`.githooks/pre-commit`** — blocks a commit when:
```
staged files touch strategy/ or server/
AND no staged file is under research/
```
The message names the files to update. `git commit --no-verify` remains
available for typo fixes and non-substantive changes.

**`scripts/setup-hooks.sh`** runs `git config core.hooksPath .githooks`. Hooks
are not installed by clone, so this is documented as a first-run step in each
README and referenced from `AGENTS.md`.

Rationale: an instruction file is advisory and degrades silently — a gap only
becomes visible weeks later. The hook fails closed and is agent-agnostic.

## 9. Railway deployment

Existing project renamed to **AI Trading**, containing two services:

| | taker service | maker service |
|---|---|---|
| repo | `polymarket-taker` | `polymarket-maker` |
| status | **existing, untouched** | new |
| volume | `/data` (existing) | `/data` (new) |
| DB var | `POLYBOT_DB=/data/trades.db` | `MAKER_DB=/data/maker.db` |
| region | `europe-west` | `europe-west` |
| health | `/api/health` | `/api/health` |
| domain | existing | new |

Both keep the preflight that fails loudly on a US region (Binance returns 451 to
US IPs; both strategies depend on that feed) and on missing persistent storage.

The maker's Dockerfile omits the Node build stage — no React app — so it is a
single Python stage and builds faster.

Wallet credentials stay **placeholders** on both hosted services. Neither
simulation constructs a CLOB client, so a real key would add risk for no
benefit.

## 10. Migration order

Each step ends in a verification gate. The taker is never offline.

1. Create `AI Trading/`; move the repo in; rename to `polymarket-taker`.
   **Gate:** `git status` clean and `git log` intact at the new path; the
   running taker/maker processes are stopped cleanly first so nothing holds a
   file handle mid-move.
2. Restructure `bot/` → `strategy/`; fix imports. **Gate:** taker runs locally,
   dashboard serves, decisions still log.
3. Rename the GitHub repo. **Gate:** Railway still builds and deploys; health
   is 200. *Taker is safe from this point.*
4. Create `polymarket-maker`; copy `maker/` → `strategy/` plus the two shared
   modules; fix imports. **Gate:** maker runs locally, kanban serves, fills
   still record.
5. Split research into both repos; write both `AGENTS.md`; install hooks.
   **Gate:** a code-only commit is refused by the hook in both repos.
6. Push maker; add the Railway service, volume and variables. **Gate:**
   preflight passes, health 200, fills appear in the hosted DB.
7. Remove `maker/` and `server/maker_dashboard.py` from the taker repo.
   **Gate:** taker still builds and deploys.

Step 7 runs last so a working copy of the maker always exists somewhere.

## 11. Risks

| Risk | Mitigation |
|---|---|
| Import rename breaks a bot silently | Verification gate after each restructure; run locally and confirm rows still write |
| Railway loses the taker on repo rename | GitHub redirects renamed repos; step 3 has its own gate before anything else changes |
| Maker deployed with an unvalidated fill model | Documented biases in `fills.py`; sample-size tracker on the dashboard; results treated as an upper bound |
| Hook not installed on a fresh clone | `setup-hooks.sh` referenced in README and `AGENTS.md`; hook lives in-repo |
| Duplicated shared code drifts | Only `config.py`/`markets.py`; both are stable, and drift is acceptable since each repo owns its own behaviour |

## 12. Success criteria

- Two repos, each cloneable and runnable alone with no reference to the other.
- `ls` of both repos shows the same top-level names.
- Both dashboards online with their own domains, surviving a laptop shutdown.
- A commit touching only `strategy/` is refused by the hook in both repos.
- Each `research/` holds four EN/HE files that describe only that strategy.
