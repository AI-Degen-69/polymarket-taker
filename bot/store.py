"""SQLite/Turso logger for every decision and order outcome.

Backend selection (pattern proven in the previous deploy):
  TURSO_URL (+TURSO_TOKEN) set and libsql importable -> remote libSQL (Turso),
  otherwise -> local SQLite at POLYBOT_DB (default ./trades.db).

Both backends expose the same DB-API surface (.execute/.executescript/.commit/
.close), so every query below works unchanged against either. This is what lets
the bot run on Railway with a hosted DB while staying a plain file locally.

libsql smoke-tested 2026-07-20: connect(url, auth_token=...) round-trips, and a
libsql:// URL reaches the Hrana layer correctly.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from bot.fees import breakeven_win_rate, net_pnl, taker_fee

DB_PATH = Path(__file__).resolve().parent.parent / "trades.db"

# Optional at import time so local runs work without libsql installed.
try:
    from libsql import connect as _libsql_connect
    _HAVE_LIBSQL = True
except Exception:
    _HAVE_LIBSQL = False

_TURSO_URL = os.environ.get("TURSO_URL")
_TURSO_TOKEN = os.environ.get("TURSO_TOKEN")
USE_TURSO = bool(_TURSO_URL) and _HAVE_LIBSQL


def _db_path() -> Path:
    """Local SQLite path. POLYBOT_DB lets Railway point at a mounted volume."""
    return Path(os.environ.get("POLYBOT_DB", str(DB_PATH)))


def backend_name() -> str:
    if USE_TURSO:
        return "turso"
    if _TURSO_URL and not _HAVE_LIBSQL:
        # Loud, because silently falling back to a local file on an ephemeral
        # container means the data disappears on the next redeploy.
        return "sqlite (TURSO_URL set but libsql NOT installed!)"
    return "sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    market_slug TEXT,
    condition_id TEXT,
    token_id TEXT,
    side TEXT,                  -- 'UP' or 'DOWN'
    t_remaining REAL,
    ask_price REAL,
    ask_size REAL,
    action TEXT,                -- 'BUY', 'SKIP_PRICE', 'SKIP_TIME', 'SKIP_SIZE', 'SKIP_AMBIGUOUS', 'SKIP_RISK'
    reason TEXT,
    dry_run INTEGER NOT NULL    -- 1 if shadow, 0 if live
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    market_slug TEXT,
    condition_id TEXT,
    token_id TEXT,
    side TEXT,
    size REAL,
    price REAL,
    order_id TEXT,
    status TEXT,
    filled_size REAL,
    error TEXT,
    dry_run INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS resolutions (
    condition_id TEXT PRIMARY KEY,
    winning_token TEXT,
    resolved_ts REAL
);

CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);
CREATE INDEX IF NOT EXISTS idx_orders_ts ON orders(ts);
CREATE INDEX IF NOT EXISTS idx_orders_cond ON orders(condition_id);
"""

# Added 2026-07-20 for the simulation build. Existing DBs predate these, so add
# them idempotently rather than forcing users to drop trades.db.
MIGRATIONS = [
    ("orders", "fee", "REAL DEFAULT 0"),
    ("decisions", "fee", "REAL DEFAULT 0"),
    ("decisions", "breakeven", "REAL DEFAULT 0"),
    # Instrumentation added 2026-07-20 to make the strategy auditable. The gate
    # value previously lived only inside the reason TEXT, so win-rate-by-bps
    # could not be queried. loser_ask captures the OTHER side's price at entry
    # (how converged the market was). Nullable -- rows before this stay NULL.
    ("orders", "spot_bps", "REAL"),
    ("orders", "loser_ask", "REAL"),
    ("orders", "breakeven", "REAL"),
    ("decisions", "spot_bps", "REAL"),
    ("decisions", "loser_ask", "REAL"),
]


def _migrate(c: sqlite3.Connection) -> None:
    for table, col, decl in MIGRATIONS:
        # .fetchall() is required: libsql's Cursor is not iterable, unlike
        # sqlite3's. Iterating it directly raises TypeError on Turso.
        cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
        if col not in cols:
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            except Exception:
                pass  # already added by a concurrent writer


def _conn() -> sqlite3.Connection:
    if USE_TURSO:
        c = _libsql_connect(_TURSO_URL, auth_token=_TURSO_TOKEN)
    else:
        c = sqlite3.connect(str(_db_path()))
    c.executescript(SCHEMA)
    _migrate(c)
    return c


def prune_decisions(older_than_days: float = 30.0) -> int:
    """Drop old `decisions` rows; never touches `orders` or `resolutions`.

    Measured ~30k decisions/day, so this table dominates storage while carrying
    the least long-term value -- PnL is reconstructed from orders+resolutions,
    which are kept forever.
    """
    cutoff = time.time() - older_than_days * 86400
    with db() as c:
        # Count first: libsql does not reliably report rowcount on DELETE.
        n = c.execute("SELECT COUNT(*) FROM decisions WHERE ts < ?", (cutoff,)).fetchone()[0]
        c.execute("DELETE FROM decisions WHERE ts < ?", (cutoff,))
        return int(n or 0)


_shared: Optional[object] = None
_lock = threading.Lock()


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    """Yield a connection.

    Local SQLite: connect per call (cheap, and avoids cross-thread reuse
    issues with sqlite3's default check_same_thread).

    Turso: reuse ONE connection, serialized by a lock. Measured 2026-07-20 --
    reconnecting per write cost 1,421 ms (0.7 writes/sec) because every call
    re-ran connect + executescript(SCHEMA) + PRAGMA migrations over the
    network. The bot needs ~4 writes/sec, so per-call connect is unusable
    remotely. Reuse keeps the handshake and schema work to once per process.
    """
    global _shared
    if not USE_TURSO:
        c = _conn()
        try:
            yield c
            c.commit()
        finally:
            c.close()
        return

    with _lock:
        if _shared is None:
            _shared = _conn()
        try:
            yield _shared            # type: ignore[misc]
            _shared.commit()         # type: ignore[union-attr]
        except Exception:
            # Drop the handle so the next call rebuilds it — a half-dead
            # socket would otherwise poison every subsequent write.
            try:
                _shared.close()      # type: ignore[union-attr]
            except Exception:
                pass
            _shared = None
            raise


_DEC_SQL = (
    "INSERT INTO decisions (ts, market_slug, condition_id, token_id, side, "
    "t_remaining, ask_price, ask_size, action, reason, dry_run, fee, breakeven, "
    "spot_bps, loser_ask) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)

_dec_buf: list[tuple] = []
_dec_lock = threading.Lock()
_FLUSH_SEC = 3.0
_flusher_started = False


def flush_decisions() -> int:
    """Write buffered decision rows in one batched transaction."""
    global _dec_buf
    with _dec_lock:
        if not _dec_buf:
            return 0
        batch, _dec_buf = _dec_buf, []
    try:
        with db() as c:
            c.executemany(_DEC_SQL, batch)
        return len(batch)
    except Exception:
        # Put them back so a transient network blip doesn't lose the rows.
        with _dec_lock:
            _dec_buf = batch + _dec_buf
        raise


def _flush_loop() -> None:
    while True:
        time.sleep(_FLUSH_SEC)
        try:
            flush_decisions()
        except Exception:
            pass  # retried on the next tick; rows are back in the buffer


def _ensure_flusher() -> None:
    global _flusher_started
    if _flusher_started:
        return
    _flusher_started = True
    threading.Thread(target=_flush_loop, name="dec-flush", daemon=True).start()
    import atexit
    atexit.register(lambda: _safe(flush_decisions))


def _safe(fn) -> None:
    try:
        fn()
    except Exception:
        pass


def log_decision(
    *,
    market_slug: str,
    condition_id: str,
    token_id: Optional[str],
    side: Optional[str],
    t_remaining: float,
    ask_price: Optional[float],
    ask_size: Optional[float],
    action: str,
    reason: str,
    dry_run: bool,
    fee: float = 0.0,
    breakeven: float = 0.0,
    spot_bps: Optional[float] = None,
    loser_ask: Optional[float] = None,
) -> None:
    """Buffer a decision row; a background thread batches them to the DB.

    The bot emits ~4 decisions/sec, nearly all of them SKIP_* noise. Writing
    each one synchronously costs a network round-trip on Turso (measured
    ~476 ms/write even with connection reuse), which would stall the poll loop.
    Batching every few seconds turns ~4 writes/sec into one multi-row insert.

    Decisions are diagnostic, so losing a few seconds of them on a hard kill is
    acceptable. Orders and resolutions -- the rows PnL is computed from -- are
    still written through synchronously.
    """
    row = (
        time.time(), market_slug, condition_id, token_id, side, t_remaining,
        ask_price, ask_size, action, reason, int(dry_run), fee, breakeven,
        spot_bps, loser_ask,
    )
    _ensure_flusher()
    with _dec_lock:
        _dec_buf.append(row)


def log_order(
    *,
    market_slug: str,
    condition_id: str,
    token_id: str,
    side: str,
    size: float,
    price: float,
    order_id: Optional[str],
    status: str,
    filled_size: float = 0.0,
    error: Optional[str] = None,
    dry_run: bool,
    fee: float = 0.0,
    spot_bps: Optional[float] = None,
    loser_ask: Optional[float] = None,
    breakeven: float = 0.0,
) -> None:
    with db() as c:
        c.execute(
            "INSERT INTO orders (ts, market_slug, condition_id, token_id, side, "
            "size, price, order_id, status, filled_size, error, dry_run, fee, "
            "spot_bps, loser_ask, breakeven) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                time.time(),
                market_slug,
                condition_id,
                token_id,
                side,
                size,
                price,
                order_id,
                status,
                filled_size,
                error,
                int(dry_run),
                fee,
                spot_bps,
                loser_ask,
                breakeven,
            ),
        )


def unresolved_condition_ids(dry_run: bool) -> list[str]:
    """All condition_ids we hold a filled order in but have no resolution recorded."""
    with db() as c:
        rows = c.execute(
            "SELECT DISTINCT o.condition_id FROM orders o "
            "LEFT JOIN resolutions r ON r.condition_id = o.condition_id "
            "WHERE o.status IN ('filled','matched','sim') AND o.dry_run=? AND r.condition_id IS NULL",
            (int(dry_run),),
        ).fetchall()
        return [r[0] for r in rows]


def unresolved_with_slug(dry_run: bool) -> list[tuple[str, str]]:
    """(condition_id, market_slug) pairs we still need to resolve."""
    with db() as c:
        rows = c.execute(
            "SELECT DISTINCT o.condition_id, o.market_slug FROM orders o "
            "LEFT JOIN resolutions r ON r.condition_id = o.condition_id "
            "WHERE o.status IN ('filled','matched','sim') AND o.dry_run=? AND r.condition_id IS NULL",
            (int(dry_run),),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]


def record_resolution(condition_id: str, winning_token: str) -> None:
    with db() as c:
        c.execute(
            "INSERT OR REPLACE INTO resolutions (condition_id, winning_token, resolved_ts) "
            "VALUES (?,?,?)",
            (condition_id, winning_token, time.time()),
        )


def open_positions_count(dry_run: bool) -> int:
    """Count distinct condition_ids where we've bought but not yet resolved."""
    return len(unresolved_condition_ids(dry_run))


def consecutive_losses(dry_run: bool, limit: int = 10) -> int:
    """Count consecutive losing resolved markets from most recent backwards."""
    with db() as c:
        rows = c.execute(
            "SELECT o.token_id, r.winning_token FROM orders o "
            "JOIN resolutions r ON r.condition_id=o.condition_id "
            "WHERE o.status IN ('filled','matched','sim') AND o.dry_run=? "
            "ORDER BY r.resolved_ts DESC LIMIT ?",
            (int(dry_run), limit),
        ).fetchall()
        streak = 0
        for token, winner in rows:
            if token == winner:
                break
            streak += 1
        return streak


def realized_pnl_today(dry_run: bool) -> float:
    """Sum gain/loss across resolved markets in the last 24h, net of fees."""
    cutoff = time.time() - 86400
    with db() as c:
        rows = c.execute(
            "SELECT o.size, o.price, o.token_id, r.winning_token "
            "FROM orders o JOIN resolutions r ON r.condition_id=o.condition_id "
            "WHERE o.status IN ('filled','matched','sim') AND o.dry_run=? AND r.resolved_ts > ?",
            (int(dry_run), cutoff),
        ).fetchall()
        return sum(net_pnl(size, price, token == winner) for size, price, token, winner in rows)


def sim_account(bankroll: float) -> dict:
    """Virtual paper account: cash, cost of open risk, realized PnL.

    Cash is debited by (cost + fee) the moment a simulated fill happens and
    credited back at resolution — winners redeem at $1.00/share, losers at $0.
    So the account can genuinely run out of money, exactly like the real one.

    Equity (cash + marked-to-market open positions) is computed by the caller,
    which is the layer that has live book prices.
    """
    with db() as c:
        spent, fees = c.execute(
            "SELECT COALESCE(SUM(size*price),0), COALESCE(SUM(fee),0) "
            "FROM orders WHERE status='sim' AND dry_run=1"
        ).fetchone()

        # Redemptions on resolved markets.
        payout, resolved_cost, resolved_fees, nres, nwin = c.execute(
            "SELECT "
            "  COALESCE(SUM(CASE WHEN o.token_id=r.winning_token THEN o.size ELSE 0 END),0),"
            "  COALESCE(SUM(o.size*o.price),0),"
            "  COALESCE(SUM(o.fee),0),"
            "  COUNT(*),"
            "  COALESCE(SUM(CASE WHEN o.token_id=r.winning_token THEN 1 ELSE 0 END),0) "
            "FROM orders o JOIN resolutions r ON r.condition_id=o.condition_id "
            "WHERE o.status='sim' AND o.dry_run=1"
        ).fetchone()

    cash = bankroll - (spent + fees) + payout
    return {
        "bankroll": bankroll,
        "cash": cash,
        "deployed": (spent + fees) - (resolved_cost + resolved_fees),  # cost of open risk
        "realized_pnl": payout - (resolved_cost + resolved_fees),
        "fills_total": None,
        "fills_resolved": nres,
        "wins": nwin,
        "gross_spent": spent,
        "total_fees": fees,
    }


def sim_open_positions() -> list[dict]:
    """Unresolved simulated holdings, aggregated per market+side.

    These are what the dashboard shows in OPEN POSITIONS while a window is
    still live — the real on-chain positions endpoint is empty in sim mode.
    """
    with db() as c:
        rows = c.execute(
            "SELECT o.market_slug, o.condition_id, o.token_id, o.side, "
            "       SUM(o.size) shares, SUM(o.size*o.price) cost, SUM(o.fee) fees, "
            "       COUNT(*) fills, MIN(o.ts) first_ts "
            "FROM orders o LEFT JOIN resolutions r ON r.condition_id=o.condition_id "
            "WHERE o.status='sim' AND o.dry_run=1 AND r.condition_id IS NULL "
            "GROUP BY o.condition_id, o.token_id "
            "ORDER BY first_ts DESC"
        ).fetchall()
    out = []
    for slug, cond, token, side, shares, cost, fees, fills, first_ts in rows:
        out.append({
            "market_slug": slug,
            "condition_id": cond,
            "token_id": token,
            "side": side,
            "shares": shares,
            "cost": cost,
            "fees": fees,
            "fills": fills,
            "avg_price": (cost / shares) if shares else 0.0,
            "first_ts": first_ts,
        })
    return out


def sim_recent_settlements(limit: int = 20) -> list[dict]:
    """Per-market outcomes, newest first — what the window actually paid."""
    with db() as c:
        rows = c.execute(
            "SELECT o.market_slug, o.side, SUM(o.size) shares, "
            "       SUM(o.size*o.price) cost, SUM(o.fee) fees, COUNT(*) fills, "
            "       SUM(CASE WHEN o.token_id=r.winning_token THEN o.size ELSE 0 END) payout, "
            "       MAX(o.token_id=r.winning_token) won, r.resolved_ts "
            "FROM orders o JOIN resolutions r ON r.condition_id=o.condition_id "
            "WHERE o.status='sim' AND o.dry_run=1 "
            "GROUP BY o.condition_id ORDER BY r.resolved_ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {
            "market_slug": slug, "side": side, "shares": sh, "cost": cost,
            "fees": fees, "fills": fills, "payout": payout, "won": bool(won),
            "pnl": payout - (cost + fees), "resolved_ts": rts,
        }
        for slug, side, sh, cost, fees, fills, payout, won, rts in rows
    ]


def kpi_report(bankroll: float) -> dict:
    """Bird's-eye quant scorecard, computed MARKET-level (the honest unit).

    A market is one bet: the bot may add ~25 fills to it, but it wins or loses
    as a whole. Fill-weighted stats flatter the result because winning markets
    accumulate more fills than losing ones, so everything here groups by market.
    """
    with db() as c:
        rows = c.execute(
            "SELECT o.condition_id, r.resolved_ts, "
            "       SUM(o.size*o.price) cost, SUM(o.fee) fees, "
            "       SUM(CASE WHEN o.token_id=r.winning_token THEN o.size ELSE 0 END) payout, "
            "       MAX(o.token_id=r.winning_token) won, "
            "       AVG(o.price) avg_price, AVG(o.spot_bps) avg_bps "
            "FROM orders o JOIN resolutions r ON r.condition_id=o.condition_id "
            "WHERE o.status='sim' AND o.dry_run=1 "
            "GROUP BY o.condition_id ORDER BY r.resolved_ts ASC"
        ).fetchall()
        open_cost = c.execute(
            "SELECT COALESCE(SUM(o.size*o.price+o.fee),0) FROM orders o "
            "LEFT JOIN resolutions r ON r.condition_id=o.condition_id "
            "WHERE o.status='sim' AND o.dry_run=1 AND r.condition_id IS NULL"
        ).fetchone()[0]

    pnls, won_flags, costs, bpses = [], [], [], []
    equity, peak, max_dd = bankroll, bankroll, 0.0
    curve = []
    for cond, rts, cost, fees, payout, won, avg_price, avg_bps in rows:
        pnl = payout - (cost + fees)
        pnls.append(pnl); won_flags.append(bool(won)); costs.append(cost + fees)
        if avg_bps is not None:
            bpses.append((abs(avg_bps), bool(won)))
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        curve.append(round(equity, 2))

    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total = sum(pnls)
    win_rate = len(wins) / n if n else None
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0

    # Expectancy per market and per dollar risked.
    expectancy = total / n if n else None
    total_cost = sum(costs)
    roi = (total / total_cost) if total_cost else None

    # Profit factor = gross wins / gross losses. >1 profitable.
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_win / gross_loss) if gross_loss else None

    # Loss/win ratio -> breakeven win rate the payoff structure demands.
    lw = (abs(avg_loss) / avg_win) if avg_win else None
    breakeven_wr = (lw / (1 + lw)) if lw else None

    # Sharpe-like: mean/stdev of per-market PnL (unitless, not annualized).
    sharpe = None
    if n > 1:
        mu = total / n
        var = sum((p - mu) ** 2 for p in pnls) / (n - 1)
        sd = var ** 0.5
        sharpe = (mu / sd) if sd else None

    # Wilson 95% CI on win rate vs the breakeven bar -> "is this conclusive?"
    ci_lo = ci_hi = verdict = None
    if n and win_rate is not None:
        z = 1.96
        d = 1 + z * z / n
        centre = (win_rate + z * z / (2 * n)) / d
        halfw = z * ((win_rate * (1 - win_rate) / n + z * z / (4 * n * n)) ** 0.5) / d
        ci_lo, ci_hi = max(0.0, centre - halfw), min(1.0, centre + halfw)
        if breakeven_wr is not None:
            if ci_hi < breakeven_wr:
                verdict = "LOSING"
            elif ci_lo > breakeven_wr:
                verdict = "WINNING"
            else:
                verdict = "INCONCLUSIVE"

    # Gate audit: win rate above vs below the config threshold.
    from bot.config import load as _load
    thr = _load().min_spot_offset_bps
    hi = [w for b, w in bpses if b >= thr * 2]      # strong signal (2x threshold)
    lo = [w for b, w in bpses if b < thr * 2]
    gate = {
        "n_with_bps": len(bpses),
        "strong_n": len(hi),
        "strong_wr": (sum(hi) / len(hi)) if hi else None,
        "weak_n": len(lo),
        "weak_wr": (sum(lo) / len(lo)) if lo else None,
        "split_bps": thr * 2,
    }

    return {
        "markets": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "total_pnl": total,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "expectancy": expectancy,
        "roi_on_cost": roi,
        "profit_factor": profit_factor,
        "breakeven_wr": breakeven_wr,
        "sharpe": sharpe,
        "max_drawdown": max_dd,
        "max_drawdown_pct": (max_dd / peak * 100) if peak else None,
        "ci_lo": ci_lo,
        "ci_hi": ci_hi,
        "verdict": verdict,
        "open_deployed": open_cost,
        "equity_curve": curve[-60:],   # last 60 markets for a sparkline
        "gate": gate,
        "markets_to_conclusive": max(0, 200 - n),  # rule of thumb from CI width
    }


def sim_report(window_sec: Optional[float] = None) -> dict:
    """Full simulation scorecard: PnL net of fees, plus per-price-bucket win
    rate measured against the breakeven that bucket actually requires.

    The bucket table is the whole point of running this. A 90% win rate means
    nothing on its own -- it's only good if you earned it at prices where
    breakeven was below 90%.
    """
    clause = ""
    params: list = [1]
    if window_sec:
        clause = " AND o.ts > ?"
        params.append(time.time() - window_sec)

    with db() as c:
        rows = c.execute(
            "SELECT o.size, o.price, o.token_id, o.ts, r.winning_token "
            "FROM orders o JOIN resolutions r ON r.condition_id=o.condition_id "
            "WHERE o.status IN ('filled','matched','sim') AND o.dry_run=?" + clause,
            params,
        ).fetchall()

        pending = c.execute(
            "SELECT COUNT(*) FROM orders o LEFT JOIN resolutions r "
            "ON r.condition_id=o.condition_id "
            "WHERE o.status IN ('filled','matched','sim') AND o.dry_run=1 "
            "AND r.condition_id IS NULL"
        ).fetchone()[0]

    buckets = [(0.80, 0.90), (0.90, 0.95), (0.95, 0.98), (0.98, 1.01)]
    agg = {f"{lo:.2f}-{hi:.2f}": {
        "n": 0, "wins": 0, "cost": 0.0, "fees": 0.0, "pnl": 0.0,
        "sum_breakeven": 0.0,
    } for lo, hi in buckets}

    total = {"n": 0, "wins": 0, "cost": 0.0, "fees": 0.0, "pnl": 0.0, "shares": 0.0}
    for size, price, token, _ts, winner in rows:
        won = token == winner
        fee = taker_fee(size, price)
        pnl = net_pnl(size, price, won)
        total["n"] += 1
        total["wins"] += int(won)
        total["cost"] += size * price
        total["fees"] += fee
        total["pnl"] += pnl
        total["shares"] += size
        for lo, hi in buckets:
            if lo <= price < hi:
                b = agg[f"{lo:.2f}-{hi:.2f}"]
                b["n"] += 1
                b["wins"] += int(won)
                b["cost"] += size * price
                b["fees"] += fee
                b["pnl"] += pnl
                b["sum_breakeven"] += breakeven_win_rate(price)
                break

    for b in agg.values():
        b["win_rate"] = b["wins"] / b["n"] if b["n"] else None
        b["breakeven"] = b["sum_breakeven"] / b["n"] if b["n"] else None
        b["edge_pts"] = (
            (b["win_rate"] - b["breakeven"]) if b["n"] and b["win_rate"] is not None else None
        )
        b.pop("sum_breakeven")

    total["win_rate"] = total["wins"] / total["n"] if total["n"] else None
    total["pnl_bps_of_cost"] = (
        (total["pnl"] / total["cost"]) * 10_000 if total["cost"] else None
    )
    total["pending"] = pending
    return {"total": total, "buckets": agg}
