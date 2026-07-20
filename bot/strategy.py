"""Late-window convergence buying, calibrated to bonereaper's measured fills.

Rebuilt 2026-07-20 from 13,914 of his BTC-5m trades over 2026-07-18..20
(research/bonereaper_live_2026-07-20.md). What the data actually shows:

  - 100% BUY, 0 SELL across every market. He never exits; he holds to
    redemption. (unchanged from the May analysis)
  - Side-neutral: 6,866 Up vs 7,048 Down. Not a directional bet.
  - ~24 fills per market (median 20, max 291) across ~292 markets/day. He
    scales into a market repeatedly rather than taking one shot.
  - Volume-weighted median entry at 196s into the 300s window => t_remaining
    ~104s. Dollars skew late even though trade COUNT is fairly uniform.
  - He sizes UP as price converges: the 0.98-0.99 bucket is only 3.6% of his
    trades but 35.4% of his dollars.

The previous version of this file fired once per market, in the last 35s, at a
fixed 5 shares, capped at 0.98 -- which excluded his single largest volume
bucket. All four of those are corrected here.

Fees matter: we are always taker, so every fill pays
shares * 0.07 * p * (1-p). See bot/fees.py. A position is only +EV if the
realized win rate at that price beats `fees.breakeven_win_rate(p)`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from bot.book import TopOfBook
from bot.config import Config
from bot.fees import breakeven_win_rate, taker_fee
from bot.markets import LiveMarket
from bot.spot import favored_side


@dataclass(frozen=True)
class Decision:
    action: str       # 'BUY', 'SKIP_TIME', 'SKIP_PRICE', 'SKIP_SIZE', 'SKIP_AMBIGUOUS'
    side: Optional[str] = None       # 'UP' or 'DOWN'
    token_id: Optional[str] = None
    price: Optional[float] = None
    size: Optional[float] = None
    reason: str = ""
    fee: float = 0.0                 # modelled taker fee for this fill
    breakeven: float = 0.0           # win rate needed at this price


def decide(
    cfg: Config,
    market: LiveMarket,
    book_up: TopOfBook,
    book_down: TopOfBook,
    t_remaining: float,
    spot_bps: Optional[float] = None,
) -> Decision:
    if t_remaining > cfg.seconds_before_close:
        return Decision(action="SKIP_TIME",
                        reason=f"t_remaining={t_remaining:.1f}s > {cfg.seconds_before_close}s")
    if t_remaining < cfg.min_t_remaining_sec:
        return Decision(action="SKIP_TIME",
                        reason=f"t_remaining={t_remaining:.1f}s < {cfg.min_t_remaining_sec}s buffer")

    candidates: list[tuple[str, str, float, float]] = []  # (side, token, ask, avail)
    for side, token, book in (
        ("UP", market.up_token, book_up),
        ("DOWN", market.down_token, book_down),
    ):
        ask = book.best_ask
        if ask is None:
            continue
        if not (cfg.loser_floor <= ask <= cfg.max_entry_price):
            continue
        candidates.append((side, token, ask, book.ask_size or 0.0))

    if not candidates:
        for side, book in (("UP", book_up), ("DOWN", book_down)):
            if book.best_ask is None:
                continue
            if book.best_ask > cfg.max_entry_price:
                return Decision(action="SKIP_PRICE", side=side, price=book.best_ask,
                                reason=f"{side} ask={book.best_ask} > cap {cfg.max_entry_price}")
        return Decision(action="SKIP_PRICE", reason="no side in entry band")

    if len(candidates) == 2:
        # Both sides priced as plausible winners -- they can't both win, so the
        # book is genuinely uncertain. He is side-neutral but not simultaneous.
        return Decision(action="SKIP_AMBIGUOUS",
                        reason=f"both sides in band: UP={book_up.best_ask}, DOWN={book_down.best_ask}")

    side, token, ask, avail = candidates[0]

    # --- Binance spot gate -------------------------------------------------
    # Backtest (584 windows): without this, the book's favoured side wins 81.3%
    # of the time at t_rem 60s -- below breakeven at every price we trade. With
    # a >=5bps agreement filter it is 96.0%. Fail CLOSED: a missing feed means
    # no trade, never an ungated one.
    if cfg.use_spot_gate:
        if spot_bps is None:
            return Decision(action="SKIP_NO_SPOT", side=side, price=ask,
                            reason="spot feed unavailable/stale — gate fails closed")
        if abs(spot_bps) < cfg.min_spot_offset_bps:
            return Decision(action="SKIP_SPOT_FLAT", side=side, price=ask,
                            reason=f"|{spot_bps:+.2f}bps| < {cfg.min_spot_offset_bps}bps threshold — too close to call")
        want_side = favored_side(spot_bps)
        if side != want_side:
            return Decision(action="SKIP_SPOT_DISAGREE", side=side, price=ask,
                            reason=f"book favors {side} but spot {spot_bps:+.2f}bps favors {want_side}")

    want = cfg.size_for_price(ask)
    if want <= 0:
        return Decision(action="SKIP_PRICE", side=side, price=ask,
                        reason=f"{side} ask={ask} outside size ladder")

    # Only claim what is actually resting on the ask -- a FOK for more than the
    # displayed size would be killed, so simulating a larger fill would be a lie.
    size = min(want, int(avail)) if cfg.respect_book_depth else want
    if size < cfg.min_order_shares:
        return Decision(action="SKIP_SIZE", side=side, price=ask, size=avail,
                        reason=f"{side} depth {avail} < min {cfg.min_order_shares} shares")

    spot_note = f" spot={spot_bps:+.2f}bps" if spot_bps is not None else ""
    return Decision(
        action="BUY",
        side=side,
        token_id=token,
        price=ask,
        size=size,
        fee=taker_fee(size, ask),
        breakeven=breakeven_win_rate(ask),
        reason=f"{side} ask={ask} depth={avail} take={size} t_rem={t_remaining:.1f}s{spot_note}",
    )
