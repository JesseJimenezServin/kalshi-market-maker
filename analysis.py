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
    "compound into a period payout)."
)


@dataclass
class TickerAnalysis:
    ticker: str
    snapshot_count: int
    avg_combined_score: float
    non_excluded_fraction: float
    period_reward: float | None
    days_remaining: float | None
    estimated_reward_usd: float | None
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
        "SELECT ts, period_reward, days_remaining "
        "FROM incentive_program_snapshots WHERE ticker = ? ORDER BY ts",
        (ticker,),
    ).fetchall()

    snapshot_count = len(scores)
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

    # Representative period_reward/days_remaining: the program terms
    # as-of the most recent snapshot we have (falls back to the latest
    # known program snapshot overall if this ticker has no scores yet).
    period_reward = None
    days_remaining = None
    if scores and programs:
        latest_match = _asof_program_row(scores[-1]["ts"], programs)
        if latest_match is not None:
            period_reward = latest_match["period_reward"]
            days_remaining = latest_match["days_remaining"]
    elif programs:
        period_reward = programs[-1]["period_reward"]
        days_remaining = programs[-1]["days_remaining"]

    estimated_reward_usd = None
    if period_reward is not None:
        estimated_reward_usd = avg_combined_score * period_reward * non_excluded_fraction

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
        estimated_reward_usd=estimated_reward_usd,
        realized_pnl_usd=(inv["realized_pnl_cents"] / 100.0) if inv else 0.0,
        unrealized_pnl_usd=(inv["unrealized_pnl_cents"] / 100.0) if inv else 0.0,
        net_yes_contracts=(inv["net_yes_contracts"] if inv else 0),
    )


def print_report(analyses: list[TickerAnalysis]) -> None:
    header = (
        f"{'Ticker':<32} {'Snaps':>7} {'AvgScore':>9} {'Clean%':>7} "
        f"{'PeriodPool':>11} {'EstReward':>10} {'Realized':>9} {'Unreal':>9} {'NetPos':>7}"
    )
    print(header)
    print("-" * len(header))
    for a in analyses:
        pool_str = f"${a.period_reward:,.2f}" if a.period_reward is not None else "n/a"
        est_str = f"${a.estimated_reward_usd:,.2f}" if a.estimated_reward_usd is not None else "n/a"
        print(
            f"{a.ticker:<32} {a.snapshot_count:>7} {a.avg_combined_score:>9.4f} "
            f"{a.non_excluded_fraction * 100:>6.1f}% {pool_str:>11} {est_str:>10} "
            f"${a.realized_pnl_usd:>8.2f} ${a.unrealized_pnl_usd:>8.2f} {a.net_yes_contracts:>7}"
        )

    print()
    print(f"EstReward is {ESTIMATED_REWARD_CAVEATS}")
    print()
    print("Net simulated result if EstReward were fully realized (EstReward + Realized + Unrealized P&L):")
    for a in analyses:
        if a.estimated_reward_usd is not None:
            net = a.estimated_reward_usd + a.realized_pnl_usd + a.unrealized_pnl_usd
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
