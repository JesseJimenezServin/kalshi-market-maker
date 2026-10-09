"""
Active quoting policy: replaces the naive touch-joining rule that used to
live in MarketRuntimeState.refresh_quote_from_book with one that reacts to
realized volatility and hypothetical inventory (risk_engine.py), and
proactively manages an open position rather than just holding it until an
emergency hard stop:
  - compute_quote applies a passive take-profit floor to the exit side of
    the quote once inventory exists (see effective_take_profit_margin /
    _apply_take_profit), relaxed as a market's resolution nears.
  - maybe_execute_stop_loss actively flattens at a HIGH risk_level, using
    a contract-count threshold refined for small max_inventory
    allocations, not a pure fraction.
  - maybe_execute_time_based_exit actively flattens once a market is
    within config.CRITICAL_EXIT_MINUTES of its real close/resolution
    time, regardless of risk_level.
Still fully read-only/paper-trading -- compute_quote only returns a
HypotheticalQuote (or None); the two maybe_execute_* functions only
mutate the in-memory InventoryState passed to them. Nothing in this
module ever calls into rest_client.py/ws_client.py or issues any
network request.
"""

from __future__ import annotations

import logging
import time

import config
from adverse_selection_sim import HypotheticalQuote, InventoryState
from orderbook import MarketBook
from reward_simulator import compute_reference_price
from risk_engine import ELEVATED, HIGH

logger = logging.getLogger(__name__)


def _clamp_price(price: int) -> int:
    return max(1, min(99, price))


def effective_take_profit_margin(
    minutes_until_close: float | None,
    full_margin_cents: float,
    window_minutes: float,
    critical_minutes: float,
) -> float:
    """
    full_margin_cents outside the pre-resolution window (or if
    minutes_until_close is unknown, or the market is already past its
    close -- settled/near-settled is safe, not risky, matching the same
    convention used elsewhere, e.g. risk_engine.set_event_proximity).
    Linearly relaxed toward 0 as minutes_until_close falls from
    window_minutes to critical_minutes; exactly 0 at/below
    critical_minutes -- any exit fill is accepted at that point, even at
    a loss. The ACTIVE flatten in maybe_execute_time_based_exit is what
    actually forces the position closed if a passive fill hasn't
    happened by then.
    """
    if (
        minutes_until_close is None
        or minutes_until_close < 0
        or minutes_until_close > window_minutes
    ):
        return full_margin_cents
    if minutes_until_close <= critical_minutes:
        return 0.0
    span = window_minutes - critical_minutes
    if span <= 0:
        return 0.0
    progress = (window_minutes - minutes_until_close) / span
    return full_margin_cents * (1.0 - progress)


def _apply_take_profit(
    yes_price: int,
    no_price: int,
    inventory: InventoryState,
    margin_cents: float,
) -> tuple[int, int]:
    """
    Caps the EXIT side only (NO if long yes, YES if short yes) so it's
    never rested more aggressively than the price implying at least
    margin_cents of profit vs. avg_entry_price. No-ops on a flat
    position (nothing to take profit on) or on the entry side (the
    inventory skew above already handles making that side less
    attractive; take-profit is strictly about the unwind side).
    """
    if inventory.net_yes_contracts > 0:
        # Long yes -> exit side is NO. equiv_yes_sell_price = 100 - no_price
        # must be >= avg_entry_price + margin, i.e. no_price <= that bound.
        target_no_price = round(100 - (inventory.avg_entry_price + margin_cents))
        no_price = min(no_price, target_no_price)
    elif inventory.net_yes_contracts < 0:
        # Short yes -> exit side is YES. yes_price must be
        # <= avg_entry_price - margin.
        target_yes_price = round(inventory.avg_entry_price - margin_cents)
        yes_price = min(yes_price, target_yes_price)
    return yes_price, no_price


def classify_fill_exit_reason(
    prior_net_yes_contracts: int,
    prior_avg_entry_price: float,
    fill_side: str,
    fill_price: int,
    margin_cents: float,
) -> str | None:
    """
    Classifies a passive fill for logging: None if it was flat-to-start
    or on the entry side (moves |net_yes_contracts| further from zero),
    "take_profit_exit" if it reduces inventory at a price meeting the
    full take-profit margin, or "exit" if it reduces inventory below
    that margin (e.g. during the pre-resolution time-decay window, where
    the margin requirement itself is being relaxed toward 0).
    """
    if prior_net_yes_contracts == 0:
        return None

    is_exit_side = (prior_net_yes_contracts > 0 and fill_side == "no") or (
        prior_net_yes_contracts < 0 and fill_side == "yes"
    )
    if not is_exit_side:
        return None

    if prior_net_yes_contracts > 0:
        profit_per_contract = (100 - fill_price) - prior_avg_entry_price
    else:
        profit_per_contract = prior_avg_entry_price - fill_price

    return "take_profit_exit" if profit_per_contract >= margin_cents - 1e-9 else "exit"


def compute_quote(
    book: MarketBook,
    risk_level: str,
    inventory: InventoryState,
    base_size: int,
    max_inventory: int,
    target_size: float,
    minutes_until_close: float | None = None,
) -> HypotheticalQuote | None:
    """
    Base case (risk_level LOW, flat inventory): rest at the live Reference
    Price on each side (falling back to the touch if that side never
    reaches the 1/5-of-target-size threshold), instead of always joining
    the touch exactly.

    Why: reward_simulator's distance_multiplier is 1.0 for any price
    at-or-better than the Reference Price -- resting exactly AT the
    Reference Price (which compute_reference_price guarantees is
    at-or-below the touch) gets the identical full multiplier as resting
    at the touch, but with strictly more distance from the touch for
    free. Confirmed live 2026-09-01: on a thinner book the Reference
    Price can sit several cents below the touch (free safety margin,
    unused by the old touch-joining rule); on a book where the touch
    alone already clears the threshold, Reference Price == touch and
    there's no free margin to give -- this naturally adapts to whichever
    is true on a given market at a given moment, rather than assuming a
    fixed offset that would be free on one market and needlessly
    expensive (via the discount_factor penalty below the Reference
    Price) on another.

    Inventory skew: shift yes_price down / no_price up proportional to
    net_yes_contracts / max_inventory, capped at
    config.MAX_INVENTORY_SKEW_TICKS. A long-yes position skews the quote to
    encourage getting flatter (worse yes bid so we're less likely to buy
    more yes; better/higher no bid so we're more likely to sell yes via a
    no fill) -- and symmetrically for a short-yes/long-no position.

    Risk-level reaction (applied after skew):
      LOW      -> skewed Reference-Price base, unchanged size.
      ELEVATED -> skewed quote widened by config.WIDEN_TICKS_ELEVATED ticks
                  further from that base on each side.
      HIGH     -> quote pulled entirely (returns None). The caller is
                  responsible for separately invoking maybe_execute_stop_loss
                  to decide whether inventory should be actively flattened.

    Take-profit (applied last, after skew/widen): if we hold inventory,
    the exit side is additionally capped so it never rests more
    aggressively than config.TAKE_PROFIT_MARGIN_CENTS of guaranteed
    profit vs. avg_entry_price -- see _apply_take_profit. That margin
    itself shrinks toward 0 as minutes_until_close (if provided --
    see event_calendar.py) falls from config.EXIT_BEFORE_RESOLUTION_MINUTES
    to config.CRITICAL_EXIT_MINUTES, so we stop holding out for a full
    margin we're unlikely to get once resolution is imminent -- see
    effective_take_profit_margin and, for what happens below the
    critical threshold, maybe_execute_time_based_exit.
    """
    if risk_level == HIGH:
        return None

    best_yes = book.best_yes_bid()
    best_no = book.best_no_bid()
    if best_yes is None or best_no is None:
        return None

    yes_ref = compute_reference_price(book.yes_side_sorted_desc(), target_size)
    no_ref = compute_reference_price(book.no_side_sorted_desc(), target_size)
    yes_price = yes_ref if yes_ref is not None else best_yes[0]
    no_price = no_ref if no_ref is not None else best_no[0]

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

    margin = effective_take_profit_margin(
        minutes_until_close,
        config.TAKE_PROFIT_MARGIN_CENTS,
        config.EXIT_BEFORE_RESOLUTION_MINUTES,
        config.CRITICAL_EXIT_MINUTES,
    )
    yes_price, no_price = _apply_take_profit(yes_price, no_price, inventory, margin)

    yes_price = _clamp_price(yes_price)
    no_price = _clamp_price(no_price)

    # Size each side by the inventory room ACTUALLY left on it, pulling the
    # side entirely when there is none.
    #
    # This closes a three-way inconsistency. apply_yes_fill/apply_no_fill
    # clamp a fill to max_inventory and justify discarding the rest as "we'd
    # have pulled the quote" -- but nothing here ever pulled it, and
    # reward_simulator scores whatever size is resting. So at the inventory
    # cap the simulation simultaneously assumed the quote was gone (for
    # fills) and still there (for reward score), collecting incentive credit
    # for liquidity it had already decided it was not providing.
    #
    # Sizing by room also makes the approach to the cap gradual rather than
    # a cliff, which is what a real maker would do.
    yes_room = max(0, max_inventory - inventory.net_yes_contracts)
    no_room = max(0, max_inventory + inventory.net_yes_contracts)
    yes_size = min(base_size, yes_room)
    no_size = min(base_size, no_room)

    if yes_size <= 0 and no_size <= 0:
        # Both sides capped: we are not quoting at all, so there is no quote
        # to score or fill. Returning None keeps that explicit rather than
        # emitting a zero-size quote the rest of the pipeline has to special-case.
        return None

    return HypotheticalQuote(
        yes_price=yes_price,
        yes_size=yes_size,
        no_price=no_price,
        no_size=no_size,
    )


def _walk_book(levels_desc: list[tuple[int, float]], size: int) -> tuple[float, int] | None:
    """
    Average price and actually-fillable size for taking `size` contracts
    against `levels_desc` (best price first).

    The old code took the whole position at the BEST price regardless of how
    much was resting there -- a 404-contract exit assumed the top of book
    absorbed all 404. Walking the levels gives the real average, which is
    what the slippage bound in flatten_position is then checked against.
    Returns None if the book is empty.
    """
    filled = 0
    cost = 0.0
    for price, avail in levels_desc:
        take = min(size - filled, int(avail))
        if take <= 0:
            continue
        cost += price * take
        filled += take
        if filled >= size:
            break
    if filled <= 0:
        return None
    return cost / filled, filled


def flatten_position(
    book: MarketBook,
    inventory: InventoryState,
    max_inventory: int,
    reason: str,
) -> dict | None:
    """
    Simulates an ACTIVE taker flatten (crossing the spread to close our
    hypothetical position), unconditionally -- the caller has already
    decided flattening is warranted (HIGH-risk hard stop, or leaving this
    market entirely via the switcher). This mutates `inventory` directly
    via apply_yes_fill/apply_no_fill and is a DISTINCT simulated event
    from passive fills recorded by process_trade_for_inventory.

    Returns a dict of fill details for logging, or None if there's
    nothing to flatten (already flat, or the book is missing the side
    needed to cross).
    """
    net = inventory.net_yes_contracts
    if net == 0:
        return None

    best_yes = book.best_yes_bid()
    best_no = book.best_no_bid()
    if best_yes is None or best_no is None:
        return None
    mid = (best_yes[0] + (100 - best_no[0])) / 2.0

    if net > 0:
        # Long yes -> sell yes as taker, walking DOWN the resting YES bids.
        walk = _walk_book(book.yes_side_sorted_desc(), net)
        if walk is None:
            return None
        avg_yes_price, fillable = walk
    else:
        # Short yes / long no -> buy yes as taker, walking the NO bids,
        # whose yes-equivalent price is 100 - no_price.
        walk = _walk_book(book.no_side_sorted_desc(), abs(net))
        if walk is None:
            return None
        avg_no_price, fillable = walk
        avg_yes_price = 100 - avg_no_price

    # Price bound. Crossing used to happen at whatever was resting, which on
    # a thin book meant dumping hundreds of contracts into a 1-cent bid --
    # see config.MAX_FLATTEN_SLIPPAGE_CENTS for the measured damage.
    slippage = abs(avg_yes_price - mid)
    if slippage > config.MAX_FLATTEN_SLIPPAGE_CENTS:
        logger.warning(
            "%s: REFUSING to flatten %d contracts -- crossing costs %.1fc of slippage "
            "(avg exit %.1fc vs mid %.1fc, limit %.1fc). Holding instead; the quote is "
            "already pulled at HIGH so the position cannot grow, and settlement will "
            "resolve it at its true value.",
            inventory.ticker, abs(net), slippage, avg_yes_price, mid,
            config.MAX_FLATTEN_SLIPPAGE_CENTS,
        )
        return None

    if net > 0:
        applied = inventory.apply_no_fill(
            no_price=round(100 - avg_yes_price), size=fillable, max_inventory=max_inventory
        )
        side = "no"  # the fill is booked as a NO buy (equiv. selling YES)
        price = round(100 - avg_yes_price)
    else:
        applied = inventory.apply_yes_fill(
            price=round(avg_yes_price), size=fillable, max_inventory=max_inventory
        )
        side = "yes"
        price = round(avg_yes_price)

    if applied <= 0:
        return None

    return {
        "side": side,
        "price": price,
        "size": applied,
        "net_yes_contracts_before": net,
        "net_yes_contracts_after": inventory.net_yes_contracts,
        "reason": reason,
        "ts": time.time(),
    }


def maybe_execute_stop_loss(
    book: MarketBook,
    inventory: InventoryState,
    risk_level: str,
    max_inventory: int,
    hard_stop_fraction: float,
    order_size: int = 0,
    min_buffer: int = 0,
) -> dict | None:
    """
    Thin gate in front of flatten_position: only flattens when risk_level
    is HIGH and our inventory is at/above a contract threshold of
    max(hard_stop_fraction * max_inventory, order_size + min_buffer).

    The second term exists so a market with a small max_inventory
    allocation doesn't hit "hard_stop_fraction of cap" after a single
    ~order_size-contract fill -- e.g. a $150 cap against a 100-contract
    quote size would otherwise trigger the hard stop at just 120
    contracts (80%), before the position ever had a chance to work.
    order_size/min_buffer both default to 0, which reduces exactly to
    the original pure-fraction behavior.
    """
    if risk_level != HIGH:
        return None
    if max_inventory <= 0:
        return None

    net = inventory.net_yes_contracts
    if net == 0:
        return None
    threshold = max(hard_stop_fraction * max_inventory, order_size + min_buffer)
    if abs(net) < threshold:
        return None

    return flatten_position(book, inventory, max_inventory, reason="hard_stop_flatten")


def maybe_execute_time_based_exit(
    book: MarketBook,
    inventory: InventoryState,
    minutes_until_close: float | None,
    max_inventory: int,
    critical_minutes: float,
) -> dict | None:
    """
    Forces an active flatten (crossing the spread, reusing
    flatten_position) once minutes_until_close falls to/below
    critical_minutes -- REGARDLESS of current risk_level, unlike
    maybe_execute_stop_loss -- so we never carry inventory into a
    market's binary resolution. No-ops if minutes_until_close is
    unknown (event_calendar.py hasn't successfully fetched this
    market's close_time yet), still above the threshold, or negative
    (already past close -- settled/near-settled, not "critical").
    """
    if minutes_until_close is None:
        return None
    if minutes_until_close < 0 or minutes_until_close > critical_minutes:
        return None

    return flatten_position(book, inventory, max_inventory, reason="pre_resolution_flatten")
