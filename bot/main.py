"""Main event loop. Dry-run by default; --live to actually trade."""
from __future__ import annotations

import os
import argparse
import logging
import time
from typing import Optional

from bot import config, store
from bot.book import fetch_book
from bot.markets import LiveMarket, fetch_live_market
from bot.orders import build_client, place_buy_fok
from bot.resolver import resolve_pending
from bot.risk import allowed_to_trade
from bot.spot import FEED
from bot.strategy import decide

log = logging.getLogger("bot")


def loop(live: bool) -> None:
    cfg = config.load()
    dry_run = not live

    # The bonereaper-calibrated sizing ladder scales up to ~$200 per fill and
    # allows ~25 fills per market. That is a simulation profile. Refuse to arm
    # it against real money unless someone deliberately clears sim_only.
    if live and cfg.sim_only:
        raise SystemExit(
            "REFUSING TO GO LIVE: config.sim_only=True.\n"
            "  This config is calibrated for simulation -- up to "
            f"{cfg.max_entries_per_market} fills/market at up to "
            f"${max(u for _, _, u in cfg.size_ladder_usdc):.0f} each.\n"
            "  Set sim_only=False in bot/config.py and size the ladder to your\n"
            "  actual bankroll before running --live."
        )

    client = build_client(cfg) if live else None

    # Write our OWN pid for the dashboard's liveness check. The launch scripts
    # derive it from `ps -p $!`, which under Git Bash reports the nohup wrapper
    # rather than this process -- so the dashboard read a pid that was never
    # alive and always showed OFFLINE. Self-reporting is authoritative, and
    # works identically on Linux/in the container.
    try:
        (config.ROOT / "bot.win.pid").write_text(str(os.getpid()))
        # Also write the mode. The launch scripts do this locally, but in the
        # container run_service.py starts us directly and never did -- so the
        # dashboard's mode chip read a missing file and showed OFFLINE while
        # the bot was demonstrably RUNNING right next to it.
        (config.ROOT / "bot.mode").write_text("live" if live else "paper")
    except Exception as e:
        log.warning("could not write pid/mode file: %s", e)

    # Materialize the DB schema up front.
    with store.db():
        pass

    if cfg.use_spot_gate:
        FEED.start()
        # Give the websocket a moment to land its first tick, otherwise the
        # first window's decisions all fail closed on a cold feed.
        for _ in range(50):
            if FEED.is_healthy():
                break
            time.sleep(0.1)
        log.info("spot feed healthy=%s px=%s gate>=%.1fbps",
                 FEED.is_healthy(), FEED.last_price(), cfg.min_spot_offset_bps)

    log.info(
        "starting bot dry_run=%s band=%.2f-%.2f window=%ss max_entries/mkt=%s scale=%.2f",
        dry_run, cfg.loser_floor, cfg.max_entry_price,
        cfg.seconds_before_close, cfg.max_entries_per_market, cfg.size_scale,
    )

    current: Optional[LiveMarket] = None
    last_market_refresh = 0.0
    last_resolve_check = 0.0
    # bonereaper averages ~24 fills per market, so we count entries per market
    # rather than locking the market out after a single shot.
    entries_this_window: dict[str, int] = {}
    last_entry_ts: dict[str, float] = {}
    # Side we committed to per market. Without this, a market that oscillates
    # across the entry band lets us accumulate BOTH outcomes -- e.g. UP @0.82
    # then DOWN @0.84 is $1.66 spent on a ticket that pays at most $1.00. Once
    # we pick a side in a market we only ever add to that side.
    committed_side: dict[str, str] = {}

    # Settle anything left unresolved from prior runs before we start.
    resolve_pending(cfg, dry_run)

    while True:
        now = time.time()

        # Periodically resolve filled positions whose markets have closed.
        if now - last_resolve_check > 30:
            try:
                resolve_pending(cfg, dry_run)
            except Exception as e:
                log.warning("resolve_pending failed: %s", e)
            last_resolve_check = now

        # Refresh live market every 5s, or when the current one expires.
        if current is None or now > current.end_ts + 5 or (now - last_market_refresh) > 5:
            try:
                m = fetch_live_market(cfg.gamma_host, cfg.series_slug)
            except Exception as e:
                log.warning("market discovery failed: %s", e)
                m = None
            last_market_refresh = now
            if m and (current is None or m.condition_id != current.condition_id):
                current = m
                log.info("new live market: %s end_ts=%.0f", current.market_slug, current.end_ts)
            elif m is None:
                current = None

        if current is None:
            time.sleep(1.0)
            continue

        t_rem = current.t_remaining(now)
        if t_rem <= 0:
            time.sleep(0.5)
            continue

        # Cap entries per market and space them out, mirroring his cadence.
        n_entries = entries_this_window.get(current.condition_id, 0)
        if n_entries >= cfg.max_entries_per_market:
            time.sleep(cfg.poll_interval_sec)
            continue
        since_last = now - last_entry_ts.get(current.condition_id, 0.0)
        if since_last < cfg.min_seconds_between_entries:
            time.sleep(cfg.poll_interval_sec)
            continue

        # Only fetch books when we're in (or near) the buy window — save bandwidth.
        if t_rem > cfg.seconds_before_close + 10:
            time.sleep(min(t_rem - cfg.seconds_before_close, 5.0))
            continue

        try:
            book_up = fetch_book(cfg.clob_host, current.up_token)
            book_down = fetch_book(cfg.clob_host, current.down_token)
        except Exception as e:
            log.warning("book fetch failed: %s", e)
            time.sleep(cfg.poll_interval_sec)
            continue

        spot_bps = FEED.offset_bps(int(current.start_ts)) if cfg.use_spot_gate else None
        d = decide(cfg, current, book_up, book_down, t_rem, spot_bps=spot_bps)

        if d.action != "BUY":
            store.log_decision(
                market_slug=current.market_slug,
                condition_id=current.condition_id,
                token_id=d.token_id,
                side=d.side,
                t_remaining=t_rem,
                ask_price=d.price,
                ask_size=None,
                action=d.action,
                reason=d.reason,
                dry_run=dry_run,
                fee=d.fee,
                breakeven=d.breakeven,
            )
            time.sleep(cfg.poll_interval_sec)
            continue

        locked = committed_side.get(current.condition_id)
        if locked is not None and d.side != locked:
            store.log_decision(
                market_slug=current.market_slug,
                condition_id=current.condition_id,
                token_id=d.token_id,
                side=d.side,
                t_remaining=t_rem,
                ask_price=d.price,
                ask_size=d.size,
                action="SKIP_SIDE_LOCK",
                reason=f"committed to {locked} in this market, book now favors {d.side}",
                dry_run=dry_run,
            )
            time.sleep(cfg.poll_interval_sec)
            continue

        # Virtual cash constraint. A paper account that can spend money it does
        # not have produces PnL that could never have been earned.
        if dry_run:
            need = d.size * d.price + d.fee
            cash = store.sim_account(cfg.sim_bankroll_usd)["cash"]
            if need > cash:
                store.log_decision(
                    market_slug=current.market_slug,
                    condition_id=current.condition_id,
                    token_id=d.token_id,
                    side=d.side,
                    t_remaining=t_rem,
                    ask_price=d.price,
                    ask_size=d.size,
                    action="SKIP_NO_CASH",
                    reason=f"need ${need:.2f} but cash ${cash:.2f}",
                    dry_run=dry_run,
                )
                time.sleep(cfg.poll_interval_sec)
                continue

        ok, why = allowed_to_trade(cfg, dry_run)
        if not ok:
            store.log_decision(
                market_slug=current.market_slug,
                condition_id=current.condition_id,
                token_id=d.token_id,
                side=d.side,
                t_remaining=t_rem,
                ask_price=d.price,
                ask_size=d.size,
                action="SKIP_RISK",
                reason=why,
                dry_run=dry_run,
            )
            log.info("risk gate: %s", why)
            time.sleep(cfg.poll_interval_sec)
            continue

        log.info(
            "BUY %s %s sz=%s @ %s  fee=$%.4f  need_wr=%.1f%%  t_rem=%.1fs  dry=%s (#%d)",
            d.side, current.market_slug, d.size, d.price, d.fee,
            d.breakeven * 100, t_rem, dry_run, n_entries + 1,
        )
        store.log_decision(
            market_slug=current.market_slug,
            condition_id=current.condition_id,
            token_id=d.token_id,
            side=d.side,
            t_remaining=t_rem,
            ask_price=d.price,
            ask_size=d.size,
            action="BUY",
            reason=d.reason,
            dry_run=dry_run,
            fee=d.fee,
            breakeven=d.breakeven,
        )

        if dry_run:
            # Simulated fill: recorded as a real position so the resolver marks
            # it to the actual outcome, but no order ever leaves this process.
            store.log_order(
                market_slug=current.market_slug,
                condition_id=current.condition_id,
                token_id=d.token_id,
                side=d.side,
                size=d.size,
                price=d.price,
                order_id=None,
                status="sim",
                filled_size=d.size,
                dry_run=True,
                fee=d.fee,
            )
        else:
            assert client is not None
            result = place_buy_fok(
                client,
                token_id=d.token_id,
                price=d.price,
                size=d.size,
                tick_size=current.tick_size,
                neg_risk=current.neg_risk,
            )
            store.log_order(
                market_slug=current.market_slug,
                condition_id=current.condition_id,
                token_id=d.token_id,
                side=d.side,
                size=d.size,
                price=d.price,
                order_id=result.order_id,
                status="filled" if result.filled_size and result.filled_size > 0 else result.status,
                filled_size=result.filled_size,
                error=result.error,
                dry_run=False,
                fee=d.fee,
            )
            log.info("order result: status=%s filled=%s id=%s err=%s",
                     result.status, result.filled_size, result.order_id, result.error)

        entries_this_window[current.condition_id] = n_entries + 1
        last_entry_ts[current.condition_id] = now
        committed_side.setdefault(current.condition_id, d.side)
        # Bound memory on a long run -- these dicts otherwise grow ~288/day.
        if len(entries_this_window) > 500:
            for k in list(entries_this_window)[:250]:
                entries_this_window.pop(k, None)
                last_entry_ts.pop(k, None)
                committed_side.pop(k, None)
        time.sleep(cfg.poll_interval_sec)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--live", action="store_true", help="actually place orders (default: dry-run)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        loop(live=args.live)
    except KeyboardInterrupt:
        log.info("shutdown")


if __name__ == "__main__":
    main()
