#!/usr/bin/env python3
"""Container entrypoint: supervise the paper bot + serve the dashboard.

One long-lived process for Railway/Fly/Render. Adapted from the previous
deploy's run_bots.py, simplified to a single bot profile (this repo runs one
strategy, not an A/B set) and hardened with:

  - a startup preflight that FAILS LOUDLY on the two silent-killer misconfigs
    (Binance geo-block, TURSO_URL set but libsql missing)
  - a daily prune of the `decisions` table (~30k rows/day)
  - bot restart with backoff if it dies

Env:
  TURSO_URL / TURSO_TOKEN   remote libSQL; omit to use local sqlite
  POLYBOT_DB                local sqlite path (use a mounted volume!)
  PORT                      dashboard port (host injects this)
  SIM_ONLY                  must stay "1"; guards against arming a hosted box
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

PORT = int(os.environ.get("PORT", "8787"))
RESTART_BACKOFF = 5.0


def preflight() -> None:
    """Fail loudly on misconfigurations that would otherwise look healthy.

    Both of these produce a green healthcheck and silently useless data, which
    is the worst possible failure mode for an unattended collector.
    """
    problems: list[str] = []

    # 1. Storage. TURSO_URL set but libsql missing means we quietly write to a
    #    container-local file that vanishes on the next redeploy.
    from strategy import store
    print(f"[preflight] storage backend: {store.backend_name()}", flush=True)
    if os.environ.get("TURSO_URL") and not store.USE_TURSO:
        problems.append(
            "TURSO_URL is set but libsql is not importable -- data would be "
            "written to an ephemeral local file and lost on redeploy."
        )
    polybot_db = os.environ.get("POLYBOT_DB")
    if not os.environ.get("TURSO_URL") and not polybot_db:
        print("[preflight] WARNING: no TURSO_URL and no POLYBOT_DB -- using a "
              "container-local trades.db, which is LOST on redeploy unless a "
              "volume is mounted.", flush=True)
    if polybot_db and not store.USE_TURSO:
        # Actually open + write + read the DB so a missing/unwritable volume
        # mount fails HERE with a clear message, not later as a cryptic
        # "unable to open database file" mid-run.
        try:
            with store.db() as _c:
                _c.execute("CREATE TABLE IF NOT EXISTS _preflight (x INTEGER)")
                _c.execute("INSERT INTO _preflight VALUES (1)")
                _c.execute("DELETE FROM _preflight")
            print(f"[preflight] volume OK: {polybot_db} is writable", flush=True)
        except Exception as e:
            problems.append(
                f"POLYBOT_DB={polybot_db} is not writable ({e}). Mount a Railway "
                f"volume at that directory (Settings -> Volumes -> /data)."
            )

    # 1b. Collector DB (separate file, same volume). The gate-collector writes
    #     ONLY here; it never touches trades.db, so there is no two-writer clash
    #     with the bot (DEPLOY.md's warning is about the SAME db, not two dbs).
    collector_db = os.environ.get("COLLECTOR_DB", "/data/collector.db")
    if not store.USE_TURSO:
        try:
            from pathlib import Path as _P
            _p = _P(collector_db)
            _p.parent.mkdir(parents=True, exist_ok=True)
            import sqlite3 as _sql
            with _sql.connect(str(_p)) as _c:
                _c.execute("CREATE TABLE IF NOT EXISTS _preflight (x INTEGER)")
                _c.execute("INSERT INTO _preflight VALUES (1)")
                _c.execute("DELETE FROM _preflight")
            print(f"[preflight] collector DB OK: {collector_db} is writable",
                  flush=True)
        except Exception as e:
            problems.append(
                f"COLLECTOR_DB={collector_db} is not writable ({e}). The "
                f"gate-collector needs a writable path on the mounted volume."
            )

    # 2. Binance reachability. The spot gate IS the strategy; if Binance geo-
    #    blocks this region the gate fails closed and we collect zero fills
    #    forever while looking perfectly healthy.
    import requests
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price",
                         params={"symbol": "BTCUSDT"}, timeout=15)
        if r.status_code == 200:
            print(f"[preflight] binance OK: BTC={r.json().get('price')}", flush=True)
        else:
            problems.append(
                f"Binance returned HTTP {r.status_code} (451/403 = geo-blocked "
                f"region, e.g. US). Redeploy in a non-US region or the spot "
                f"gate will reject every trade."
            )
    except Exception as e:
        problems.append(f"Binance unreachable: {e}")

    # 3. Config. Reported separately from network errors: a missing env var
    #    surfaced as "Polymarket unreachable: 'PRIVATE_KEY'", which sent the
    #    reader hunting for a connectivity problem that did not exist.
    cfg = None
    try:
        from strategy.config import load
        cfg = load()
    except KeyError as e:
        problems.append(
            f"Missing required environment variable {e}. Set the variables from "
            f"deploy/.env.deploy.example in your host's Variables tab "
            f"(the wallet fields stay as the fake placeholders)."
        )
    except Exception as e:
        problems.append(f"Config failed to load: {type(e).__name__}: {e}")

    # 4. Polymarket reachability (only meaningful once config loaded).
    if cfg is not None:
        try:
            from strategy.markets import fetch_live_market
            m = fetch_live_market(cfg.gamma_host, cfg.series_slug)
            print(f"[preflight] polymarket OK: live market={m.market_slug if m else None}",
                  flush=True)
        except Exception as e:
            problems.append(f"Polymarket unreachable: {type(e).__name__}: {e}")
        if not cfg.sim_only:
            problems.append("sim_only is False -- refusing to run a hosted box "
                            "that could place real orders.")

    if problems:
        print("\n[preflight] FAILED:", flush=True)
        for p in problems:
            print(f"  - {p}", flush=True)
        # Back off before exiting. The host restarts on failure, and without
        # this the container respawns ~1/sec, spamming logs and hammering
        # Binance/Polymarket with a preflight burst on every retry.
        if "--preflight" not in sys.argv:
            print("[preflight] sleeping 30s before exit to avoid a hot restart loop",
                  flush=True)
            time.sleep(30)
        sys.exit(1)
    print("[preflight] all checks passed", flush=True)


def prune_loop() -> None:
    """Drop decisions older than 30d, once a day. orders/resolutions kept."""
    from strategy import store
    while True:
        time.sleep(86400)
        try:
            n = store.prune_decisions(30.0)
            print(f"[prune] removed {n} old decision rows", flush=True)
        except Exception as e:
            print(f"[prune] failed: {e}", flush=True)


def run_bot() -> None:
    """Run the paper bot, restarting it if it exits."""
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    while True:
        print("[bot] starting (paper/sim)", flush=True)
        # No --live. Ever. This container has placeholder credentials.
        proc = subprocess.Popen([sys.executable, "-m", "strategy.main"],
                                cwd=str(ROOT), env=env)
        code = proc.wait()
        print(f"[bot] exited code={code}; restarting in {RESTART_BACKOFF}s", flush=True)
        time.sleep(RESTART_BACKOFF)


def run_collector() -> None:
    """Run the forward gate-collector, restarting it if it exits.

    Writes ONLY to COLLECTOR_DB (default /data/collector.db) -- a separate file
    from the bot's trades.db, so the two never contend. It is a read-only
    observer of the live market; it never places orders.
    """
    env = dict(os.environ, PYTHONPATH=str(ROOT),
               COLLECTOR_DB=os.environ.get("COLLECTOR_DB", "/data/collector.db"))
    pid_path = ROOT / "collector.pid"
    while True:
        # Record our own pid so the dashboard can show collector liveness
        # (mirrors the bot's bot.pid / bot.win.pid pattern).
        try:
            pid_path.write_text(str(os.getpid()))
        except OSError:
            pass
        print("[collector] starting (read-only gate collector)", flush=True)
        proc = subprocess.Popen([sys.executable, "-m", "strategy.collect_gate"],
                                cwd=str(ROOT), env=env)
        code = proc.wait()
        try:
            if pid_path.exists():
                pid_path.unlink()
        except OSError:
            pass
        print(f"[collector] exited code={code}; restarting in {RESTART_BACKOFF}s",
              flush=True)
        time.sleep(RESTART_BACKOFF)


def main() -> None:
    # `--preflight` runs the checks and exits: use it to validate a host's
    # region/config before committing to a full deploy.
    if "--preflight" in sys.argv:
        preflight()
        return
    preflight()
    threading.Thread(target=run_bot, name="bot", daemon=True).start()
    # Collector is opt-in. It retired the gate question at n=313 (forward combo
    # 78/88 vs backtest 81/96; gate DEAD as a standalone win-rate improver).
    # Keep COLLECTOR_ENABLED unset/off to leave it stopped; set it to start the
    # read-only observer again (e.g. to keep archiving windows).
    if os.environ.get("COLLECTOR_ENABLED", "0").strip() in ("1", "true", "True", "yes"):
        threading.Thread(target=run_collector, name="collector", daemon=True).start()
        print("[collector] ENABLED via COLLECTOR_ENABLED=1", flush=True)
    else:
        # Drop any stale pid so the dashboard reports collector_running=False
        # instead of a false "alive" inherited from the previous deploy.
        try:
            (ROOT / "collector.pid").unlink(missing_ok=True)
        except OSError:
            pass
        print("[collector] DISABLED (COLLECTOR_ENABLED not set) -- gate thesis "
              "retired at n=313; collector.db frozen on the /data volume", flush=True)
    threading.Thread(target=prune_loop, name="prune", daemon=True).start()

    import uvicorn
    print(f"[dashboard] serving on 0.0.0.0:{PORT}", flush=True)
    uvicorn.run("server.dashboard:app", host="0.0.0.0", port=PORT, log_level="info")


if __name__ == "__main__":
    main()
