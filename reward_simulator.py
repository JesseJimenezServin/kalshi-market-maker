"""
Implements Kalshi's published Liquidity Incentive Program scoring mechanics
against a live local order book, so we can answer: "if I rested N contracts
at price P on side S right now, what would my snapshot score be?"

Mechanics (from Kalshi's Help Center article on the Liquidity Incentive
Program):

  1. Reference Price: walking down from the best bid, it's the first price
     level at which cumulative resting size reaches 1/5 of the Target Size.
  2. A side only qualifies at all if cumulative resting size on that side
     reaches the full Target Size. If it doesn't, NO orders on that side
     score for this snapshot.
  3. For each qualifying resting order:
       raw_score = order_size * distance_multiplier
     where distance_multiplier = 1.0 if price is at-or-better than the
     Reference Price, else discount_factor ** (ticks_below_reference).
  4. Each order's normalized score = raw_score / sum(raw_score of all
     qualifying orders on that side).
  5. Snapshot score (for one participant) = their yes-side share +
     their no-side share (max possible = 2.0, shared across everyone).

We only see AGGREGATE size at each price level from the public book (not
individual participants' orders), so to estimate "my" share we model our
hypothetical order as being added on top of the existing aggregate size at
our chosen price level, and compute what fraction of the total qualifying
score it would represent.
"""

from __future__ import annotations

from dataclasses import dataclass

from orderbook import MarketBook


@dataclass
class SnapshotScoreResult:
    qualifies: bool
    reference_price: int | None
    my_raw_score: float
    total_raw_score: float
    my_share: float  # my_raw_score / total_raw_score, 0 if not qualifying


def compute_reference_price(levels_desc: list[tuple[int, int]], target_size: float) -> int | None:
    """
    levels_desc: [(price_cents, size), ...] sorted best (highest) price first.
    Returns the price level at which cumulative size first reaches
    target_size / 5, or None if the side never reaches that depth.
    """
    threshold = target_size / 5.0
    cumulative = 0
    for price, size in levels_desc:
        cumulative += size
        if cumulative >= threshold:
            return price
    return None


def side_reaches_target(levels_desc: list[tuple[int, int]], target_size: float) -> bool:
    return sum(size for _, size in levels_desc) >= target_size


def _distance_multiplier(price: int, reference_price: int, discount_factor: float, tick_size: int = 1) -> float:
    if price >= reference_price:
        return 1.0
    ticks_below = (reference_price - price) / tick_size
    return discount_factor ** ticks_below


def score_hypothetical_order(
    levels_desc: list[tuple[int, int]],
    target_size: float,
    discount_factor: float,
    my_price: int,
    my_size: int,
) -> SnapshotScoreResult:
    """
    Computes what my snapshot score share WOULD be on one side of the book
    (yes or no) if I added `my_size` contracts at `my_price`, on top of the
    existing aggregate `levels_desc`.
    """
    # Merge my hypothetical size into the existing levels.
    merged: dict[int, int] = dict(levels_desc)
    merged[my_price] = merged.get(my_price, 0) + my_size
    merged_desc = sorted(merged.items(), key=lambda kv: -kv[0])

    if not side_reaches_target(merged_desc, target_size):
        return SnapshotScoreResult(
            qualifies=False, reference_price=None, my_raw_score=0.0, total_raw_score=0.0, my_share=0.0
        )

    reference_price = compute_reference_price(merged_desc, target_size)
    if reference_price is None:
        return SnapshotScoreResult(
            qualifies=False, reference_price=None, my_raw_score=0.0, total_raw_score=0.0, my_share=0.0
        )

    total_raw = 0.0
    my_raw = 0.0
    for price, size in merged_desc:
        mult = _distance_multiplier(price, reference_price, discount_factor)
        raw = size * mult
        total_raw += raw
        if price == my_price:
            # CORRECTED 2026-09-02 (comment only, this line's math was
            # never wrong): only OUR OWN contribution (my_size) goes into
            # the numerator here -- any pre-existing real resting size at
            # this same price is excluded from my_raw and only counted in
            # total_raw (the denominator, via `raw` above), so this is
            # NOT double-counting. What this genuinely can't model is
            # competitive RESPONSE -- other real participants might change
            # their own behavior once we're resting here too, which no
            # snapshot of the public book can tell us. True validation of
            # that needs Phase 1 real fills, not fixable in pure simulation.
            my_raw += my_size * mult

    if total_raw <= 0:
        return SnapshotScoreResult(
            qualifies=False, reference_price=reference_price, my_raw_score=0.0, total_raw_score=0.0, my_share=0.0
        )

    return SnapshotScoreResult(
        qualifies=True,
        reference_price=reference_price,
        my_raw_score=my_raw,
        total_raw_score=total_raw,
        my_share=my_raw / total_raw,
    )


def score_both_sides(
    book: MarketBook,
    target_size: float,
    discount_factor: float,
    my_yes_price: int,
    my_yes_size: int,
    my_no_price: int,
    my_no_size: int,
) -> tuple[SnapshotScoreResult, SnapshotScoreResult]:
    """Returns (yes_side_result, no_side_result). Combined snapshot score
    for this participant = yes_result.my_share + no_result.my_share."""
    yes_result = score_hypothetical_order(
        book.yes_side_sorted_desc(), target_size, discount_factor, my_yes_price, my_yes_size
    )
    no_result = score_hypothetical_order(
        book.no_side_sorted_desc(), target_size, discount_factor, my_no_price, my_no_size
    )
    return yes_result, no_result
