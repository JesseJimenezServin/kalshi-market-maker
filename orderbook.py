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
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class MarketBook:
    ticker: str
    sid: int | None = None
    seq: int | None = None
    yes_levels: dict[int, int] = field(default_factory=dict)  # price_cents -> size
    no_levels: dict[int, int] = field(default_factory=dict)   # price_cents -> size
    last_update_ts: float = 0.0
    stale: bool = True  # True until we've applied a valid snapshot

    def apply_snapshot(self, sid: int, seq: int, yes: list[list[int]], no: list[list[int]]) -> None:
        self.sid = sid
        self.seq = seq
        self.yes_levels = {price: size for price, size in yes}
        self.no_levels = {price: size for price, size in no}
        self.last_update_ts = time.time()
        self.stale = False

    def apply_delta(self, sid: int, seq: int, side: str, price: int, delta: int) -> bool:
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
        Dispatch a raw WebSocket message. Expected shapes (per Kalshi docs):
          {"type": "orderbook_snapshot", "msg": {"market_ticker": ..., "sid":
              ..., "seq": ..., "yes": [[price, size], ...], "no": [...]}}
          {"type": "orderbook_delta", "msg": {"market_ticker": ..., "sid":
              ..., "seq": ..., "side": "yes"|"no", "price": ..., "delta": ...}}
        """
        msg_type = msg.get("type")
        body = msg.get("msg", {})
        ticker = body.get("market_ticker")
        if not ticker:
            return

        book = self.get_or_create(ticker)

        if msg_type == "orderbook_snapshot":
            book.apply_snapshot(
                sid=body["sid"],
                seq=body["seq"],
                yes=body.get("yes", []),
                no=body.get("no", []),
            )
            logger.info("%s: applied snapshot (seq=%s)", ticker, body["seq"])

        elif msg_type == "orderbook_delta":
            ok = book.apply_delta(
                sid=body["sid"],
                seq=body["seq"],
                side=body["side"],
                price=body["price"],
                delta=body["delta"],
            )
            if not ok:
                logger.warning("%s: needs resync", ticker)

    def needs_resync(self, ticker: str) -> bool:
        book = self.books.get(ticker)
        return book is None or book.stale
