"""
Local order-book reconstruction from Kalshi's orderbook_delta WebSocket
channel.

Kalshi's book is YES-denominated: there's a single ladder of price levels,
and "bid" (long yes) / "ask" (long no, expressed as a yes-price) sides are
derived from that. We track raw levels as given by the API and expose
convenience views on top.

`seq` is a SINGLE monotonic counter shared across the WHOLE subscription
(`sid`), interleaved across every ticker subscribed under it -- CONFIRMED
live 2026-09-01: with 4 tickers subscribed, their initial snapshots
arrived at seq 1, 2, 3, 4 respectively, one each, not independent
per-ticker sequences. Gap detection therefore happens once, globally, per
sid (see OrderbookManager._last_seq_by_sid) -- NOT per ticker. Tracking it
per-ticker (this module's original design) meant any OTHER ticker's
activity would look like a sequence "gap" for a quiet ticker the moment
it received its own next real update, causing spurious staleness/resync
churn on every ticker whenever a busy one was interleaved with quiet
ones. If we ever detect a real gap, we must NOT keep applying deltas --
the local book(s) may now be unreliable. We flag them stale and request a
fresh snapshot (via `get_snapshot` on the same channel, per Kalshi's
April 2026 addition) rather than silently trading on wrong data.

Price/size wire format (CONFIRMED live 2026-09-01 against a real
orderbook_snapshot message -- Kalshi's API has since moved to this shape,
differing from what this codebase originally assumed without live access,
see README): prices are dollar-denominated decimal strings with a 1-cent
tick size (e.g. "0.0100" == 1 cent), and sizes/counts are "_fp"
(fixed-point) decimal strings that CAN be fractional (e.g. "5787.01").
dollars_to_cents()/fp_to_size() below convert these at the parsing
boundary; everything downstream of MarketBook still works in integer
cents (lossless, since 1-cent is the real tick size) and float sizes.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


def dollars_to_cents(price_str) -> int:
    """"_dollars"-suffixed Kalshi price fields -> integer cents."""
    return round(float(price_str) * 100)


def fp_to_size(size_str) -> float:
    """"_fp"-suffixed Kalshi size/count fields -> float (may be fractional)."""
    return float(size_str)


# A level at or below this size is treated as absent.
#
# Sizes are floats because Kalshi's "_fp" fields are genuinely fractional, so
# a level being cancelled down to nothing does NOT reliably land on exactly
# 0.0 -- it lands on binary-float residue like 2.8e-14. apply_delta
# originally popped a level only on `new_size <= 0`, so every such level
# stayed in the book forever at an effectively-zero size.
#
# That was not cosmetic. best_yes_bid()/best_no_bid() return max(price) over
# whatever keys exist, so a phantom level became the best bid, which fed the
# mid price, the quote placement, the fill test in
# adverse_selection_sim.process_trade_for_inventory, AND the mark the P&L is
# measured against. Measured 2026-10-03 on the live DB: 31.6% of all logged
# price levels were phantoms, the best level was a phantom in 51208 sampled
# snapshots, and one book showed a best YES bid of 12c at size 2.8e-14 when
# the real best bid was 9c with 634 contracts -- a 1.5c mid error on a 10c
# market. 99.6% of the run's reported $162 "profit" came from the three
# markets whose books were 39-60% phantoms; every clean-book market made $0.
#
# Deliberately a tolerance and NOT integer rounding: 11% of real sizes are
# legitimately fractional (e.g. 5787.01), so coercing to int would corrupt
# live data to fix a float-residue bug. 1e-6 is ~11 orders of magnitude
# above the observed residue and ~6 below the smallest meaningful size.
SIZE_EPSILON = 1e-6


def _live_levels(levels: dict[int, float]) -> dict[int, float]:
    """Levels with real size. Belt-and-braces: apply_snapshot/apply_delta
    already maintain this invariant, but every read path filters too, so no
    future write path can reintroduce a phantom best bid silently."""
    return {price: size for price, size in levels.items() if size > SIZE_EPSILON}


def _parse_dollar_fp_levels(levels: list[list]) -> list[tuple[int, float]]:
    return [(dollars_to_cents(price), fp_to_size(size)) for price, size in levels]


@dataclass
class MarketBook:
    ticker: str
    sid: int | None = None
    seq: int | None = None
    yes_levels: dict[int, float] = field(default_factory=dict)  # price_cents -> size
    no_levels: dict[int, float] = field(default_factory=dict)   # price_cents -> size
    last_update_ts: float = 0.0
    stale: bool = True  # True until we've applied a valid snapshot

    def apply_snapshot(self, sid: int, seq: int, yes: list[tuple[int, float]], no: list[tuple[int, float]]) -> None:
        self.sid = sid
        self.seq = seq
        # Filter here too: a snapshot can legitimately carry a zero level,
        # and the book invariant should hold from the first message.
        self.yes_levels = {price: size for price, size in yes if size > SIZE_EPSILON}
        self.no_levels = {price: size for price, size in no if size > SIZE_EPSILON}
        self.last_update_ts = time.time()
        self.stale = False

    def apply_delta(self, side: str, price: int, delta: float) -> None:
        """
        Applies one price-level delta. Sequence/gap detection happens at
        the OrderbookManager level (shared across the whole subscription),
        not here -- by the time this is called, the caller has already
        confirmed the global sequence is intact and this book isn't stale.
        """
        levels = self.yes_levels if side == "yes" else self.no_levels
        new_size = levels.get(price, 0) + delta
        if new_size <= SIZE_EPSILON:
            # <= EPSILON, not <= 0: float residue from cancelling a
            # fractional level down to nothing is what created the phantom
            # levels described at SIZE_EPSILON.
            levels.pop(price, None)
        else:
            levels[price] = new_size

        self.last_update_ts = time.time()

    # ----------------------------------------------------------------
    # Convenience views used by the reward simulator
    # ----------------------------------------------------------------
    def best_yes_bid(self) -> tuple[int, float] | None:
        """Highest price a YES buyer is resting at. (price_cents, size)"""
        live = _live_levels(self.yes_levels)
        if not live:
            return None
        price = max(live)
        return price, live[price]

    def best_no_bid(self) -> tuple[int, float] | None:
        """Highest price a NO buyer is resting at, i.e. lowest implied YES ask."""
        live = _live_levels(self.no_levels)
        if not live:
            return None
        price = max(live)
        return price, live[price]

    def yes_side_sorted_desc(self) -> list[tuple[int, float]]:
        """Yes-side resting levels, best (highest) price first."""
        return sorted(_live_levels(self.yes_levels).items(), key=lambda kv: -kv[0])

    def no_side_sorted_desc(self) -> list[tuple[int, float]]:
        return sorted(_live_levels(self.no_levels).items(), key=lambda kv: -kv[0])


class OrderbookManager:
    """Tracks MarketBook state for every subscribed ticker."""

    def __init__(self):
        self.books: dict[str, MarketBook] = {}
        # seq is shared across the WHOLE subscription (per sid), not
        # per-ticker -- see the module docstring. One running "last seen"
        # value per sid, not per ticker.
        self._last_seq_by_sid: dict[int, int] = {}

    def get_or_create(self, ticker: str) -> MarketBook:
        if ticker not in self.books:
            self.books[ticker] = MarketBook(ticker=ticker)
        return self.books[ticker]

    def handle_message(self, msg: dict) -> list[str]:
        """
        Dispatch a raw WebSocket message. Returns the list of tickers that
        need a fresh resync snapshot as a result of processing this
        message (empty if none) -- the caller (ws_client.py) is
        responsible for actually requesting it.

        Real shape CONFIRMED live 2026-09-01 for orderbook_snapshot:
          {"type": "orderbook_snapshot", "sid": ..., "seq": ...,
           "msg": {"market_ticker": ..., "yes_dollars_fp": [["0.0100","3095.00"], ...],
                   "no_dollars_fp": [...]}}
        Note sid/seq are TOP-LEVEL on the message, not nested in "msg".

        orderbook_delta's exact field names have NOT been confirmed against
        a live message shape mismatch (only a sequencing bug was found so
        far, live 2026-09-01 -- see PROJECT_STATUS.md). The parsing below
        is a best-effort inference from the confirmed "_dollars" (price) /
        "_fp" (size) convention seen everywhere else in Kalshi's API. If a
        real delta doesn't match, we log the full raw message once and
        mark that book stale (triggering a resync) rather than crash the
        whole WebSocket session.
        """
        msg_type = msg.get("type")
        body = msg.get("msg", {})
        ticker = body.get("market_ticker")
        if not ticker:
            return []

        book = self.get_or_create(ticker)

        if msg_type == "orderbook_snapshot":
            sid, seq = msg["sid"], msg["seq"]
            book.apply_snapshot(
                sid=sid,
                seq=seq,
                yes=_parse_dollar_fp_levels(body.get("yes_dollars_fp", [])),
                no=_parse_dollar_fp_levels(body.get("no_dollars_fp", [])),
            )
            # A snapshot is a fresh baseline for the WHOLE subscription's
            # sequence counter (it consumes a seq slot too), not just this
            # ticker -- confirmed live: 4 tickers' snapshots landed at
            # seq 1,2,3,4 respectively.
            self._last_seq_by_sid[sid] = seq
            logger.info("%s: applied snapshot (seq=%s)", ticker, seq)
            return []

        elif msg_type == "orderbook_delta":
            try:
                sid, seq = msg["sid"], msg["seq"]
                side = body["side"]
                price_raw = body.get("price_dollars", body.get("price"))
                delta_raw = body.get("delta_fp", body.get("delta"))
                if price_raw is None or delta_raw is None:
                    raise KeyError("no recognized price/delta field in orderbook_delta body")
                price = dollars_to_cents(price_raw)
                delta = fp_to_size(delta_raw)
            except (KeyError, ValueError, TypeError):
                logger.error(
                    "%s: unrecognized orderbook_delta shape, marking stale -- raw message: %s",
                    ticker, msg,
                )
                book.stale = True
                return [ticker]

            expected = self._last_seq_by_sid.get(sid)
            self._last_seq_by_sid[sid] = seq

            if expected is not None and seq != expected + 1:
                # A gap ANYWHERE in this shared stream means we can't
                # trust any book under this sid until each is resynced --
                # we don't know which ticker(s) the lost message(s)
                # actually belonged to, so treat all of them as suspect.
                affected = list(self.books.keys())
                for t in affected:
                    self.books[t].stale = True
                logger.error(
                    "sid=%s: sequence gap detected (expected %s, got %s) -- "
                    "marking all %d tracked book(s) stale for resync",
                    sid, expected + 1, seq, len(affected),
                )
                return affected

            if book.stale:
                logger.warning("%s: dropping delta, book is stale (awaiting resync)", ticker)
                return []

            book.apply_delta(side=side, price=price, delta=delta)
            return []

        return []

    def needs_resync(self, ticker: str) -> bool:
        book = self.books.get(ticker)
        return book is None or book.stale

    def remove(self, ticker: str) -> None:
        """Drops local book state for a ticker we're no longer watching
        (e.g. after a market switch) -- avoids stale/orphaned data lingering
        in memory or getting picked up by anything iterating self.books."""
        self.books.pop(ticker, None)
