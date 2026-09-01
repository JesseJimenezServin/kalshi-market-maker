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
    fill_events: list[dict] = field(default_factory=list)
    max_inventory_hit: int = 0

    def apply_yes_fill(self, price: int, size: int, max_inventory: int) -> int:
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
            self.realized_pnl_cents += covered * (self.avg_entry_price - price)
            self.net_yes_contracts += applied
            if self.net_yes_contracts > 0:
                self.avg_entry_price = price  # flipped long

        self.max_inventory_hit = max(self.max_inventory_hit, abs(self.net_yes_contracts))
        self.fill_events.append({"side": "yes", "price": price, "size": applied, "ts": time.time()})
        return applied

    def apply_no_fill(self, no_price: int, size: int, max_inventory: int) -> int:
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
            self.realized_pnl_cents += covered * (equiv_yes_price - self.avg_entry_price)
            self.net_yes_contracts -= applied
            if self.net_yes_contracts < 0:
                self.avg_entry_price = equiv_yes_price

        self.max_inventory_hit = max(self.max_inventory_hit, abs(self.net_yes_contracts))
        self.fill_events.append({"side": "no", "price": no_price, "size": applied, "ts": time.time()})
        return applied

    def mark_to_market_pnl_cents(self, current_mid_yes_price: float) -> float:
        """Unrealized P&L if we marked net_yes_contracts to current mid."""
        return self.net_yes_contracts * (current_mid_yes_price - self.avg_entry_price)


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
