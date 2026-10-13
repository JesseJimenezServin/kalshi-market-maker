"""
Simulates the RISK half of the strategy: if I had a resting bid on both
sides (a hypothetical "yes bid" at price P_y and a hypothetical "no bid" at
price P_n, sized S_y / S_n), what inventory would real trade flow have
handed me, and what's the mark-to-market P&L of that inventory over time?

This is deliberately conservative/approximate in Phase 0 -- the goal is a
directional read ("is adverse selection small or large relative to reward
income on this market"), not exact accounting. Known simplifications are
called out inline. Phase 1 (live, tiny real orders) is what gives us exact
numbers, because at that point OUR fills come directly from Kalshi's
fills/user_orders stream instead of being inferred from the public tape.

Kalshi's public trade prints (per API docs) include: yes_price (cents),
count (contracts), and taker_side ("yes" or "no" -- which side the
aggressor bought).

  - taker_side == "no"  => the aggressor bought NO, which means they sold
    YES into the resting YES bids. This is the flow that would fill OUR
    hypothetical yes-bid if our price is at or above the trade's yes_price
    (i.e. we were part of what got hit).
  - taker_side == "yes" => the aggressor bought YES, hitting the NO bids
    (equivalently, lifting the yes offer). This would fill our hypothetical
    no-bid if our no-side price is at or above (100 - yes_price).

We approximate our fill probability on a given trade as our share of the
resting size at that exact price level at the moment of the trade -- the
same "share" concept the reward formula itself uses.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class HypotheticalQuote:
    yes_price: int  # price (cents) of our resting YES bid
    yes_size: int
    no_price: int  # price (cents) of our resting NO bid
    no_size: int


@dataclass
class InventoryState:
    ticker: str
    net_yes_contracts: int = 0  # positive = long yes, negative = long no
    avg_entry_price: float = 0.0  # cents, cost basis of net_yes_contracts
    realized_pnl_cents: float = 0.0
    # Decomposition of realized_pnl_cents by HOW it was earned or lost.
    # They sum to realized_pnl_cents.
    #
    # Added 2026-10-13 because the Phase 0 autopsy needed four ad-hoc
    # scripts to separate these after the fact, and the single most
    # important finding -- that taker exits cost $1,267 against a $747 total
    # loss -- was only visible once they were pulled apart. That should be a
    # property of the schema, not an archaeology exercise.
    realized_passive_cents: float = 0.0      # closing fills we did NOT initiate
    realized_taker_cents: float = 0.0        # crossing the spread (stops, switches)
    realized_settlement_cents: float = 0.0   # resolved at 0/100
    fill_events: list[dict] = field(default_factory=list)
    max_inventory_hit: int = 0

    def _book_realized(self, delta: float, taker: bool) -> None:
        """Single place that moves realized_pnl_cents, so the decomposition
        cannot drift out of sync with the total."""
        self.realized_pnl_cents += delta
        if taker:
            self.realized_taker_cents += delta
        else:
            self.realized_passive_cents += delta

    def apply_yes_fill(self, price: int, size: int, max_inventory: int, taker: bool = False) -> int:
        """We bought `size` YES at `price`. Returns the size actually
        applied after clamping to max_inventory (rest assumed unfilled
        because we'd have pulled the quote)."""
        room = max_inventory - self.net_yes_contracts
        applied = max(0, min(size, room))
        if applied <= 0:
            return 0

        if self.net_yes_contracts >= 0:
            # adding to a long-yes (or flat) position
            total_cost = self.avg_entry_price * self.net_yes_contracts + price * applied
            self.net_yes_contracts += applied
            self.avg_entry_price = total_cost / self.net_yes_contracts
        else:
            # covering a short-yes position
            covered = min(applied, -self.net_yes_contracts)
            self._book_realized(covered * (self.avg_entry_price - price), taker)
            self.net_yes_contracts += applied
            if self.net_yes_contracts > 0:
                self.avg_entry_price = price  # flipped long

        self.max_inventory_hit = max(self.max_inventory_hit, abs(self.net_yes_contracts))
        self.fill_events.append({"side": "yes", "price": price, "size": applied, "ts": time.time()})
        return applied

    def apply_no_fill(self, no_price: int, size: int, max_inventory: int, taker: bool = False) -> int:
        """We bought `size` NO at `no_price` (equivalent to selling YES at
        100 - no_price)."""
        equiv_yes_price = 100 - no_price
        room = max_inventory + self.net_yes_contracts  # how much short-yes room we have
        applied = max(0, min(size, room))
        if applied <= 0:
            return 0

        if self.net_yes_contracts <= 0:
            total_cost = self.avg_entry_price * (-self.net_yes_contracts) + equiv_yes_price * applied
            self.net_yes_contracts -= applied
            self.avg_entry_price = total_cost / (-self.net_yes_contracts) if self.net_yes_contracts != 0 else 0.0
        else:
            covered = min(applied, self.net_yes_contracts)
            self._book_realized(covered * (equiv_yes_price - self.avg_entry_price), taker)
            self.net_yes_contracts -= applied
            if self.net_yes_contracts < 0:
                self.avg_entry_price = equiv_yes_price

        self.max_inventory_hit = max(self.max_inventory_hit, abs(self.net_yes_contracts))
        self.fill_events.append({"side": "no", "price": no_price, "size": applied, "ts": time.time()})
        return applied

    def mark_to_market_pnl_cents(self, current_mid_yes_price: float) -> float:
        """Unrealized P&L if we marked net_yes_contracts to current mid."""
        return self.net_yes_contracts * (current_mid_yes_price - self.avg_entry_price)

    def settle(self, result_yes: bool) -> float:
        """
        Resolve the position at settlement and book the result as REALIZED
        P&L. Returns the cents booked (signed). Idempotent: a flat position
        returns 0.0 and changes nothing.

        A binary market does not expire at its last mid -- it resolves to
        100 or 0, so the final P&L is a known fact, not an estimate. Before
        this existed, leaving a settled market just froze the position at
        whatever the last observed mid happened to be: on 2026-10-04 that
        left 145 contracts of KXTRUMPAPPROVE-26OCT04-E38.1 recorded at
        -$11.88 UNREALIZED forever, when the true outcome was determined.
        These contracts settle daily, so the bias applied every day.

        Uses the same sign convention as apply_yes_fill/apply_no_fill:
        closing a long yes at price P books net * (P - entry); closing a
        short yes books (-net) * (entry - P). Settlement is just that with
        P pinned to 100 (yes) or 0 (no).
        """
        net = self.net_yes_contracts
        if net == 0:
            return 0.0

        final_yes_price = 100.0 if result_yes else 0.0
        if net > 0:
            delta = net * (final_yes_price - self.avg_entry_price)
        else:
            delta = (-net) * (self.avg_entry_price - final_yes_price)

        self.realized_pnl_cents += delta
        self.realized_settlement_cents += delta
        self.net_yes_contracts = 0
        self.avg_entry_price = 0.0
        self.fill_events.append(
            {"side": "settlement", "price": final_yes_price, "size": abs(net), "ts": time.time()}
        )
        logger.info(
            "%s: settled %d contracts at %s -> realized %+.2f cents (total realized %+.2f)",
            self.ticker, abs(net), "YES" if result_yes else "NO", delta, self.realized_pnl_cents,
        )
        return delta


def process_trade_for_inventory(
    inventory: InventoryState,
    quote: HypotheticalQuote,
    trade_yes_price: int,
    trade_count: int,
    taker_side: str,
    my_share_at_price: float,
    max_inventory: int,
) -> None:
    """
    my_share_at_price: our estimated fraction of resting size at the
    relevant price level (from reward_simulator's share calculation, or a
    simple size-based heuristic). Used to avoid assuming we capture 100%
    of every print that touches our price.
    """
    if taker_side == "no" and trade_yes_price <= quote.yes_price:
        our_fill_size = round(trade_count * my_share_at_price)
        if our_fill_size > 0:
            applied = inventory.apply_yes_fill(quote.yes_price, our_fill_size, max_inventory)
            if applied > 0:
                logger.info("%s: simulated YES fill %d @ %dc (inventory now %d)",
                            inventory.ticker, applied, quote.yes_price, inventory.net_yes_contracts)

    elif taker_side == "yes" and (100 - trade_yes_price) <= quote.no_price:
        our_fill_size = round(trade_count * my_share_at_price)
        if our_fill_size > 0:
            applied = inventory.apply_no_fill(quote.no_price, our_fill_size, max_inventory)
            if applied > 0:
                logger.info("%s: simulated NO fill %d @ %dc (inventory now %d)",
                            inventory.ticker, applied, quote.no_price, inventory.net_yes_contracts)
