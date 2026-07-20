"""FastAPI dashboard backend.

Polls the local SQLite (trades.db), the CLOB book of the live market, the
deposit wallet's positions, and on-chain pUSD balance. Serves /api/state for
the UI to poll, and /api/events for incremental new-row pulls.

Run:  .venv/bin/uvicorn server.dashboard:app --host 127.0.0.1 --port 8787
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import requests
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from bot import store
from bot.book import fetch_book
from bot.config import load as load_cfg
from bot.fees import net_pnl
from bot.markets import LiveMarket, fetch_live_market
from bot.spot import FEED as SPOT, favored_side

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
        "action, reason, dry_run FROM decisions WHERE id > ? "
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
    rows = store.sim_open_positions()
    bu, bd = _state.get("book_up"), _state.get("book_down")
    now = time.time()
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
    if cfg.use_spot_gate:
        SPOT.start()   # read-only Binance feed, for gate visibility in the UI


@app.get("/api/health")
def health():
    return {"ok": True, "ts": time.time()}


def _risk_state(pnl: dict) -> str:
    """Approximate the bot's risk-gate state for UI display."""
    if pnl["realized_usd"] <= -cfg.max_daily_loss_usd:
        return "LOSS_CAP"
    # consecutive-loss kill detection
    rows = _query(
        "SELECT o.token_id, r.winning_token FROM orders o "
        "JOIN resolutions r ON r.condition_id=o.condition_id "
        "WHERE o.status IN ('filled','matched') AND o.dry_run=0 "
        "ORDER BY r.resolved_ts DESC LIMIT 10"
    )
    streak = 0
    for r in rows:
        if r["token_id"] == r["winning_token"]:
            break
        streak += 1
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


@app.get("/api/state")
def state():
    m = _state.get("market")
    now = time.time()
    pnl = realized_pnl_today()

    # Virtual account: cash from the ledger, open risk marked to the live book.
    sim_positions = _sim_positions_marked(m)
    account = store.sim_account(cfg.sim_bankroll_usd)
    open_value = sum(p["value"] for p in sim_positions)
    account["open_value"] = open_value
    account["equity"] = account["cash"] + open_value
    account["total_pnl"] = account["equity"] - account["bankroll"]
    account["return_pct"] = (
        (account["total_pnl"] / account["bankroll"] * 100.0) if account["bankroll"] else 0.0
    )
    account["open_positions"] = len(sim_positions)
    return {
        "now": now,
        "bot_running": _state["bot_running"],
        "bot_mode": _state.get("bot_mode", "stopped"),
        "risk_state": _risk_state(pnl),
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
        "sim": _sim_cached(),
        "kpi": _kpi_cached(),
        "spot": _spot_state(m),
        "settlements": store.sim_recent_settlements(limit=15),
        "decisions": recent_decisions(limit=80),
        "orders": recent_orders(limit=30),
        "errors": _state.get("errors") or {},
    }


@app.get("/api/events")
def events(since_decision: int = Query(0), since_order: int = Query(0)):
    return {
        "decisions": recent_decisions(limit=200, since_id=since_decision),
        "orders": recent_orders(limit=50, since_id=since_order),
    }


# ---------------------------------------------------------------------------
# Static UI (production). In dev the Vite server on :5173 proxies /api here;
# in the container there is no Vite, so FastAPI serves the built bundle itself.
# Mounted LAST so it never shadows an /api/* route.
# ---------------------------------------------------------------------------
_UI_DIST = ROOT / "ui" / "dist"
if _UI_DIST.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(_UI_DIST), html=True), name="ui")
