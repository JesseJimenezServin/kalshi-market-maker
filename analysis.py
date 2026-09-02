"""
Read-only analysis of already-collected Phase 0 data in
data/paper_trading.sqlite. Does NOT change any simulation logic and does
NOT track a simulated cash balance -- realized/unrealized P&L is already
tracked correctly in cents by adverse_selection_sim.py / logger.py, so a
separate "balance" would just be redundant, not new information.

What this adds: an ESTIMATED dollar reward per watched ticker, computed
from snapshot_scores joined against incentive_program_snapshots, printed
alongside the real adverse-selection P&L from inventory_marks -- so both
sides of "does reward income beat adverse-selection risk" are visible
together. The reward number is explicitly an optimistic upper-bound
ESTIMATE, not a guaranteed figure -- see ESTIMATED_REWARD_CAVEATS below
for exactly why.

CORRECTED 2026-09-01: the first version of this script multiplied the
observed average score directly against the FULL program-period reward
pool, with no time-scaling at all -- e.g. ~1 hour of data against a
14-day, $50,000 pool produced a ~$6,900 "estimate" for one ticker, wildly
overstating what ~1 hour of observation actually supports. This version
adds `estimated_reward_so_far_usd`, prorated by how much of the ACTUAL
program period we've observed (hours_observed / total_period_hours), as
the primary, honest answer to "what has this earned so far." The old
unprorated figure is kept as `naive_full_period_projection_usd`, clearly
relabeled as a forward-looking projection (if this exact average held for
the ENTIRE remaining period), not something already earned.

Run with: python3 analysis.py
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import config

ESTIMATED_REWARD_CAVEATS = (
    "ESTIMATED, optimistic upper bound -- assumes no double-counting with "
    "other participants resting at our exact quoted price (the same known "
    "simplification documented for reward_simulator.py's my_share estimate "
    "in README.md), and Time Period Score aggregation assumed as a simple "
    "average of snapshot scores over the collected window (unverified "
    "against Kalshi's exact methodology for how per-snapshot scores "
    "compound into a period payout). EstSoFar additionally assumes our "
    "observed average score is representative of the rest of the period "
    "too, just scaled by elapsed time -- not scaled by anything else "
    "(volatility regime changes, other LPs arriving/leaving, etc.)."
)


@dataclass
class TickerAnalysis:
    ticker: str
    snapshot_count: int
    avg_combined_score: float
    non_excluded_fraction: float
    period_reward: float | None
    days_remaining: float | None
    total_period_days: float | None
    hours_observed: float
    estimated_reward_so_far_usd: float | None
    naive_full_period_projection_usd: float | None
    realized_pnl_usd: float
    unrealized_pnl_usd: float
    net_yes_contracts: int


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _asof_program_row(score_ts: float, program_rows: list[sqlite3.Row]) -> sqlite3.Row | None:
    """
    Returns the most recent incentive_program_snapshots row at or before
    score_ts (an "as-of" join, since period_reward/target_size/discount
    factor can change between program periods) -- program_rows must
    already be sorted ascending by ts. None if score_ts predates the
    first observed program snapshot for this ticker.
    """
    result = None
    for row in program_rows:
        if row["ts"] > score_ts:
            break
        result = row
    return result


def analyze_ticker(conn: sqlite3.Connection, ticker: str) -> TickerAnalysis:
    scores = conn.execute(
        "SELECT ts, combined_score, qualifies_yes, qualifies_no "
        "FROM snapshot_scores WHERE ticker = ? ORDER BY ts",
        (ticker,),
    ).fetchall()
    programs = conn.execute(
        "SELECT ts, period_reward, days_remaining, total_period_days "
        "FROM incentive_program_snapshots WHERE ticker = ? ORDER BY ts",
        (ticker,),
    ).fetchall()

    snapshot_count = len(scores)
    hours_observed = 0.0
    if snapshot_count == 0:
        avg_combined_score = 0.0
        non_excluded_fraction = 0.0
    else:
        avg_combined_score = sum(r["combined_score"] or 0.0 for r in scores) / snapshot_count
        # A snapshot is excluded from the "clean" fraction if EITHER side
        # failed to reach Target Size (qualifies_yes or qualifies_no is
        # false) -- per the requested definition, not just partial credit.
        non_excluded = sum(1 for r in scores if r["qualifies_yes"] and r["qualifies_no"])
        non_excluded_fraction = non_excluded / snapshot_count
        hours_observed = (scores[-1]["ts"] - scores[0]["ts"]) / 3600.0

    # Representative period_reward/days_remaining/total_period_days: the
    # program terms as-of the most recent snapshot we have (falls back to
    # the latest known program snapshot overall if this ticker has no
    # scores yet).
    period_reward = None
    days_remaining = None
    total_period_days = None
    if scores and programs:
        latest_match = _asof_program_row(scores[-1]["ts"], programs)
        if latest_match is not None:
            period_reward = latest_match["period_reward"]
            days_remaining = latest_match["days_remaining"]
            total_period_days = latest_match["total_period_days"]
    elif programs:
        period_reward = programs[-1]["period_reward"]
        days_remaining = programs[-1]["days_remaining"]
        total_period_days = programs[-1]["total_period_days"]

    naive_full_period_projection_usd = None
    estimated_reward_so_far_usd = None
    if period_reward is not None:
        naive_full_period_projection_usd = avg_combined_score * period_reward * non_excluded_fraction
        if total_period_days:
            total_period_hours = total_period_days * 24.0
            time_fraction = min(1.0, hours_observed / total_period_hours)
            estimated_reward_so_far_usd = naive_full_period_projection_usd * time_fraction

    inv = conn.execute(
        "SELECT net_yes_contracts, unrealized_pnl_cents, realized_pnl_cents "
        "FROM inventory_marks WHERE ticker = ? ORDER BY ts DESC LIMIT 1",
        (ticker,),
    ).fetchone()

    return TickerAnalysis(
        ticker=ticker,
        snapshot_count=snapshot_count,
        avg_combined_score=avg_combined_score,
        non_excluded_fraction=non_excluded_fraction,
        period_reward=period_reward,
        days_remaining=days_remaining,
        total_period_days=total_period_days,
        hours_observed=hours_observed,
        estimated_reward_so_far_usd=estimated_reward_so_far_usd,
        naive_full_period_projection_usd=naive_full_period_projection_usd,
        realized_pnl_usd=(inv["realized_pnl_cents"] / 100.0) if inv else 0.0,
        unrealized_pnl_usd=(inv["unrealized_pnl_cents"] / 100.0) if inv else 0.0,
        net_yes_contracts=(inv["net_yes_contracts"] if inv else 0),
    )


def print_report(analyses: list[TickerAnalysis]) -> None:
    header = (
        f"{'Ticker':<32} {'HoursObs':>8} {'AvgScore':>9} {'Clean%':>7} "
        f"{'PeriodPool':>11} {'PeriodDays':>10} {'EstSoFar':>10} {'IfFullPeriod':>12} "
        f"{'Realized':>9} {'Unreal':>9} {'NetPos':>7}"
    )
    print(header)
    print("-" * len(header))
    for a in analyses:
        pool_str = f"${a.period_reward:,.2f}" if a.period_reward is not None else "n/a"
        days_str = f"{a.total_period_days:.1f}" if a.total_period_days else "n/a"
        soFar_str = f"${a.estimated_reward_so_far_usd:,.2f}" if a.estimated_reward_so_far_usd is not None else "n/a"
        fullPeriod_str = (
            f"${a.naive_full_period_projection_usd:,.2f}" if a.naive_full_period_projection_usd is not None else "n/a"
        )
        print(
            f"{a.ticker:<32} {a.hours_observed:>8.2f} {a.avg_combined_score:>9.4f} "
            f"{a.non_excluded_fraction * 100:>6.1f}% {pool_str:>11} {days_str:>10} "
            f"{soFar_str:>10} {fullPeriod_str:>12} "
            f"${a.realized_pnl_usd:>8.2f} ${a.unrealized_pnl_usd:>8.2f} {a.net_yes_contracts:>7}"
        )

    print()
    print("EstSoFar = reward fairly attributable to the time observed SO FAR (properly prorated by")
    print("  hours_observed / (PeriodDays * 24) -- this is the honest answer to 'what has this earned so far.'")
    print("IfFullPeriod = what the estimate would be if our observed average score held for the ENTIRE")
    print("  period (NOT something already earned -- a forward-looking projection only).")
    print()
    print(f"Both are {ESTIMATED_REWARD_CAVEATS}")
    print()
    print("Net simulated result so far (EstSoFar + Realized + Unrealized P&L):")
    for a in analyses:
        if a.estimated_reward_so_far_usd is not None:
            net = a.estimated_reward_so_far_usd + a.realized_pnl_usd + a.unrealized_pnl_usd
            print(f"  {a.ticker}: ${net:,.2f}")
        else:
            print(f"  {a.ticker}: n/a (no incentive_program_snapshots data collected yet)")


def main() -> None:
    conn = _connect()
    try:
        rows = conn.execute("SELECT DISTINCT ticker FROM snapshot_scores").fetchall()
        tickers = {r["ticker"] for r in rows} | set(config.WATCHED_MARKET_TICKERS)
        analyses = [analyze_ticker(conn, t) for t in sorted(tickers)]
        print_report(analyses)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
