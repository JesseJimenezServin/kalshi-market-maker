"""
One-time (but idempotent) backfill: resolve positions that were left frozen
at a mid because they settled before settlement accounting existed.

Context. Until 2026-10-04 there was no settlement path at all, so leaving a
settled market froze the position at whatever mid was last observed. A
binary market resolves to 100 or 0, so the true realised P&L was a known
fact being recorded as an estimate. Measured at the time, this overstated
the run's loss by ~$10 on ~$1000 of capital -- three short positions that
resolved in our favour were still marked at their last mid rather than at 0.
main.py now settles automatically via _settle_if_resolved(), but only for
markets that go dead while being watched; anything already abandoned stays
frozen. This fixes those.

Design notes, both deliberate:

  - It reuses InventoryState.settle() rather than reimplementing the
    arithmetic. A backfill that computed settlement even slightly
    differently from the live path would create a permanent inconsistency
    in the same column, which is worse than the bug being fixed.

  - It only ADDS rows. The dashboard takes the latest mark per ticker
    (see server.py's _latest_per_ticker), so a new row supersedes the stale
    one without deleting history -- and the old rows stay as the record of
    what was believed at the time.

Idempotent: a settled position is written with net_yes_contracts = 0, so a
second run sees nothing to do.

Usage:
    python3 backfill_settlements.py --dry-run     # show what would change
    python3 backfill_settlements.py --apply
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

import config
from adverse_selection_sim import InventoryState
from logger import SimulationLogger
from rest_client import KalshiRestClient


def latest_marks(db_path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            """
            SELECT ticker, ts, net_yes_contracts, avg_entry_price, mid_price,
                   unrealized_pnl_cents, realized_pnl_cents, max_inventory
            FROM inventory_marks GROUP BY ticker HAVING ts = max(ts)
            """
        ).fetchall()
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    rest = KalshiRestClient()
    sim_logger = SimulationLogger(config.DB_PATH) if args.apply else None

    total_before = total_after = 0.0
    changed = skipped_open = skipped_flat = skipped_contaminated = 0

    print(f"{'ticker':44} {'net':>5} {'result':>6} {'frozen':>9} {'settled':>9}")
    for r in latest_marks(config.DB_PATH):
        ticker = r["ticker"]
        before = (r["realized_pnl_cents"] + r["unrealized_pnl_cents"]) / 100.0

        if ticker in config.CONTAMINATED_TICKERS:
            # Excluded from all reporting anyway, and their entry prices came
            # from phantom liquidity -- settling them would promote an
            # artefact to a "realised fact".
            skipped_contaminated += 1
            continue
        if r["net_yes_contracts"] == 0:
            skipped_flat += 1
            total_before += before
            total_after += before
            continue

        market = rest.get_market(ticker).get("market", {})
        result = (market.get("result") or "").lower()
        if result not in ("yes", "no"):
            print(f"{ticker[:44]:44} {r['net_yes_contracts']:>5} {'-':>6} "
                  f"${before:8.2f} {'(not resolved)':>9}")
            skipped_open += 1
            total_before += before
            total_after += before
            continue

        # Rebuild the position and settle it with the PRODUCTION code path.
        inv = InventoryState(
            ticker=ticker,
            net_yes_contracts=r["net_yes_contracts"],
            avg_entry_price=r["avg_entry_price"],
            realized_pnl_cents=r["realized_pnl_cents"],
        )
        inv.settle(result_yes=(result == "yes"))
        after = inv.realized_pnl_cents / 100.0
        final_price = 100.0 if result == "yes" else 0.0

        print(f"{ticker[:44]:44} {r['net_yes_contracts']:>5} {result:>6} "
              f"${before:8.2f} ${after:8.2f}")
        total_before += before
        total_after += after
        changed += 1

        if args.apply:
            sim_logger.log_inventory_mark(
                ticker=ticker,
                net_yes_contracts=inv.net_yes_contracts,
                avg_entry_price=inv.avg_entry_price,
                mid_price=final_price,
                unrealized_pnl_cents=0.0,
                realized_pnl_cents=inv.realized_pnl_cents,
                max_inventory=r["max_inventory"],
            )

    print()
    print(f"  settled now         : {changed}")
    print(f"  already flat        : {skipped_flat}")
    print(f"  not resolved yet    : {skipped_open}")
    print(f"  contaminated, skipped: {skipped_contaminated}")
    print(f"  reported P&L  before : ${total_before:.2f}  (balance ${1000 + total_before:.2f})")
    print(f"  reported P&L  after  : ${total_after:.2f}  (balance ${1000 + total_after:.2f})")
    print(f"  correction           : ${total_after - total_before:+.2f}")
    if args.dry_run:
        print("\n  DRY RUN -- nothing written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
