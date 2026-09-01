"""
Active quoting policy: replaces the naive touch-joining rule that used to
live in MarketRuntimeState.refresh_quote_from_book with one that reacts to
realized volatility and hypothetical inventory (risk_engine.py). Still
fully read-only/paper-trading -- compute_quote only returns a
HypotheticalQuote (or None); maybe_execute_stop_loss only mutates the
in-memory InventoryState passed to it. Neither function ever calls into
rest_client.py/ws_client.py or issues any network request.
"""

from __future__ import annotations

import time

import config
from adverse_selection_sim import HypotheticalQuote, InventoryState
from orderbook import MarketBook
from risk_engine import ELEVATED, HIGH


def _clamp_price(price: int) -> int:
    return max(1, min(99, price))


def compute_quote(
    book: MarketBook,
    risk_level: str,
    inventory: InventoryState,
    base_size: int,
    max_inventory: int,
) -> HypotheticalQuote | None:
    """
    Base case (risk_level LOW, flat inventory): join best bid each side at
    base_size, identical to the old refresh_quote_from_book behavior.

    Inventory skew: shift yes_price down / no_price up proportional to
    net_yes_contracts / max_inventory, capped at
    config.MAX_INVENTORY_SKEW_TICKS. A long-yes position skews the quote to
    encourage getting flatter (worse yes bid so we're less likely to buy
    more yes; better/higher no bid so we're more likely to sell yes via a
    no fill) -- and symmetrically for a short-yes/long-no position.

    Risk-level reaction (applied after skew):
      LOW      -> skewed touch-join, unchanged size.
      ELEVATED -> skewed quote widened by config.WIDEN_TICKS_ELEVATED ticks
                  off the best bid on each side.
      HIGH     -> quote pulled entirely (returns None). The caller is
                  responsible for separately invoking maybe_execute_stop_loss
                  to decide whether inventory should be actively flattened.
    """
    if risk_level == HIGH:
        return None

    best_yes = book.best_yes_bid()
    best_no = book.best_no_bid()
    if best_yes is None or best_no is None:
        return None

    yes_price, no_price = best_yes[0], best_no[0]

    if max_inventory > 0:
        skew_ticks = round(
            (inventory.net_yes_contracts / max_inventory) * config.MAX_INVENTORY_SKEW_TICKS
        )
        skew_ticks = max(-config.MAX_INVENTORY_SKEW_TICKS, min(config.MAX_INVENTORY_SKEW_TICKS, skew_ticks))
    else:
        skew_ticks = 0

    # Long yes (skew_ticks > 0): quote a worse yes bid, better no bid.
    # Short yes / long no (skew_ticks < 0): quote a better yes bid, worse no bid.
    yes_price -= skew_ticks
    no_price += skew_ticks

    if risk_level == ELEVATED:
        yes_price -= config.WIDEN_TICKS_ELEVATED
        no_price -= config.WIDEN_TICKS_ELEVATED

    yes_price = _clamp_price(yes_price)
    no_price = _clamp_price(no_price)

    return HypotheticalQuote(
        yes_price=yes_price,
        yes_size=base_size,
        no_price=no_price,
        no_size=base_size,
    )


def maybe_execute_stop_loss(
    book: MarketBook,
    inventory: InventoryState,
    risk_level: str,
    max_inventory: int,
    hard_stop_fraction: float,
) -> dict | None:
    """
    Simulates an ACTIVE taker flatten (crossing the spread to close our
    hypothetical position) when risk_level is HIGH and our inventory
    fraction is at/above hard_stop_fraction. This mutates `inventory`
    directly via apply_yes_fill/apply_no_fill and is a DISTINCT simulated
    event from passive fills recorded by process_trade_for_inventory.

    Returns a dict of fill details for logging, or None if no flatten
    happened this tick (risk not HIGH, inventory under threshold, size
    already zero, or the book is missing the side needed to cross).
    """
    if risk_level != HIGH:
        return None
    if max_inventory <= 0:
        return None

    net = inventory.net_yes_contracts
    if net == 0:
        return None
    if abs(net) / max_inventory < hard_stop_fraction:
        return None

    if net > 0:
        # Long yes -> sell yes as taker, i.e. hit the best resting YES bid.
        best_yes = book.best_yes_bid()
        if best_yes is None:
            return None
        cross_price = best_yes[0]
        size = net
        applied = inventory.apply_no_fill(no_price=100 - cross_price, size=size, max_inventory=max_inventory)
        side = "no"  # the fill is booked as a NO buy (equiv. selling YES)
        price = 100 - cross_price
    else:
        # Short yes / long no -> buy yes as taker, i.e. hit the implied
        # yes-ask, which is 100 - best resting NO bid price.
        best_no = book.best_no_bid()
        if best_no is None:
            return None
        cross_price = 100 - best_no[0]
        size = abs(net)
        applied = inventory.apply_yes_fill(price=cross_price, size=size, max_inventory=max_inventory)
        side = "yes"
        price = cross_price

    if applied <= 0:
        return None

    return {
        "side": side,
        "price": price,
        "size": applied,
        "net_yes_contracts_before": net,
        "net_yes_contracts_after": inventory.net_yes_contracts,
        "reason": "hard_stop_flatten",
        "ts": time.time(),
    }
