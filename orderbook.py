"""
Local order-book reconstruction from Kalshi's orderbook_delta WebSocket
channel.

Kalshi's book is YES-denominated: there's a single ladder of price levels,
and "bid" (long yes) / "ask" (long no, expressed as a yes-price) sides are
derived from that. We track raw levels as given by the API and expose
convenience views on top.

Each market has its own sequence number (`seq`) per subscription (`sid`).
If we ever detect a gap in `seq`, we must NOT keep applying deltas -- the
local book is now unreliable. We flag it as stale and request a fresh
snapshot (via `get_snapshot` on the same channel, per Kalshi's April 2026
addition) rather than silently trading on wrong data.

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
        self.yes_levels = {price: size for price, size in yes}
        self.no_levels = {price: size for price, size in no}
        self.last_update_ts = time.time()
        self.stale = False

    def apply_delta(self, sid: int, seq: int, side: str, price: int, delta: float) -> bool:
        """
        Returns True if applied cleanly, False if a gap was detected
        (caller should then request a fresh snapshot and mark stale).
        """
        if self.stale or self.sid != sid:
            logger.warning("%s: dropping delta, book is stale or sid mismatch", self.ticker)
            return False

        if self.seq is not None and seq != self.seq + 1:
            logger.error(
                "%s: sequence gap detected (expected %s, got %s) -- marking book stale",
                self.ticker, self.seq + 1 if self.seq is not None else None, seq,
            )
            self.stale = True
            return False

        levels = self.yes_levels if side == "yes" else self.no_levels
        new_size = levels.get(price, 0) + delta
        if new_size <= 0:
            levels.pop(price, None)
        else:
            levels[price] = new_size

        self.seq = seq
        self.last_update_ts = time.time()
        return True

    # ----------------------------------------------------------------
    # Convenience views used by the reward simulator
    # ----------------------------------------------------------------
    def best_yes_bid(self) -> tuple[int, int] | None:
        """Highest price a YES buyer is resting at. (price_cents, size)"""
        if not self.yes_levels:
            return None
        price = max(self.yes_levels)
        return price, self.yes_levels[price]

    def best_no_bid(self) -> tuple[int, int] | None:
        """Highest price a NO buyer is resting at, i.e. lowest implied YES ask."""
        if not self.no_levels:
            return None
        price = max(self.no_levels)
        return price, self.no_levels[price]

    def yes_side_sorted_desc(self) -> list[tuple[int, int]]:
        """Yes-side resting levels, best (highest) price first."""
        return sorted(self.yes_levels.items(), key=lambda kv: -kv[0])

    def no_side_sorted_desc(self) -> list[tuple[int, int]]:
        return sorted(self.no_levels.items(), key=lambda kv: -kv[0])


class OrderbookManager:
    """Tracks MarketBook state for every subscribed ticker."""

    def __init__(self):
        self.books: dict[str, MarketBook] = {}

    def get_or_create(self, ticker: str) -> MarketBook:
        if ticker not in self.books:
            self.books[ticker] = MarketBook(ticker=ticker)
        return self.books[ticker]

    def handle_message(self, msg: dict) -> None:
        """
        Dispatch a raw WebSocket message. Real shape CONFIRMED live
        2026-09-01 for orderbook_snapshot:
          {"type": "orderbook_snapshot", "sid": ..., "seq": ...,
           "msg": {"market_ticker": ..., "yes_dollars_fp": [["0.0100","3095.00"], ...],
                   "no_dollars_fp": [...]}}
        Note sid/seq are TOP-LEVEL on the message, not nested in "msg".

        orderbook_delta's exact field names have NOT been confirmed against
        a live message (thin/slow watched markets by design -- see
        PROJECT_STATUS.md). The parsing below is a best-effort inference
        from the confirmed "_dollars" (price) / "_fp" (size) convention
        seen everywhere else in Kalshi's API. If a real delta doesn't
        match, we log the full raw message once and mark the book stale
        (triggering a resync) rather than crash the whole WebSocket
        session -- check the logs and fix the field names here if that
        happens.
        """
        msg_type = msg.get("type")
        body = msg.get("msg", {})
        ticker = body.get("market_ticker")
        if not ticker:
            return

        book = self.get_or_create(ticker)

        if msg_type == "orderbook_snapshot":
            sid, seq = msg["sid"], msg["seq"]
            book.apply_snapshot(
                sid=sid,
                seq=seq,
                yes=_parse_dollar_fp_levels(body.get("yes_dollars_fp", [])),
                no=_parse_dollar_fp_levels(body.get("no_dollars_fp", [])),
            )
            logger.info("%s: applied snapshot (seq=%s)", ticker, seq)

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
                return

            ok = book.apply_delta(sid=sid, seq=seq, side=side, price=price, delta=delta)
            if not ok:
                logger.warning("%s: needs resync", ticker)

    def needs_resync(self, ticker: str) -> bool:
        book = self.books.get(ticker)
        return book is None or book.stale
