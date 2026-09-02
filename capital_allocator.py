"""
Allocates a fixed pretend starting balance across the currently-watched
markets, weighted by potential profit / risk. Reuses the existing
crowding/volatility-dampened adjusted_score (see
incentive_screener._adjusted_score) as that profit/risk ratio rather
than inventing a second metric -- it's already reward density (profit
potential) divided by real crowding and recent trade-price volatility
(risk of a diminished or choppy fill), which is exactly "profit / risk".

This governs each market's max_inventory (the existing hard cap on
hypothetical position size -- see adverse_selection_sim.InventoryState),
NOT quote price/size, which quoting_policy.py already handles
independently. Still fully read-only/simulation: this only changes how
big a hypothetical position is ALLOWED to get before quotes get pulled/
flattened -- exactly what the single flat config.MAX_SIMULATED_INVENTORY
constant this replaces used to do, just split unevenly by opportunity
instead of split evenly. Same $1-per-contract worst-case approximation
documented on that constant applies here too.
"""

from __future__ import annotations

import logging

from incentive_screener import ScreenedProgram

logger = logging.getLogger(__name__)


def _profit_risk_weight(program: ScreenedProgram) -> float:
    """
    adjusted_score IS a profit/risk ratio (reward density dampened by
    real crowding and recent volatility) -- see incentive_screener.py.
    Falls back to the plain reward_density_score when adjusted_score
    wasn't computed for this candidate (e.g. the manual-fallback watch
    list, which skips the crowding-aware live check -- see
    screen_incentive_programs), same fallback screen_incentive_programs
    itself uses when re-sorting its crowding-aware pool.
    """
    if program.adjusted_score is not None:
        return program.adjusted_score
    return program.reward_density_score


def _waterfill_fractions(weights: dict[str, float], min_fraction: float, max_fraction: float) -> dict[str, float]:
    """
    Splits a 1.0 budget across `weights` proportionally, but with every
    ticker HARD-bounded to [min_fraction, max_fraction] -- unlike a
    single clip-then-rescale pass (which can push a value back outside
    the bounds it just clipped to, since rescaling to sum-to-1.0 touches
    every entry, including the ones already at a bound), this locks each
    out-of-bounds entry at its bound and re-splits the REMAINING budget
    across only the still-unlocked entries, repeating until nothing new
    violates a bound. Standard bounded-proportional ("waterfilling")
    allocation; cheap since watch lists here are small.

    Falls back to an equal split if the bounds are jointly infeasible
    (e.g. min_fraction * len(weights) > 1.0) -- logged, not silently
    wrong, since no proportional split can satisfy such bounds anyway.
    """
    if min_fraction * len(weights) > 1.0 + 1e-9 or max_fraction < min_fraction:
        logger.warning(
            "Infeasible allocation bounds (min_fraction=%.3f, max_fraction=%.3f, %d markets) -- "
            "falling back to an equal split",
            min_fraction, max_fraction, len(weights),
        )
        return {t: 1.0 / len(weights) for t in weights}

    locked: dict[str, float] = {}
    active = dict(weights)

    while active:
        remaining_budget = 1.0 - sum(locked.values())
        total_active_weight = sum(active.values())
        proposed = {t: remaining_budget * (w / total_active_weight) for t, w in active.items()}

        over = {t: f for t, f in proposed.items() if f > max_fraction + 1e-12}
        under = {t: f for t, f in proposed.items() if f < min_fraction - 1e-12}

        if not over and not under:
            locked.update(proposed)
            break

        # Lock only the single most extreme violator this pass, then
        # recompute the remaining budget/weights from scratch -- fixing
        # one violation can resolve others (e.g. capping a dominant
        # ticker can free up enough budget that a previously-under
        # ticker's natural share clears the floor on its own without
        # being forced there). Overs are resolved before unders: freeing
        # budget from a capped dominant ticker is what typically relaxes
        # an under violation in the first place.
        if over:
            worst = max(over, key=over.get)
            locked[worst] = max_fraction
        else:
            worst = min(under, key=under.get)
            locked[worst] = min_fraction
        del active[worst]

    return locked


def allocate_capital(
    total_capital: float,
    programs: list[ScreenedProgram],
    min_fraction: float,
    max_fraction: float,
) -> dict[str, int]:
    """
    Splits total_capital across `programs`, weighted by each one's
    profit/risk ratio (see _profit_risk_weight), hard-bounded to
    [min_fraction, max_fraction] of the total per market (see
    _waterfill_fractions).

    Returns {ticker: max_inventory} (dollars rounded to whole contracts,
    the same $1-per-contract worst-case proxy as the flat constant this
    replaces). Returns {} if `programs` is empty. A program with a
    non-positive weight is floored to a tiny positive epsilon rather than
    excluded -- every currently-watched market still gets at least
    min_fraction of the capital, consistent with "always watch/allocate
    to something" from the autonomous-selection design (see
    incentive_screener.select_watchlist).
    """
    if not programs:
        return {}

    epsilon = 1e-9
    weights = {p.market_ticker: max(_profit_risk_weight(p), epsilon) for p in programs}
    final_fractions = _waterfill_fractions(weights, min_fraction, max_fraction)

    allocations = {t: int(round(total_capital * f)) for t, f in final_fractions.items()}
    logger.info("Capital allocation ($%.0f total, profit/risk-weighted): %s", total_capital, allocations)
    return allocations
