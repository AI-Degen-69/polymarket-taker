# Repo Split Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split one repo containing two trading strategies into two independent, identically-structured repos, each self-contained and separately deployed on Railway.

**Architecture:** The existing repo becomes the taker (keeps history and its live Railway service). The maker gets a fresh repo. Both are restructured so `bot/` and `maker/` each become `strategy/`, making their top-level layout identical. Shared code (`config.py`, `markets.py`) is duplicated rather than packaged. A pre-commit hook enforces research updates in both.

**Tech Stack:** Python 3.11/3.12, FastAPI, uvicorn, SQLite, Docker, Railway, git.

## Global Constraints

- Holder folder: `C:\Users\Tiger\Agents\Projects\AI Trading\` — a plain folder, not a repo.
- Repo names: `polymarket-taker`, `polymarket-maker`. GitHub owner: `AI-Degen-69`.
- Engine package in BOTH repos is named `strategy/`. Never `bot/` or `maker/`.
- Dashboard modules in BOTH repos: `server/dashboard.py` and `server/kanban.py`.
- Research is 5 files per repo: `RESEARCH_LOG.md`, `RESEARCH_SUMMARY.md`, `he_RESEARCH_LOG.md`, `he_RESEARCH_SUMMARY.md`, `market_spec.md`.
- English and Hebrew research are updated in the SAME commit.
- Verdict vocabulary in research: `DEAD` / `PARKED` / `LIVE` / `OPEN`.
- Inherited files `bonereader_analysis.md`, `bonereader_public_intel.md`, `btc_5min_market_spec.md`, `fetch_bonereader.py`, `fetch_full.py`, `fetch_more.py`, `fetch_resolutions.py`, `analyze.py` are EXCLUDED from both new repos pending provenance review.
- Railway region for both services: `europe-west`. Never a US region (Binance returns HTTP 451 to US IPs).
- Hosted wallet credentials stay placeholders. Never deploy a real `PRIVATE_KEY`.
- The taker service must never go offline during migration.
- Windows/Git Bash: use `MSYS_NO_PATHCONV=1` before any command passing a `/`-prefixed path to a non-MSYS binary (e.g. `railway volume add --mount-path /data`).

---

## File Structure

**polymarket-taker/** (from existing repo)
| Path | Responsibility |
|---|---|
| `strategy/config.py` | knobs + `.env` loading (renamed from `bot/config.py`) |
| `strategy/markets.py` | live market discovery |
| `strategy/book.py` | top-of-book reader |
| `strategy/spot.py` | Binance gate |
| `strategy/strategy_rules.py` | `decide()` — renamed from `bot/strategy.py` to avoid `strategy.strategy` |
| `strategy/fees.py` `orders.py` `risk.py` `resolver.py` `store.py` `main.py` | unchanged roles |
| `server/dashboard.py` | FastAPI app |
| `server/kanban.py` | kanban page (renamed from `taker_kanban.py`) |
| `research/` | 5 files, taker content |
| `.githooks/pre-commit`, `scripts/setup-hooks.sh`, `AGENTS.md` | new |

**polymarket-maker/** (new)
| Path | Responsibility |
|---|---|
| `strategy/config.py` | maker knobs |
| `strategy/markets.py` | copied from taker |
| `strategy/net_config.py` | CLOB/gamma hosts, copied from taker `config.py` |
| `strategy/quotes.py` `fills.py` `kpi.py` `store.py` `main.py` | maker engine |
| `server/dashboard.py` | FastAPI app (renamed from `maker_dashboard.py`) |
| `server/kanban.py` | kanban page (extracted from the `PAGE` constant) |
| `research/` | 5 files, maker content |
| `deploy/run_service.py`, `Dockerfile`, `railway.toml` | new, Python-only |

---

### Task 1: Stop processes and create the holder folder

**Files:**
- Create: `C:\Users\Tiger\Agents\Projects\AI Trading\` (directory)

**Interfaces:**
- Consumes: nothing
- Produces: holder folder path used by all later tasks

- [ ] **Step 1: Stop every running bot and dashboard**

```powershell
Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
  Where-Object { $_.CommandLine -match 'maker\.main|maker_dashboard|bot\.main|server\.dashboard' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

- [ ] **Step 2: Verify none remain**

Run:
```powershell
(Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
  Where-Object { $_.CommandLine -match 'maker\.main|maker_dashboard|bot\.main|server\.dashboard' }).Count
```
Expected: `0`

Note: leave `follow.main` / `follow.dashboard` running — that is a separate user project.

- [ ] **Step 3: Create the holder folder**

```bash
mkdir -p "/c/Users/Tiger/Agents/Projects/AI Trading"
ls -d "/c/Users/Tiger/Agents/Projects/AI Trading"
```
Expected: the path prints.

- [ ] **Step 4: Commit any outstanding work first**

```bash
cd "/c/Users/Tiger/Agents/Projects/claude-poly-bot/poly-trading-bot"
git status --short
git add -A && git commit -m "chore: checkpoint before repo split" || echo "nothing to commit"
git log --oneline -1
```
Expected: clean tree, commit hash printed.

---

### Task 2: Move the repo and rename `bot/` to `strategy/`

**Files:**
- Move: `claude-poly-bot/poly-trading-bot/` → `AI Trading/polymarket-taker/`
- Rename: `bot/` → `strategy/`, `bot/strategy.py` → `strategy/strategy_rules.py`
- Modify: all files listed in Step 3 below

**Interfaces:**
- Consumes: holder folder from Task 1
- Produces: `strategy` package importable as `from strategy.config import load`; `decide()` lives in `strategy.strategy_rules`

- [ ] **Step 1: Move the repo with history**

```bash
cd "/c/Users/Tiger/Agents/Projects"
mv "claude-poly-bot/poly-trading-bot" "AI Trading/polymarket-taker"
cd "AI Trading/polymarket-taker"
git log --oneline -1 && git status --short
```
Expected: last commit prints, tree clean.

- [ ] **Step 2: Rename the package with git so history follows**

```bash
git mv bot strategy
git mv strategy/strategy.py strategy/strategy_rules.py
git mv server/taker_kanban.py server/kanban.py
ls strategy/
```
Expected: `strategy_rules.py` present, no `strategy.py`.

Rationale: `strategy/strategy.py` would import as `strategy.strategy`, which is confusing and shadows the package name.

- [ ] **Step 3: Rewrite every import**

```bash
python - <<'EOF'
import pathlib, re
files = list(pathlib.Path('strategy').rglob('*.py')) + \
        list(pathlib.Path('server').rglob('*.py')) + \
        list(pathlib.Path('deploy').rglob('*.py'))
for p in files:
    s = p.read_text(encoding='utf-8'); o = s
    s = s.replace('from bot.strategy import', 'from strategy.strategy_rules import')
    s = s.replace('from bot import', 'from strategy import')
    s = s.replace('from bot.', 'from strategy.')
    s = s.replace('import bot.', 'import strategy.')
    s = s.replace('"-m", "bot.main"', '"-m", "strategy.main"')
    s = s.replace('from server.taker_kanban import', 'from server.kanban import')
    if s != o:
        p.write_text(s, encoding='utf-8'); print('rewrote', p)
EOF
```
Expected: prints each rewritten file.

- [ ] **Step 4: Rewrite shell scripts and Dockerfile**

```bash
sed -i 's/-m bot\.main/-m strategy.main/g' scripts/run_live.sh scripts/run_paper.sh
sed -i 's|COPY bot/     /app/bot/|COPY strategy/ /app/strategy/|' Dockerfile
sed -i 's|bot/main\.py|strategy/main.py|g' Dockerfile
grep -rn "bot\." scripts/*.sh Dockerfile deploy/run_service.py | grep -v "^Binary" || echo "no stale refs"
```
Expected: `no stale refs`.

- [ ] **Step 5: Verify no `bot` imports remain**

```bash
grep -rn "from bot\|import bot\b\|bot\.main" --include=*.py --include=*.sh --include=Dockerfile . | grep -v ".venv" || echo "CLEAN"
```
Expected: `CLEAN`

- [ ] **Step 6: Verify the taker still imports and runs**

```bash
.venv/Scripts/python.exe -c "import strategy.main, strategy.strategy_rules, server.dashboard; print('imports OK')"
```
Expected: `imports OK`

- [ ] **Step 7: Verify preflight passes**

```bash
.venv/Scripts/python.exe deploy/run_service.py --preflight
```
Expected: ends with `[preflight] all checks passed`

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "refactor: rename bot/ to strategy/ for cross-repo consistency

Both repos will expose the same top-level layout, so the only difference
between them is strategy-specific. bot/strategy.py becomes
strategy/strategy_rules.py to avoid the confusing strategy.strategy import."
```

---

### Task 3: Rename the GitHub repo and confirm Railway still deploys

**Files:**
- Modify: git remote URL

**Interfaces:**
- Consumes: renamed local repo from Task 2
- Produces: `polymarket-taker` on GitHub, still building on Railway

- [ ] **Step 1: Rename on GitHub**

```bash
gh repo rename polymarket-taker --repo AI-Degen-69/claude-poly-bot --yes
```
Expected: confirmation of the new name.

- [ ] **Step 2: Point the local remote at the new name**

```bash
git remote set-url origin https://github.com/AI-Degen-69/polymarket-taker.git
git remote -v
```
Expected: both fetch and push show `polymarket-taker`.

- [ ] **Step 3: Push**

```bash
git push
git log --oneline -1
```
Expected: push succeeds.

- [ ] **Step 4: Wait for Railway and verify health**

```bash
sleep 180
curl -s -m 25 -o /dev/null -w "health HTTP:%{http_code}\n" https://claude-poly-bot-production.up.railway.app/api/health
```
Expected: `HTTP:200`

- [ ] **Step 5: Verify the deploy used the new code**

```bash
railway logs --deployment --lines 30 | grep -E "preflight|bot\] starting|dashboard\] serving"
```
Expected: `[preflight] all checks passed` and `[bot] starting`.

If health is not 200 after 5 minutes, run `railway logs --build --lines 40`, fix, and repeat before continuing. **The taker is the live system; do not proceed until it is green.**

---

### Task 4: Create the maker repo skeleton

**Files:**
- Create: `AI Trading/polymarket-maker/` with `strategy/`, `server/`, `research/`, `deploy/`, `scripts/`, `.githooks/`

**Interfaces:**
- Consumes: maker sources from the taker repo
- Produces: `polymarket-maker` local repo

- [ ] **Step 1: Create and initialise**

```bash
cd "/c/Users/Tiger/Agents/Projects/AI Trading"
mkdir -p polymarket-maker/{strategy,server,research,deploy,scripts,.githooks}
cd polymarket-maker
git init -b main
```
Expected: empty repo on `main`.

- [ ] **Step 2: Copy the maker engine**

```bash
T=../polymarket-taker
cp $T/maker/config.py   strategy/config.py
cp $T/maker/quotes.py   strategy/quotes.py
cp $T/maker/fills.py    strategy/fills.py
cp $T/maker/kpi.py      strategy/kpi.py
cp $T/maker/store.py    strategy/store.py
cp $T/maker/main.py     strategy/main.py
touch strategy/__init__.py server/__init__.py
cp $T/maker/markets.py strategy/markets.py 2>/dev/null || cp $T/strategy/markets.py strategy/markets.py
ls strategy/
```
Expected: 8 files including `markets.py` and `__init__.py`.

- [ ] **Step 3: Create `strategy/net_config.py` (the shared host/env loader)**

```python
"""Network + env config shared with the taker repo.

Duplicated deliberately rather than packaged: ~120 lines of stable code, and a
shared library across two repos costs more in version management than the
duplication costs in drift. See docs spec 2026-07-21 section 4.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class NetConfig:
    clob_host: str = os.environ.get("CLOB_HOST", "https://clob.polymarket.com")
    gamma_host: str = os.environ.get("GAMMA_HOST", "https://gamma-api.polymarket.com")
    series_slug: str = "btc-up-or-down-5m"


def load_net() -> NetConfig:
    return NetConfig()
```

Write this to `strategy/net_config.py`.

- [ ] **Step 4: Repoint maker imports**

```bash
python - <<'EOF'
import pathlib
for p in pathlib.Path('strategy').rglob('*.py'):
    s = p.read_text(encoding='utf-8'); o = s
    s = s.replace('from bot.config import load as load_bot_cfg',
                  'from strategy.net_config import load_net as load_bot_cfg')
    s = s.replace('from bot.markets import', 'from strategy.markets import')
    s = s.replace('from maker import', 'from strategy import')
    s = s.replace('from maker.', 'from strategy.')
    if s != o:
        p.write_text(s, encoding='utf-8'); print('rewrote', p)
EOF
grep -rn "from bot\|from maker" strategy/ || echo "CLEAN"
```
Expected: `CLEAN`

- [ ] **Step 5: Verify imports**

```bash
"../polymarket-taker/.venv/Scripts/python.exe" -c "import strategy.main, strategy.quotes, strategy.fills, strategy.kpi; print('maker imports OK')"
```
Expected: `maker imports OK`

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: maker engine extracted into its own repo

Fresh history: the maker's ~3 commits in the old repo were interleaved with
taker work, so a filtered history would misrepresent them. The narrative is
preserved in research/RESEARCH_LOG.md."
```

---

### Task 5: Split the maker dashboard into `dashboard.py` + `kanban.py`

**Files:**
- Create: `polymarket-maker/server/dashboard.py`, `polymarket-maker/server/kanban.py`

**Interfaces:**
- Consumes: `strategy.kpi.report()`, `strategy.store.get_live_state()`
- Produces: `app` (FastAPI) in `server.dashboard`; `PAGE` (str) in `server.kanban`

- [ ] **Step 1: Extract the HTML into `server/kanban.py`**

```bash
T=../polymarket-taker
python - <<'EOF'
import pathlib, re
src = pathlib.Path('../polymarket-taker/server/maker_dashboard.py').read_text(encoding='utf-8')
start = src.index('PAGE = r"""')
end = src.index('"""', start + 11) + 3
page = src[start:end]
pathlib.Path('server/kanban.py').write_text(
    '"""Maker kanban page. Same pipeline shape as the taker repo\'s '
    'server/kanban.py,\nwith maker-specific lanes and metrics."""\n'
    'from __future__ import annotations\n\n' + page + '\n', encoding='utf-8')
rest = (src[:start] + src[end:]).replace(
    'from maker import kpi, store', 'from strategy import kpi, store').replace(
    'from maker.config import load as load_cfg', 'from strategy.config import load as load_cfg')
rest = rest.replace('@app.get("/", response_class=HTMLResponse)\ndef index():\n    return PAGE',
                    'from server.kanban import PAGE\n\n\n@app.get("/", response_class=HTMLResponse)\ndef index():\n    return PAGE')
pathlib.Path('server/dashboard.py').write_text(rest, encoding='utf-8')
print('split done')
EOF
```
Expected: `split done`

- [ ] **Step 2: Verify both modules import and the page is non-empty**

```bash
"../polymarket-taker/.venv/Scripts/python.exe" -c "
from server.kanban import PAGE
import server.dashboard as d
assert len(PAGE) > 2000, len(PAGE)
print('kanban chars:', len(PAGE))
print('routes:', [r.path for r in d.app.routes if getattr(r,'path','').startswith(('/api','/'))][:5])
"
```
Expected: `kanban chars:` > 2000 and routes include `/api/state`.

- [ ] **Step 3: Run it and check it serves**

```bash
"../polymarket-taker/.venv/Scripts/python.exe" -m uvicorn server.dashboard:app --host 127.0.0.1 --port 8790 &
sleep 10
curl -s -m 15 -o /dev/null -w "root HTTP:%{http_code}\n" http://127.0.0.1:8790/
curl -s -m 15 -o /dev/null -w "health HTTP:%{http_code}\n" http://127.0.0.1:8790/api/health
```
Expected: both `HTTP:200`

- [ ] **Step 4: Stop the test server**

```powershell
Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
  Where-Object { $_.CommandLine -match 'port 8790' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: split maker dashboard into server/dashboard.py + server/kanban.py

Matches the taker repo's module names so both repos expose the same layout."
```

---

### Task 6: Maker deploy files

**Files:**
- Create: `polymarket-maker/Dockerfile`, `railway.toml`, `requirements.txt`, `deploy/run_service.py`, `.gitignore`

**Interfaces:**
- Consumes: `strategy.main`, `server.dashboard:app`
- Produces: a container that runs the maker bot and serves its dashboard on `$PORT`

- [ ] **Step 1: Write `requirements.txt`**

```
requests>=2.31
websocket-client>=1.7
python-dotenv>=1.0
fastapi>=0.110
uvicorn[standard]>=0.27
```

Note: no `py-clob-client-v2` and no `web3` — the maker never constructs a CLOB client.

- [ ] **Step 2: Write `Dockerfile`**

```dockerfile
# Maker sim: single Python stage. No Node build -- the dashboard is
# self-contained HTML, unlike the taker's React UI.
#
# DEPLOY IN A NON-US REGION. Binance returns 451 to US IPs and the maker's
# market discovery and the taker's gate both depend on that feed.
FROM python:3.12-slim
WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY strategy/ /app/strategy/
COPY server/   /app/server/
COPY deploy/   /app/deploy/

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV PORT=8788

EXPOSE 8788
CMD ["python", "deploy/run_service.py"]
```

- [ ] **Step 3: Write `railway.toml`**

```toml
[build]
builder = "dockerfile"
dockerfilePath = "Dockerfile"

[deploy]
healthcheckPath = "/api/health"
healthcheckTimeout = 300
restartPolicyType = "ON_FAILURE"
restartPolicyMaxRetries = 10
```

- [ ] **Step 4: Write `deploy/run_service.py`**

```python
#!/usr/bin/env python3
"""Container entrypoint: supervise the maker sim + serve its dashboard.

Preflight fails loudly on the two silent killers: a US region (Binance 451s US
IPs) and missing persistent storage (a container-local DB vanishes on redeploy).
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PORT = int(os.environ.get("PORT", "8788"))
RESTART_BACKOFF = 5.0


def preflight() -> None:
    problems: list[str] = []

    db = os.environ.get("MAKER_DB")
    if not db:
        print("[preflight] WARNING: MAKER_DB unset -- using a container-local "
              "maker.db, which is LOST on redeploy unless a volume is mounted.",
              flush=True)
    else:
        try:
            from strategy import store
            with store.db() as c:
                c.execute("CREATE TABLE IF NOT EXISTS _pf (x INTEGER)")
                c.execute("DELETE FROM _pf")
            print(f"[preflight] volume OK: {db} is writable", flush=True)
        except Exception as e:
            problems.append(
                f"MAKER_DB={db} is not writable ({e}). Mount a Railway volume "
                f"at that directory (Settings -> Volumes -> /data)."
            )

    import requests
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price",
                         params={"symbol": "BTCUSDT"}, timeout=15)
        if r.status_code == 200:
            print(f"[preflight] binance OK: BTC={r.json().get('price')}", flush=True)
        else:
            problems.append(
                f"Binance returned HTTP {r.status_code} (451/403 = geo-blocked "
                f"region, e.g. US). Redeploy in a non-US region."
            )
    except Exception as e:
        problems.append(f"Binance unreachable: {e}")

    try:
        from strategy.markets import fetch_live_market
        from strategy.net_config import load_net
        n = load_net()
        m = fetch_live_market(n.gamma_host, n.series_slug)
        print(f"[preflight] polymarket OK: live market={m.market_slug if m else None}",
              flush=True)
    except Exception as e:
        problems.append(f"Polymarket unreachable: {type(e).__name__}: {e}")

    if problems:
        print("\n[preflight] FAILED:", flush=True)
        for p in problems:
            print(f"  - {p}", flush=True)
        if "--preflight" not in sys.argv:
            print("[preflight] sleeping 30s before exit to avoid a hot restart loop",
                  flush=True)
            time.sleep(30)
        sys.exit(1)
    print("[preflight] all checks passed", flush=True)


def run_bot() -> None:
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    while True:
        print("[bot] starting (maker sim)", flush=True)
        proc = subprocess.Popen([sys.executable, "-m", "strategy.main"],
                                cwd=str(ROOT), env=env)
        code = proc.wait()
        print(f"[bot] exited code={code}; restarting in {RESTART_BACKOFF}s", flush=True)
        time.sleep(RESTART_BACKOFF)


def main() -> None:
    if "--preflight" in sys.argv:
        preflight()
        return
    preflight()
    threading.Thread(target=run_bot, name="bot", daemon=True).start()
    import uvicorn
    print(f"[dashboard] serving on 0.0.0.0:{PORT}", flush=True)
    uvicorn.run("server.dashboard:app", host="0.0.0.0", port=PORT, log_level="info")


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Write `.gitignore`**

```
.env
.env.*
!.env.example
maker.db
maker.db-*
*.pid
logs/
.venv/
__pycache__/
*.pyc
```

- [ ] **Step 6: Run preflight locally**

```bash
MAKER_DB="C:/tmp/mk_pf.db" "../polymarket-taker/.venv/Scripts/python.exe" deploy/run_service.py --preflight
```
Expected: ends with `[preflight] all checks passed`

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat: maker deploy config

Single-stage Python image (no Node -- the dashboard is plain HTML). Preflight
fails loudly on a US region and on an unwritable MAKER_DB."
```

---

### Task 7: Research split, AGENTS.md and the enforcing hook — both repos

**Files:**
- Create in BOTH repos: `research/RESEARCH_LOG.md`, `research/RESEARCH_SUMMARY.md`, `research/he_RESEARCH_LOG.md`, `research/he_RESEARCH_SUMMARY.md`, `research/market_spec.md`, `AGENTS.md`, `.githooks/pre-commit`, `scripts/setup-hooks.sh`
- Delete from taker: the inherited research files

**Interfaces:**
- Consumes: existing `research/RESEARCH_LOG.md`, `RESEARCH_LOG_SUMMARY.md`, `he_RESEARCH.md` in the taker repo
- Produces: the hook, which later commits in both repos must satisfy

- [ ] **Step 1: Write `.githooks/pre-commit` (identical in both repos)**

```bash
#!/usr/bin/env bash
# Refuse a commit that changes strategy or server code without also updating
# research/. Research that is only written when someone remembers gets written
# rarely; this makes the rule agent-agnostic.
#
# Escape hatch:  git commit --no-verify   (typos, formatting, non-substantive)
set -e

staged=$(git diff --cached --name-only)
code=$(echo "$staged" | grep -E '^(strategy|server)/' || true)
research=$(echo "$staged" | grep -E '^research/' || true)

if [ -n "$code" ] && [ -z "$research" ]; then
  cat >&2 <<'MSG'

  COMMIT BLOCKED — code changed but research/ did not.

  This repo keeps a running lab notebook. Add an entry describing what you
  changed and what you learned, in BOTH languages:

      research/RESEARCH_LOG.md        Question -> Method -> Result -> Verdict
      research/RESEARCH_SUMMARY.md    one dated bullet
      research/he_RESEARCH_LOG.md     Hebrew mirror
      research/he_RESEARCH_SUMMARY.md Hebrew mirror

  Verdict vocabulary: DEAD / PARKED / LIVE / OPEN

  Non-substantive change (typo, formatting)?  git commit --no-verify

MSG
  exit 1
fi
exit 0
```

- [ ] **Step 2: Write `scripts/setup-hooks.sh` (identical in both repos)**

```bash
#!/usr/bin/env bash
# Git does not install hooks on clone. Run this once after cloning.
set -e
cd "$(dirname "$0")/.."
git config core.hooksPath .githooks
chmod +x .githooks/* 2>/dev/null || true
echo "hooks installed: $(git config core.hooksPath)"
```

- [ ] **Step 3: Install and verify the hook BLOCKS a code-only commit**

```bash
bash scripts/setup-hooks.sh
echo "# test" >> strategy/config.py
git add strategy/config.py
git commit -m "test: should be blocked" && echo "HOOK FAILED TO BLOCK" || echo "hook blocked correctly"
git reset HEAD strategy/config.py && git checkout strategy/config.py
```
Expected: `hook blocked correctly`

- [ ] **Step 4: Verify the hook ALLOWS a commit that includes research**

```bash
echo "# test" >> strategy/config.py
mkdir -p research && echo "- test entry" >> research/RESEARCH_SUMMARY.md
git add strategy/config.py research/RESEARCH_SUMMARY.md
git commit -m "test: should pass" && echo "hook allowed correctly"
git reset --hard HEAD~1
```
Expected: `hook allowed correctly`

- [ ] **Step 5: Write `AGENTS.md` (both repos, strategy name substituted)**

```markdown
# AGENTS.md — <polymarket-taker | polymarket-maker>

## What this repo is

<One paragraph: which strategy, what it does, what it is measured against.>

## Non-negotiable: keep the research log current

Any commit that touches `strategy/` or `server/` MUST also update `research/`
in the same commit. A pre-commit hook enforces this.

Run once after cloning:

    bash scripts/setup-hooks.sh

Update all four files together:

| file | content |
|---|---|
| `research/RESEARCH_LOG.md` | Question -> Method -> Result -> Verdict |
| `research/RESEARCH_SUMMARY.md` | one dated bullet per concrete thing done |
| `research/he_RESEARCH_LOG.md` | Hebrew mirror of the log |
| `research/he_RESEARCH_SUMMARY.md` | Hebrew mirror of the summary |

Conventions:
- Verdict is a decision, not a summary: `DEAD` / `PARKED` / `LIVE` / `OPEN`
- Negative results are kept, never deleted
- Numbers are measured, not estimated; if a figure is an estimate, say so
- Instrumentation bugs get their own entry — on this project they have
  repeatedly been the difference between a real finding and a fake one
- Hebrew mirrors the English; it is not an independent document

## Layout

    strategy/   engine
    server/     dashboard.py (API) + kanban.py (page)
    research/   the five files above
    deploy/     container entrypoint + preflight

The sibling repo uses the SAME layout. Keep it that way — the only difference
between the repos should be strategy-specific.

## Safety

- Simulation only. Never place a real order.
- Hosted credentials are placeholders; never deploy a real `PRIVATE_KEY`.
- Deploy only to a non-US region: Binance returns HTTP 451 to US IPs.
- Changing strategy parameters invalidates the current sample. Archive the DB
  and start a fresh run rather than mixing configs in one dataset.
```

- [ ] **Step 6: Split the research content**

In the **taker** repo, `research/RESEARCH_LOG.md` keeps: the Windows port, the `os.kill` liveness bug, the bonereaper parameter rebuild, the fee model, the Binance spot gate backtest, the sizing-ladder floor bug, the resolver `/markets` vs `/events` bug, the Turso read explosion, and the volume migration.

In the **maker** repo, `research/RESEARCH_LOG.md` takes: the powerwinner analysis (maker not predictor; 41.4% market win rate; +$39,884 gross vs −$32,501 with taker fees), the trade-tape `side` ambiguity that forced the book-delta fill model, the queue-aware design and its documented biases, the duplicate-process contamination, and the balance finding (hedged +$409 vs unbalanced −$848).

Both get `research/market_spec.md` written fresh from verified measurements only: the `crypto_fees_v2` formula `shares × 0.07 × p × (1−p)`, tick size 0.01, `close >= open` resolution on the Chainlink stream, 60–120s resolution lag, and `/events?slug=` reliability (584/584).

Delete the inherited files from the taker repo:

```bash
cd "/c/Users/Tiger/Agents/Projects/AI Trading/polymarket-taker"
git rm research/bonereader_analysis.md research/bonereader_public_intel.md \
       research/btc_5min_market_spec.md research/fetch_bonereader.py \
       research/fetch_full.py research/fetch_more.py \
       research/fetch_resolutions.py research/analyze.py
git rm research/RESEARCH_LOG_SUMMARY.md research/he_RESEARCH.md
ls research/
```
Expected: only the five approved files remain.

- [ ] **Step 7: Commit both repos**

```bash
git add -A && git commit -m "docs: self-contained EN+HE research, AGENTS.md and enforcing hook

Inherited third-party research removed pending provenance review; market_spec
rewritten from our own verified measurements."
```

---

### Task 8: Push the maker and deploy it on Railway

**Files:**
- Modify: Railway project (rename), add maker service

**Interfaces:**
- Consumes: `polymarket-maker` repo from Tasks 4–7
- Produces: a second live service

- [ ] **Step 1: Create the GitHub repo and push**

```bash
cd "/c/Users/Tiger/Agents/Projects/AI Trading/polymarket-maker"
gh repo create AI-Degen-69/polymarket-maker --private --source=. --remote=origin --push
git remote -v
```
Expected: repo created, push succeeds.

- [ ] **Step 2: Rename the Railway project**

```bash
cd "../polymarket-taker"
railway status
```
Then rename the project to `AI Trading` in the Railway dashboard (Settings → Project Name). The CLI has no rename command.

- [ ] **Step 3: Add the maker service, pointed at the new repo**

In the Railway dashboard: **New → GitHub Repo → `polymarket-maker`**, inside the `AI Trading` project.

- [ ] **Step 4: Set the region BEFORE first deploy**

Service → Settings → Regions → **europe-west**.

- [ ] **Step 5: Add a volume**

```bash
cd "/c/Users/Tiger/Agents/Projects/AI Trading/polymarket-maker"
railway link --project "AI Trading" --environment production --service polymarket-maker
MSYS_NO_PATHCONV=1 railway volume add --mount-path /data
railway volume list --json
```
Expected: one volume, `mountPath: /data`, `status: Ready`.

`MSYS_NO_PATHCONV=1` is required — Git Bash otherwise rewrites `/data` into a Windows path and the CLI rejects it.

- [ ] **Step 6: Set variables**

```bash
railway variables --set "MAKER_DB=/data/maker.db"
railway variables --json | python -c "import json,sys; v=json.load(sys.stdin); print('MAKER_DB =', v.get('MAKER_DB'))"
```
Expected: `MAKER_DB = /data/maker.db`

- [ ] **Step 7: Deploy and verify**

```bash
railway redeploy --yes || true
sleep 200
railway deployment list | head -3
railway logs --deployment --lines 30 | grep -E "preflight|bot\] starting|dashboard\] serving"
```
Expected: `SUCCESS`, `[preflight] all checks passed`, `[bot] starting (maker sim)`.

- [ ] **Step 8: Generate a domain and confirm it serves**

Railway → Settings → Networking → Generate Domain. Then:

```bash
curl -s -m 25 -o /dev/null -w "health HTTP:%{http_code}\n" https://<maker-domain>/api/health
curl -s -m 25 -o /dev/null -w "root   HTTP:%{http_code}\n" https://<maker-domain>/
```
Expected: both `HTTP:200`

- [ ] **Step 9: Confirm fills are being recorded online**

```bash
sleep 300
curl -s -m 25 "https://<maker-domain>/api/state" | python -c "
import json,sys; s=json.load(sys.stdin)
print('fills:', s.get('fills'), '| quotes:', s.get('quotes'), '| errors:', s.get('errors', 'none'))
assert s.get('quotes', 0) > 0, 'no quotes recorded'
print('MAKER LIVE')"
```
Expected: `MAKER LIVE`

---

### Task 9: Remove the maker from the taker repo

**Files:**
- Delete: `polymarket-taker/maker/`, `polymarket-taker/server/maker_dashboard.py`
- Modify: `polymarket-taker/.gitignore`

**Interfaces:**
- Consumes: a verified-live maker service from Task 8
- Produces: a taker repo containing only the taker

Run this ONLY after Task 8 Step 9 prints `MAKER LIVE`.

- [ ] **Step 1: Remove**

```bash
cd "/c/Users/Tiger/Agents/Projects/AI Trading/polymarket-taker"
git rm -r maker/
git rm server/maker_dashboard.py
sed -i '/# maker sim runtime/,+3d' .gitignore
grep -c maker .gitignore || echo "gitignore clean"
```

- [ ] **Step 2: Verify the taker still imports and preflights**

```bash
.venv/Scripts/python.exe -c "import strategy.main, server.dashboard, server.kanban; print('imports OK')"
.venv/Scripts/python.exe deploy/run_service.py --preflight
```
Expected: `imports OK` and `[preflight] all checks passed`

- [ ] **Step 3: Commit and push**

```bash
git add -A
git commit -m "chore: remove maker; it now lives in polymarket-maker

Removed last, after the maker service was verified live, so a working copy
always existed somewhere during the migration."
git push
```

- [ ] **Step 4: Verify the taker deploy is still green**

```bash
sleep 200
curl -s -m 25 -o /dev/null -w "taker health HTTP:%{http_code}\n" https://claude-poly-bot-production.up.railway.app/api/health
```
Expected: `HTTP:200`

- [ ] **Step 5: Final structural check — both repos, same shape**

```bash
cd "/c/Users/Tiger/Agents/Projects/AI Trading"
echo "--- taker ---"; ls polymarket-taker | grep -vE "^(\.venv|logs|archive|ui|scripts|node_modules)$"
echo "--- maker ---"; ls polymarket-maker | grep -vE "^(\.venv|logs|archive)$"
```
Expected: both list `AGENTS.md deploy Dockerfile railway.toml requirements.txt research server strategy`.

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §5 target structure | 2, 4, 5 |
| §6 research model (5 files, EN+HE) | 7 |
| §7 inherited files excluded | 7 step 6 |
| §8 AGENTS.md + hook | 7 |
| §9 Railway one project two services | 3, 8 |
| §10 migration order + gates | 1–9, in order |
| §11 risk: taker never offline | 3 gate, 9 runs last |
| §12 success criteria | 9 step 5 |

**Placeholder scan:** `<maker-domain>` in Task 8 steps 8–9 is a value produced by step 8, not a placeholder for missing content. `<polymarket-taker | polymarket-maker>` in the AGENTS.md template is a per-repo substitution, stated as such. No TBD/TODO.

**Type consistency:** `load_net()` returns `NetConfig` (Task 4 step 3) and is imported as `load_bot_cfg` in `strategy/main.py` (step 4) — the maker calls `.gamma_host` and `.series_slug`, both present on `NetConfig`. `PAGE` is a `str` in `server/kanban.py`, consumed by `server/dashboard.py` (Task 5). `store.db()` context manager used identically in both repos.
