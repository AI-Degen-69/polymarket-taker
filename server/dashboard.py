"""FastAPI dashboard backend.

Polls the local SQLite (trades.db), the CLOB book of the live market, the
deposit wallet's positions, and on-chain pUSD balance. Serves /api/state for
the UI to poll, and /api/events for incremental new-row pulls.

Run:  .venv/bin/uvicorn server.dashboard:app --host 127.0.0.1 --port 8787
"""
from __future__ import annotations

import asyncio
import math
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

from strategy import store
from strategy.book import fetch_book
from strategy.config import load as load_cfg
from strategy.fees import net_pnl
from strategy.markets import LiveMarket, fetch_live_market
from strategy.spot import FEED as SPOT, favored_side

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "trades.db"

cfg = load_cfg()

# In-memory rolling state, refreshed by background tasks.
_state: dict[str, Any] = {
    "ts": 0.0,
    "market": None,           # LiveMarket as dict
    "book_up": None,
    "book_down": None,
    "balance_pusd": None,     # on-chain pUSD in deposit wallet
    "positions": [],          # data-api positions for deposit wallet
    "value_usd": None,        # data-api value endpoint
    "bot_running": False,
    "collector_running": False,
    "errors": {},             # last error per poller for debugging
}


# ---------------------------------------------------------------------------
# SQLite reads


def _query(sql: str, params: tuple = ()) -> list[dict]:
    """Run a read against whichever backend store is using.

    Goes through store.db() rather than opening its own sqlite connection --
    otherwise with TURSO_URL set the bot would write to Turso while the
    dashboard read an empty local file and showed nothing. Rows are zipped with
    cursor.description instead of using sqlite3.Row, because the libsql
    connection does not support row_factory.
    """
    try:
        with store.db() as c:
            cur = c.execute(sql, params)
            # Lower-case the column names. libsql returns SQL KEYWORDS in
            # cursor.description upper-cased -- `action` comes back as `ACTION`
            # on Turso but `action` on sqlite3. The UI reads `d.action`, so on
            # Turso it got undefined, called .startsWith() on it, and the
            # resulting throw unmounted React into a blank page. Our schema is
            # all-lowercase, so normalising is safe and covers future keywords.
            cols = [d[0].lower() for d in (cur.description or [])]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        _state["errors"]["db"] = str(e)
        return []


def recent_decisions(limit: int = 50, since_id: int = 0) -> list[dict]:
    return _query(
        "SELECT id, ts, market_slug, side, t_remaining, ask_price, ask_size, "
        "action, reason, dry_run, count FROM decisions WHERE id > ? "
        "ORDER BY id DESC LIMIT ?",
        (since_id, limit),
    )


def recent_orders(limit: int = 50, since_id: int = 0) -> list[dict]:
    return _query(
        "SELECT id, ts, market_slug, condition_id, token_id, side, size, "
        "price, order_id, status, filled_size, error, dry_run FROM orders "
        "WHERE id > ? ORDER BY id DESC LIMIT ?",
        (since_id, limit),
    )


def realized_pnl_today() -> dict:
    """Compute realized PnL from filled orders. Uses the bot's resolutions table
    (populated by bot.resolver) and back-fills any new resolutions on demand."""
    cutoff = time.time() - 86400
    orders = _query(
        "SELECT o.condition_id, o.market_slug, o.token_id, o.size, o.price, "
        "r.winning_token "
        "FROM orders o LEFT JOIN resolutions r ON r.condition_id = o.condition_id "
        "WHERE o.dry_run=0 AND o.status IN ('filled','matched') AND o.ts > ?",
        (cutoff,),
    )
    if not orders:
        return {"realized_usd": 0.0, "wins": 0, "losses": 0, "pending": 0}

    realized = 0.0
    wins = 0
    losses = 0
    pending = 0
    for o in orders:
        winner = o["winning_token"]
        if winner is None:
            winner = _resolved_winning_token(o["market_slug"])
            if winner is not None:
                _record_resolution(o["condition_id"], winner)
        if winner is None:
            pending += 1
            continue
        # Net of the taker fee paid at entry -- gross PnL flatters this strategy
        # by ~6-7% of edge, which is enough to invert the sign.
        realized += net_pnl(float(o["size"]), float(o["price"]), winner == o["token_id"])
        if winner == o["token_id"]:
            wins += 1
        else:
            losses += 1
    return {"realized_usd": realized, "wins": wins, "losses": losses, "pending": pending}


def _sim_positions_marked(market: Optional[dict]) -> list[dict]:
    """Open simulated positions, marked to the live book where we can.

    Mark uses the current best BID for that side — what we could actually get
    out at. We never sell, so this is a valuation, not an exit plan. If the
    position isn't in the currently-live window we have no fresh quote, so we
    fall back to cost (mark == cost, unrealized 0) rather than invent a price.
    """
    return _mark_positions(store.sim_open_positions(), market, time.time())


def _mark_positions(rows: list[dict], market: Optional[dict], now: float) -> list[dict]:
    """Mark pre-fetched open positions to the live book. Pure in-memory: no DB
    reads, so it runs on every request while the position SET refreshes on the
    slower analytics cadence."""
    bu, bd = _state.get("book_up"), _state.get("book_down")
    for p in rows:
        # A position's own window ends 300s after the ts encoded in its slug.
        # Once that passes, the outcome is decided and only the resolver is
        # outstanding -- there is no live price for it any more. Marking it
        # against whatever market happens to be live now would be nonsense:
        # a different market's book has nothing to do with this position.
        window_end = None
        try:
            window_end = int(p["market_slug"].rsplit("-", 1)[1]) + 300
        except Exception:
            pass
        is_live = (
            market is not None
            and p["condition_id"] == market.get("condition_id")
            and window_end is not None
            and now < window_end
        )

        if is_live:
            book = bu if p["side"] == "UP" else bd
            mark_px = book.get("best_bid") if book else None
            if mark_px is not None:
                p["mark_source"] = "book"
                p["mark_price"] = mark_px
                p["value"] = p["shares"] * mark_px
                p["unrealized"] = p["value"] - (p["cost"] + p["fees"])
                p["pending"] = False
                continue

        # Closed (or no quote): freeze at cost and flag as awaiting settlement.
        # Unrealized is 0 by definition -- the position is worth what we paid
        # until the resolver tells us whether it paid $1.00 or $0.00.
        p["mark_source"] = "pending"
        p["mark_price"] = None
        p["value"] = p["cost"] + p["fees"]
        p["unrealized"] = 0.0
        p["pending"] = True
        p["closed_secs_ago"] = (now - window_end) if window_end else None
    return rows


def _spot_state(market: Optional[dict]) -> dict:
    """Live view of the Binance gate: where BTC is vs the window open, and
    whether that currently permits a trade."""
    if not cfg.use_spot_gate:
        return {"enabled": False}
    px = SPOT.last_price()
    out: dict = {
        "enabled": True,
        "healthy": px is not None,
        "price": px,
        "threshold_bps": cfg.min_spot_offset_bps,
        "offset_bps": None,
        "favored": None,
        "gate": "NO FEED" if px is None else "…",
    }
    if market and px is not None:
        off = SPOT.offset_bps(int(market["start_ts"]))
        out["offset_bps"] = off
        if off is None:
            out["gate"] = "NO OPEN PX"
        else:
            out["favored"] = favored_side(off)
            out["gate"] = "OPEN" if abs(off) >= cfg.min_spot_offset_bps else "FLAT"
    return out


_sim_cache: dict = {"ts": 0.0, "data": None}


def _sim_cached(ttl: float = 5.0) -> dict:
    """sim_report() scans the orders table; /api/state is polled ~1/s, so cache."""
    now = time.time()
    if _sim_cache["data"] is not None and now - _sim_cache["ts"] < ttl:
        return _sim_cache["data"]

    # Back-fill resolutions for simulated fills the bot's resolver hasn't
    # reached yet, so the scorecard isn't permanently stuck on "pending".
    try:
        for cond, slug in store.unresolved_with_slug(dry_run=True)[:25]:
            winner = _resolved_winning_token(slug)
            if winner is not None:
                _record_resolution(cond, winner)
    except Exception as e:
        _state["errors"]["sim_resolve"] = str(e)

    try:
        data = store.sim_report()
        _state["errors"].pop("sim", None)
    except Exception as e:
        _state["errors"]["sim"] = str(e)
        data = {"total": {}, "buckets": {}}
    _sim_cache.update(ts=now, data=data)
    return data


_kpi_cache: dict = {"ts": 0.0, "data": None}


def _kpi_cached(ttl: float = 5.0) -> dict:
    """kpi_report() also scans the orders table; share the same 5s TTL."""
    now = time.time()
    if _kpi_cache["data"] is not None and now - _kpi_cache["ts"] < ttl:
        return _kpi_cache["data"]
    try:
        data = store.kpi_report(cfg.sim_bankroll_usd)
        _state["errors"].pop("kpi", None)
    except Exception as e:
        _state["errors"]["kpi"] = str(e)
        data = {}
    _kpi_cache.update(ts=now, data=data)
    return data


_resolved_cache: dict[str, Optional[str]] = {}


def _resolved_winning_token(market_slug: str) -> Optional[str]:
    """Return the winning token_id for a resolved market, or None if unresolved.

    Gamma's `condition_ids` filter hides closed markets — query by slug instead.
    Cached in-process; markets resolve immutably so cache hits are safe.
    """
    if not market_slug:
        return None
    if market_slug in _resolved_cache:
        return _resolved_cache[market_slug]
    try:
        # /events, not /markets — see bot/resolver.py: `/markets?slug=` returns
        # an empty list once a 5-min market ages out of that index, so this
        # silently reported everything as unresolved.
        r = requests.get(
            f"{cfg.gamma_host}/events",
            params={"slug": market_slug},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8,
        )
        if r.status_code != 200:
            return None
        events = r.json()
        if not events:
            return None
        ev = events[0] if isinstance(events, list) else events
        markets = ev.get("markets") or []
        if not markets:
            return None
        m = markets[0]
        if not m.get("closed"):
            return None
        prices = m.get("outcomePrices")
        if isinstance(prices, str):
            import json as _json
            prices = _json.loads(prices)
        if not prices or len(prices) != 2:
            return None
        token_ids = m.get("clobTokenIds")
        if isinstance(token_ids, str):
            import json as _json
            token_ids = _json.loads(token_ids)
        if not token_ids or len(token_ids) != 2:
            return None
        winner_idx = 0 if float(prices[0]) > float(prices[1]) else 1
        winner = str(token_ids[winner_idx])
        _resolved_cache[market_slug] = winner
        return winner
    except Exception:
        return None


def _record_resolution(condition_id: str, winning_token: str) -> None:
    # store.record_resolution writes through whichever backend is active.
    store.record_resolution(condition_id, winning_token)


# ---------------------------------------------------------------------------
# Background pollers


async def poll_market_loop():
    while True:
        try:
            m = await asyncio.to_thread(fetch_live_market, cfg.gamma_host, cfg.series_slug)
            _state["market"] = (
                {
                    "condition_id": m.condition_id,
                    "market_slug": m.market_slug,
                    "up_token": m.up_token,
                    "down_token": m.down_token,
                    "start_ts": m.start_ts,
                    "end_ts": m.end_ts,
                    "tick_size": m.tick_size,
                    "neg_risk": m.neg_risk,
                }
                if m
                else None
            )
            _state["errors"].pop("market", None)
        except Exception as e:
            _state["errors"]["market"] = str(e)
        await asyncio.sleep(2.0)


async def poll_book_loop():
    while True:
        m = _state.get("market")
        if not m:
            await asyncio.sleep(0.5)
            continue
        try:
            bu, bd = await asyncio.gather(
                asyncio.to_thread(fetch_book, cfg.clob_host, m["up_token"]),
                asyncio.to_thread(fetch_book, cfg.clob_host, m["down_token"]),
            )
            _state["book_up"] = {
                "best_bid": bu.best_bid,
                "bid_size": bu.bid_size,
                "best_ask": bu.best_ask,
                "ask_size": bu.ask_size,
            }
            _state["book_down"] = {
                "best_bid": bd.best_bid,
                "bid_size": bd.bid_size,
                "best_ask": bd.best_ask,
                "ask_size": bd.ask_size,
            }
            _state["errors"].pop("book", None)
        except Exception as e:
            _state["errors"]["book"] = str(e)
        await asyncio.sleep(0.25)


async def poll_positions_loop():
    while True:
        try:
            r = await asyncio.to_thread(
                lambda: requests.get(
                    "https://data-api.polymarket.com/positions",
                    params={"user": cfg.funder_address},
                    timeout=3,
                )
            )
            r.raise_for_status()
            _state["positions"] = r.json()
            _state["errors"].pop("positions", None)
        except Exception as e:
            _state["errors"]["positions"] = str(e)

        try:
            r = await asyncio.to_thread(
                lambda: requests.get(
                    "https://data-api.polymarket.com/value",
                    params={"user": cfg.funder_address},
                    timeout=3,
                )
            )
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list) and data:
                _state["value_usd"] = float(data[0].get("value") or 0.0)
            _state["errors"].pop("value", None)
        except Exception as e:
            _state["errors"]["value"] = str(e)

        await asyncio.sleep(2.0)


async def poll_balance_loop():
    """Read pUSD balance of deposit wallet on-chain."""
    PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
    SELECTOR = "0x70a08231"
    while True:
        try:
            data = SELECTOR + cfg.funder_address.lower().replace("0x", "").rjust(64, "0")
            r = await asyncio.to_thread(
                lambda: requests.post(
                    cfg.polygon_rpc,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "eth_call",
                        "params": [{"to": PUSD, "data": data}, "latest"],
                    },
                    timeout=5,
                )
            )
            r.raise_for_status()
            raw = r.json().get("result")
            if raw:
                _state["balance_pusd"] = int(raw, 16) / 1e6
            _state["errors"].pop("balance", None)
        except Exception as e:
            _state["errors"]["balance"] = str(e)
        await asyncio.sleep(5.0)


def _pid_alive(pid: int) -> bool:
    """Check whether a pid is running, without signalling it.

    NOTE: os.kill(pid, 0) is NOT a liveness probe on Windows -- for any signal
    other than CTRL_C_EVENT/CTRL_BREAK_EVENT, CPython calls TerminateProcess(),
    so it would kill the bot instead of checking on it. Use the Win32 API here
    and reserve os.kill for POSIX.
    """
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)  # signal 0 = existence check (POSIX only)
        return True
    except PermissionError:
        return True  # exists, just owned by another user
    except (ProcessLookupError, ValueError):
        return False


async def poll_bot_running_loop():
    pid_path = ROOT / "bot.pid"
    # Under Git Bash on Windows, bot.pid holds an MSYS pid that os.kill() cannot
    # resolve; the launch scripts also record the real Windows pid here.
    win_pid_path = ROOT / "bot.win.pid"
    mode_path = ROOT / "bot.mode"
    while True:
        running = False
        active_pid_path = win_pid_path if win_pid_path.exists() else pid_path
        if active_pid_path.exists():
            try:
                pid = int(active_pid_path.read_text().strip())
                running = _pid_alive(pid)
            except (OSError, ValueError):
                running = False
        mode = "unknown"
        if mode_path.exists():
            try:
                mode = mode_path.read_text().strip() or "unknown"
            except Exception:
                pass
        if not running:
            mode = "stopped"
        _state["bot_running"] = running
        _state["bot_mode"] = mode
        await asyncio.sleep(2.0)


async def poll_collector_running_loop():
    """Mirror poll_bot_running_loop for the gate-collector subprocess.

    The collector supervisor (run_service.run_collector) writes ROOT/collector.pid
    with its own pid. On Railway (Linux) that is a real OS pid, so _pid_alive
    resolves it directly. Absence of the file (or a dead pid) => not running.
    """
    pid_path = ROOT / "collector.pid"
    collector_enabled = os.environ.get("COLLECTOR_ENABLED", "0").strip() in (
        "1", "true", "True", "yes")
    while True:
        if not collector_enabled:
            # Collector intentionally stopped (gate thesis retired). Report
            # not-running and do not spin on a stale pid file.
            _state["collector_running"] = False
            await asyncio.sleep(5.0)
            continue
        running = False
        if pid_path.exists():
            try:
                pid = int(pid_path.read_text().strip())
                running = _pid_alive(pid)
            except (OSError, ValueError):
                running = False
        _state["collector_running"] = running
        await asyncio.sleep(2.0)


# ---------------------------------------------------------------------------
# FastAPI app

app = FastAPI(title="poly_hft dashboard")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _startup():
    asyncio.create_task(poll_market_loop())
    asyncio.create_task(poll_book_loop())
    asyncio.create_task(poll_positions_loop())
    asyncio.create_task(poll_balance_loop())
    asyncio.create_task(poll_bot_running_loop())
    asyncio.create_task(poll_collector_running_loop())
    asyncio.create_task(analytics_loop())
    if cfg.use_spot_gate:
        SPOT.start()   # read-only Binance feed, for gate visibility in the UI


@app.get("/api/health")
def health():
    return {"ok": True, "ts": time.time()}


def _risk_state_from(pnl: dict, streak: int) -> str:
    """Risk-gate state for the UI, from pre-computed values (no DB read)."""
    if pnl["realized_usd"] <= -cfg.max_daily_loss_usd:
        return "LOSS_CAP"
    if streak >= cfg.consecutive_loss_kill:
        return f"LOSS_STREAK({streak})"
    return "OK"


def _filter_active_positions(positions: list[dict]) -> list[dict]:
    """data-api keeps resolved positions in the list at curPrice=0; drop them."""
    out: list[dict] = []
    for p in positions:
        cp = p.get("curPrice")
        sz = p.get("size")
        if cp is None or sz is None:
            continue
        try:
            if float(cp) <= 0.0 or float(sz) <= 0.0:
                continue
        except Exception:
            continue
        out.append(p)
    return out


# ---------------------------------------------------------------------------
# Analytics snapshot.
#
# CRITICAL: every aggregate below scans the orders table, and hosted databases
# (Turso) bill *every scanned row*. Running these per /api/state poll -- ~2/sec
# per open browser tab -- read 1.09 BILLION rows in a day and tripped the free
# tier's block. So they run ONCE here, in a single background pass shared by all
# clients, and /api/state serves the cached result with zero DB reads.
# ---------------------------------------------------------------------------
_analytics: dict = {"ts": 0.0, "data": None}
ANALYTICS_TTL = 15.0


def _refresh_analytics() -> None:
    """One pass: back-fill new resolutions, then recompute every aggregate."""
    try:
        for cond, slug in store.unresolved_with_slug(dry_run=True)[:25]:
            winner = _resolved_winning_token(slug)
            if winner is not None:
                _record_resolution(cond, winner)
    except Exception as e:
        _state["errors"]["sim_resolve"] = str(e)

    try:
        data = {
            "account": store.sim_account(cfg.sim_bankroll_usd),
            "open_positions_raw": store.sim_open_positions(),
            "kpi": store.kpi_report(cfg.sim_bankroll_usd),
            "sim": store.sim_report(),
            "settlements": store.sim_recent_settlements(limit=2000),
            "pnl": realized_pnl_today(),
            "risk_streak": _consec_loss_streak(),
            "decisions": recent_decisions(limit=80),
            "orders": recent_orders(limit=30),
        }
        _analytics.update(ts=time.time(), data=data)
        _state["errors"].pop("analytics", None)
    except Exception as e:
        _state["errors"]["analytics"] = str(e)


async def analytics_loop():
    while True:
        await asyncio.to_thread(_refresh_analytics)
        await asyncio.sleep(ANALYTICS_TTL)


def _consec_loss_streak() -> int:
    rows = _query(
        "SELECT o.token_id, r.winning_token FROM orders o "
        "JOIN resolutions r ON r.condition_id=o.condition_id "
        "WHERE o.status IN ('filled','matched','sim') AND o.dry_run=1 "
        "ORDER BY r.resolved_ts DESC LIMIT 10"
    )
    streak = 0
    for r in rows:
        if r["token_id"] == r["winning_token"]:
            break
        streak += 1
    return streak


@app.get("/api/state")
def state():
    m = _state.get("market")
    now = time.time()

    snap = _analytics["data"]
    if snap is None:
        # First request before the loop's first pass — compute once inline so
        # the dashboard isn't blank on cold start.
        _refresh_analytics()
        snap = _analytics["data"] or {}

    pnl = snap.get("pnl") or {"realized_usd": 0.0, "wins": 0, "losses": 0, "pending": 0}

    # Mark open positions to the LIVE book here (cheap, in-memory) so marks stay
    # real-time even though the position SET only refreshes every ANALYTICS_TTL.
    sim_positions = _mark_positions(snap.get("open_positions_raw") or [], m, now)
    account = dict(snap.get("account") or {})
    open_value = sum(p["value"] for p in sim_positions)
    account["open_value"] = open_value
    account["equity"] = account.get("cash", 0.0) + open_value
    account["total_pnl"] = account["equity"] - account.get("bankroll", cfg.sim_bankroll_usd)
    account["return_pct"] = (
        (account["total_pnl"] / account["bankroll"] * 100.0) if account.get("bankroll") else 0.0
    )
    account["open_positions"] = len(sim_positions)
    return {
        "now": now,
        "bot_running": _state["bot_running"],
        "collector_running": _state["collector_running"],
        "bot_mode": _state.get("bot_mode", "stopped"),
        "risk_state": _risk_state_from(pnl, snap.get("risk_streak", 0)),
        "wallet": {
            "eoa": cfg.wallet_address,
            "deposit": cfg.funder_address,
            "balance_pusd": _state.get("balance_pusd"),
            "value_usd": _state.get("value_usd"),
        },
        "market": (
            None
            if not m
            else {
                **m,
                "t_remaining": m["end_ts"] - now,
            }
        ),
        "book_up": _state.get("book_up"),
        "book_down": _state.get("book_down"),
        "positions": (
            _filter_active_positions(_state.get("positions") or [])
            if not cfg.sim_only
            else []
        ),
        "sim_positions": sim_positions,
        "account": account,
        "pnl": pnl,
        "config": {
            "max_entry_price": cfg.max_entry_price,
            "loser_floor": cfg.loser_floor,
            "seconds_before_close": cfg.seconds_before_close,
            "min_t_remaining_sec": cfg.min_t_remaining_sec,
            "max_entries_per_market": cfg.max_entries_per_market,
            "size_scale": cfg.size_scale,
            "max_open_positions": cfg.max_open_positions,
            "max_daily_loss_usd": cfg.max_daily_loss_usd,
            "sim_only": cfg.sim_only,
            "use_spot_gate": cfg.use_spot_gate,
            "min_spot_offset_bps": cfg.min_spot_offset_bps,
        },
        "sim": snap.get("sim") or {"total": {}, "buckets": {}},
        "kpi": snap.get("kpi") or {},
        "spot": _spot_state(m),
        "settlements": snap.get("settlements") or [],
        "decisions": snap.get("decisions") or [],
        "orders": snap.get("orders") or [],
        "errors": _state.get("errors") or {},
    }


@app.get("/api/events")
def events(since_decision: int = Query(0), since_order: int = Query(0)):
    return {
        "decisions": recent_decisions(limit=200, since_id=since_decision),
        "orders": recent_orders(limit=50, since_id=since_order),
    }


# ---------------------------------------------------------------------------
# Deploy metadata (footer). DEPLOY_SHA is set at deploy time via
# `railway variables set DEPLOY_SHA=<git sha>`; RAILWAY_DEPLOYMENT_ID is
# injected automatically by Railway into every container.
# ---------------------------------------------------------------------------
DEPLOY_META = {
    "deploy_sha": os.environ.get("DEPLOY_SHA", "unknown"),
    "railway_deploy_id": os.environ.get("RAILWAY_DEPLOYMENT_ID", "unknown"),
}


@app.get("/api/meta")
def meta():
    return DEPLOY_META


# ---------------------------------------------------------------------------
# Gate-collector read-only state. Reads COLLECTOR_DB (a SEPARATE sqlite file
# from trades.db) built by strategy.collect_gate. Never writes.
# ---------------------------------------------------------------------------
COLLECTOR_DB_PATH = os.environ.get("COLLECTOR_DB", "/data/collector.db")


def _wilson(acc: float | None, n: int) -> tuple[float | None, float | None]:
    """Wilson score 95% CI (lo, hi) as fractions. None if undefined."""
    if acc is None or n == 0:
        return (None, None)
    z = 1.96
    denom = 1.0 + z * z / n
    center = (acc + z * z / (2 * n)) / denom
    half = z * math.sqrt(acc * (1 - acc) / n + z * z / (4 * n * n)) / denom
    return (center - half, center + half)


def _collector_state() -> dict:
    import sqlite3 as _sql
    from pathlib import Path as _P

    p = _P(COLLECTOR_DB_PATH)
    if not p.exists():
        return {
            "db": str(p),
            "present": False,
            "windows": [],
            "stats": {"n": 0, "resolved": 0, "hit_book": 0, "hit_gate": 0,
                      "gate_coverage": 0.0, "book_acc": None, "gate_acc": None,
                      "book_ci": [None, None], "gate_ci": [None, None]},
        }
    try:
        con = _sql.connect(str(p), timeout=2.0)
        rows = con.execute(
            "SELECT condition_id, market_slug, snap_ts, book_favored, spot_bps, "
            "spot_favored, winner, resolved_ts, status, hit_book, hit_gate "
            "FROM collector_windows ORDER BY snap_ts DESC LIMIT 200"
        ).fetchall()
        # Stats are computed over the FULL sample, not the 200-row payload tail
        # (the collector is meant to exceed 200 windows; a tail-limited stat
        # would silently misreport the verdict numbers).
        agg = con.execute(
            "SELECT COUNT(*) AS total,"
            "       SUM(CASE WHEN status='RESOLVED' THEN 1 ELSE 0 END) AS resolved,"
            "       SUM(CASE WHEN status='RESOLVED' AND hit_book=1 THEN 1 ELSE 0 END) AS hb,"
            "       SUM(CASE WHEN status='RESOLVED' AND spot_bps IS NOT NULL"
            "               AND ABS(spot_bps)>=5.0 THEN 1 ELSE 0 END) AS gn,"
            "       SUM(CASE WHEN status='RESOLVED' AND spot_bps IS NOT NULL"
            "               AND ABS(spot_bps)>=5.0 AND hit_gate=1 THEN 1 ELSE 0 END) AS hg"
            "  FROM collector_windows"
        ).fetchone()
        con.close()
    except Exception as e:
        return {"db": str(p), "present": True, "error": str(e),
                "windows": [], "stats": {}}

    windows = [
        {
            "condition_id": r[0], "market_slug": r[1], "snap_ts": r[2],
            "book_favored": r[3], "spot_bps": r[4], "spot_favored": r[5],
            "winner": r[6], "resolved_ts": r[7], "status": r[8],
            "hit_book": bool(r[9]), "hit_gate": bool(r[10]),
        }
        for r in rows
    ]
    total = int(agg[0] or 0)
    n = int(agg[1] or 0)
    hit_book = int(agg[2] or 0)
    gate_n = int(agg[3] or 0)
    hit_gate = int(agg[4] or 0)
    # gated = windows where the gate actually FIRED (|spot_bps| >= 5). The
    # "gated accuracy" is the win rate among those windows only -- it is the
    # direct forward analogue of the backtest's 81->96 gate number, and it is
    # what GATE HEAT >=94% tests. Dividing by n (all windows) is wrong: it is
    # capped at gate_coverage (~33%) and can never light the flame.
    book_acc = hit_book / n if n else None
    gate_acc = hit_gate / gate_n if gate_n else None
    book_ci = _wilson(book_acc, n)
    gate_ci = _wilson(gate_acc, gate_n)
    return {
        "db": str(p), "present": True,
        "windows": windows,
        "stats": {
            "n": n,
            "open": total - n,
            "hit_book": hit_book,
            "hit_gate": hit_gate,
            "gate_n": gate_n,
            "gate_coverage": round(100.0 * gate_n / n, 1) if n else 0.0,
            "book_acc": round(100.0 * book_acc, 1) if book_acc is not None else None,
            "gate_acc": round(100.0 * gate_acc, 1) if gate_acc is not None else None,
            "book_ci": [round(100.0 * lo, 1) if lo is not None else None
                        for lo in book_ci],
            "gate_ci": [round(100.0 * lo, 1) if lo is not None else None
                        for lo in gate_ci],
        },
    }


@app.get("/api/collector-state")
def collector_state():
    return _collector_state()





# ---------------------------------------------------------------------------
# Static UI (production). In dev the Vite server on :5173 proxies /api here;
# in the container there is no Vite, so FastAPI serves the built bundle itself.
# Mounted LAST so it never shadows an /api/* route.
# ---------------------------------------------------------------------------
# Kanban view of the same data, mounted BEFORE the static UI so it isn't
# shadowed by the catch-all mount. Mirrors the maker dashboard's pipeline shape
# with taker-specific lanes and metrics.
from server.kanban import PAGE as _KANBAN_PAGE
from server.collector_page import PAGE as _COLLECTOR_PAGE


@app.get("/kanban", response_class=HTMLResponse)
def kanban():
    return _KANBAN_PAGE


@app.get("/collector", response_class=HTMLResponse)
def collector():
    return _COLLECTOR_PAGE


_UI_DIST = ROOT / "ui" / "dist"
if _UI_DIST.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(_UI_DIST), html=True), name="ui")
