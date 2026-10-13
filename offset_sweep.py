"""
THE GATING EXPERIMENT (see BLUEPRINT.md section 2).

Question: as we rest further behind the Reference Price, how fast does our
reward score fall, and how fast does our fill rate fall?

Kalshi's reward formula gives an order `d` ticks below the Reference Price a
multiplier of discount_factor ** d, so with discount_factor ~0.9 each tick
back costs ~10% of score. Resting further back also puts us behind more
queue, so fill probability should fall faster than that. If it does, the
strategy becomes profitable by BACKING OFF -- fills are a pure cost under
the rebate thesis (AUTOPSY.md section 8). If it does not, passive rebate
harvesting is not viable at any offset and the project should stop.

TWO HALVES, DIFFERENT DATA REQUIREMENTS
---------------------------------------
  score half: computable from orderbook_snapshots alone, which we have
              2.8M of. Runs today.
  fill half:  needs the raw trade tape (price, count, taker_side), which was
              NOT persisted before 2026-10-13. BLUEPRINT.md originally
              claimed the whole experiment was answerable from existing
              data; that was wrong. simulated_fills records OUR quote price,
              not the trade price, so it cannot tell us whether a deeper
              quote would also have been touched. The `trades` table now
              exists and this script reports the fill half for whatever
              period it covers.

Usage:
    python3 offset_sweep.py --max-offset 5 --sample 20
    python3 offset_sweep.py --since 2026-10-13      # tape-era only
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import defaultdict

import config
from adverse_selection_sim import HypotheticalQuote, InventoryState, process_trade_for_inventory
from orderbook import MarketBook
from reward_simulator import compute_reference_price, score_hypothetical_order


def _book_from_row(ticker: str, yes_levels: str | None, no_levels: str | None) -> MarketBook | None:
    """Rebuild a MarketBook from a logged snapshot."""
    if not yes_levels or not no_levels:
        return None
    try:
        yes = [(int(p), float(sz)) for p, sz in json.loads(yes_levels)]
        no = [(int(p), float(sz)) for p, sz in json.loads(no_levels)]
    except (TypeError, ValueError):
        return None
    if not yes or not no:
        return None
    book = MarketBook(ticker=ticker)
    book.apply_snapshot(sid=0, seq=0, yes=yes, no=no)
    return book


def _program_params(conn: sqlite3.Connection) -> dict[str, tuple[float, float]]:
    """ticker -> (target_size, discount_factor). Latest row per ticker."""
    out = {}
    for r in conn.execute(
        """
        SELECT ticker, target_size, discount_factor_bps FROM (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY ts DESC) AS rn
            FROM incentive_program_snapshots
        ) WHERE rn = 1
        """
    ):
        if r["target_size"]:
            out[r["ticker"]] = (float(r["target_size"]), (r["discount_factor_bps"] or 0) / 10000.0)
    return out


def score_curve(conn, max_offset: int, sample: int, since: float, quote_size: int):
    """
    Mean snapshot score at each offset behind the Reference Price.

    Offset 0 reproduces what the live strategy did, so it doubles as a
    sanity check against the recorded combined_score.
    """
    params = _program_params(conn)
    tot = defaultdict(float)
    n = defaultdict(int)
    qualified = defaultdict(int)
    seen = 0

    rows = conn.execute(
        "SELECT ticker, ts, yes_levels, no_levels FROM orderbook_snapshots "
        "WHERE ts > ? ORDER BY ts",
        (since,),
    )
    for i, r in enumerate(rows):
        if i % sample:
            continue
        if r["ticker"] in config.CONTAMINATED_TICKERS:
            continue
        pp = params.get(r["ticker"])
        if pp is None:
            continue
        target_size, discount = pp
        book = _book_from_row(r["ticker"], r["yes_levels"], r["no_levels"])
        if book is None:
            continue
        seen += 1

        yes_desc = book.yes_side_sorted_desc()
        no_desc = book.no_side_sorted_desc()
        yes_ref = compute_reference_price(yes_desc, target_size)
        no_ref = compute_reference_price(no_desc, target_size)
        if yes_ref is None or no_ref is None:
            continue

        for d in range(max_offset + 1):
            # Resting further BEHIND the reference price means a worse price
            # for us: lower yes bid, lower no bid.
            y = max(1, yes_ref - d)
            no_p = max(1, no_ref - d)
            ys = score_hypothetical_order(yes_desc, target_size, discount, y, quote_size)
            ns = score_hypothetical_order(no_desc, target_size, discount, no_p, quote_size)
            combined = (ys.my_share if ys.qualifies else 0.0) + (ns.my_share if ns.qualifies else 0.0)
            tot[d] += combined
            n[d] += 1
            if ys.qualifies and ns.qualifies:
                qualified[d] += 1
    return seen, {d: (tot[d] / n[d] if n[d] else 0.0, 100 * qualified[d] / n[d] if n[d] else 0.0)
                  for d in range(max_offset + 1)}


def fill_curve(conn, max_offset: int, since: float, quote_size: int, max_inventory: int):
    """
    Replay the real trade tape against a quote held at each offset.

    Exact rather than modelled: process_trade_for_inventory is the same
    function the live strategy uses, fed the same fields.
    """
    params = _program_params(conn)
    have, = conn.execute("SELECT COUNT(*) FROM trades WHERE ts > ?", (since,)).fetchone()
    if not have:
        return 0, {}, 0.0

    span = conn.execute("SELECT MIN(ts) a, MAX(ts) b FROM trades WHERE ts > ?", (since,)).fetchone()
    hours = max((span["b"] - span["a"]) / 3600.0, 1e-9)

    results = {}
    for d in range(max_offset + 1):
        inv_by_ticker: dict[str, InventoryState] = {}
        filled = 0
        peak = 0
        for r in conn.execute(
            "SELECT ticker, ts, trade_yes_price, trade_count, taker_side FROM trades "
            "WHERE ts > ? ORDER BY ts",
            (since,),
        ):
            tk = r["ticker"]
            if tk in config.CONTAMINATED_TICKERS:
                continue
            pp = params.get(tk)
            if pp is None:
                continue
            target_size, _ = pp
            ob = conn.execute(
                "SELECT yes_levels, no_levels FROM orderbook_snapshots "
                "WHERE ticker=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                (tk, r["ts"]),
            ).fetchone()
            if ob is None:
                continue
            book = _book_from_row(tk, ob["yes_levels"], ob["no_levels"])
            if book is None:
                continue
            yes_ref = compute_reference_price(book.yes_side_sorted_desc(), target_size)
            no_ref = compute_reference_price(book.no_side_sorted_desc(), target_size)
            if yes_ref is None or no_ref is None:
                continue
            quote = HypotheticalQuote(
                yes_price=max(1, yes_ref - d), yes_size=quote_size,
                no_price=max(1, no_ref - d), no_size=quote_size,
            )
            inv = inv_by_ticker.setdefault(tk, InventoryState(ticker=tk))
            before = abs(inv.net_yes_contracts)
            # share of the touched level, same estimator the live path uses
            if r["taker_side"] == "no":
                resting = book.yes_levels.get(r["trade_yes_price"], 0.0)
                mine = quote.yes_size
            else:
                resting = book.no_levels.get(100 - r["trade_yes_price"], 0.0)
                mine = quote.no_size
            share = mine / (resting + mine) if (resting + mine) > 0 else 0.0
            process_trade_for_inventory(
                inv, quote, r["trade_yes_price"], r["trade_count"],
                r["taker_side"], share, max_inventory,
            )
            after = abs(inv.net_yes_contracts)
            filled += abs(after - before)
            peak = max(peak, after)
        pnl = sum(i.realized_pnl_cents for i in inv_by_ticker.values()) / 100.0
        results[d] = (filled, filled / hours, peak, pnl)
    return have, results, hours


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-offset", type=int, default=5)
    ap.add_argument("--sample", type=int, default=20, help="use every Nth book snapshot")
    ap.add_argument("--since", type=float, default=0.0, help="unix ts floor")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    qs = config.DEFAULT_SIMULATED_QUOTE_SIZE
    mi = config.MAX_SIMULATED_INVENTORY

    print(f"quote size {qs}/side, max inventory {mi}, sampling every {args.sample}th snapshot")
    print()
    print("=== SCORE vs OFFSET (from order books; runs on all historical data) ===")
    seen, curve = score_curve(conn, args.max_offset, args.sample, args.since, qs)
    print(f"  {seen} usable snapshots")
    print(f"  {'offset':>7} {'mean score':>11} {'vs d=0':>8} {'both qualify':>13}")
    base = curve.get(0, (0.0, 0.0))[0]
    for d in sorted(curve):
        sc, q = curve[d]
        rel = f"{100*sc/base:6.1f}%" if base else "    n/a"
        print(f"  {d:>7} {sc:11.5f} {rel:>8} {q:12.1f}%")

    print()
    print("=== FILL RATE vs OFFSET (needs the trade tape) ===")
    have, fills, hours = fill_curve(conn, args.max_offset, args.since, qs, mi)
    if not have:
        print("  NO TRADE TAPE YET for this window.")
        print("  The tape started being recorded 2026-10-13; re-run once it has")
        print("  accumulated. Until then the fill half of the gate is UNANSWERED,")
        print("  and the score half alone cannot decide it -- backing off is only")
        print("  worth it if fills fall FASTER than score.")
        return 0
    print(f"  {have} trades over {hours:.1f}h")
    print(f"  {'offset':>7} {'filled ct':>10} {'fills/hr':>9} {'peak inv':>9} {'P&L $':>9} {'score/fill':>11}")
    for d in sorted(fills):
        ct, rate, peak, pnl = fills[d]
        sc = curve.get(d, (0.0, 0.0))[0]
        ratio = f"{sc/rate:11.6f}" if rate else "        inf"
        print(f"  {d:>7} {ct:>10} {rate:9.1f} {peak:>9} {pnl:9.2f} {ratio}")
    print()
    print("  DECISION RULE: pick the offset maximising score per unit of fill")
    print("  rate, then check its absolute reward exceeds its absolute fill")
    print("  cost. If no offset does, stop (BLUEPRINT.md section 6, Phase 0.5).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
