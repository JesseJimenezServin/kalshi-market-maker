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
    python3 offset_sweep.py --since 1791849600      # tape era (2026-10-13) only
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
from incentive_screener import program_reward_per_day
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


def _program_params(conn: sqlite3.Connection) -> dict[str, tuple[float, float, float | None]]:
    """ticker -> (target_size, discount_factor, pool dollars/second). Latest row per ticker."""
    out = {}
    for r in conn.execute(
        """
        SELECT ticker, target_size, discount_factor_bps, period_reward, start_date, end_date FROM (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY ts DESC) AS rn
            FROM incentive_program_snapshots
        ) WHERE rn = 1
        """
    ):
        if r["target_size"]:
            per_day = program_reward_per_day(r["period_reward"], r["start_date"], r["end_date"])
            out[r["ticker"]] = (
                float(r["target_size"]),
                (r["discount_factor_bps"] or 0) / 10000.0,
                None if per_day is None else per_day / 86400.0,
            )
    return out


# A sampled snapshot stands for the time since the previous sampled snapshot
# of the same ticker, capped so a collection gap (or the bot not watching the
# market) is never credited as quoting time.
MAX_CREDITED_GAP_S = 120.0


def score_curve(conn, max_offset: int, sample: int, since: float, quote_size: int):
    """
    Mean snapshot score, and estimated reward dollars, at each offset behind
    the Reference Price.

    Offset 0 reproduces what the live strategy did, so it doubles as a
    sanity check against the recorded combined_score.

    Dollars use the dashboard's convention (2026-10-05): Kalshi pools 2.0 of
    score per snapshot, so our pool fraction is combined / 2.0, times the
    program's dollars-per-second, times the time the snapshot stands for.
    UPPER BOUND: nobody reacts to our resting size (BLUEPRINT.md section 5).
    It also scores the full quote size even when the inventory cap would
    have pulled a side, which flatters every offset roughly equally.
    """
    params = _program_params(conn)
    tot = defaultdict(float)
    dollars = defaultdict(float)
    n = defaultdict(int)
    qualified = defaultdict(int)
    last_ts: dict[str, float] = {}
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
        target_size, discount, per_sec = pp
        book = _book_from_row(r["ticker"], r["yes_levels"], r["no_levels"])
        if book is None:
            continue
        seen += 1
        prev = last_ts.get(r["ticker"])
        last_ts[r["ticker"]] = r["ts"]
        dt = 0.0 if prev is None else min(r["ts"] - prev, MAX_CREDITED_GAP_S)

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
            if per_sec is not None:
                dollars[d] += combined / 2.0 * per_sec * dt
            if ys.qualifies and ns.qualifies:
                qualified[d] += 1
    return seen, {d: (tot[d] / n[d] if n[d] else 0.0,
                      100 * qualified[d] / n[d] if n[d] else 0.0,
                      dollars[d])
                  for d in range(max_offset + 1)}


def _mid(book: MarketBook) -> float | None:
    by, bn = book.best_yes_bid(), book.best_no_bid()
    if by is None or bn is None:
        return None
    return (by[0] + (100 - bn[0])) / 2.0


def _settlement_results(tickers) -> dict[str, bool]:
    """ticker -> result_yes, for markets Kalshi has resolved. Best effort:
    any failure just leaves that ticker to be marked at its last mid."""
    try:
        from rest_client import KalshiRestClient
        rest = KalshiRestClient()
    except Exception as e:  # no credentials, no network
        print(f"  (settlement lookup unavailable: {e}; marking open positions at last mid)")
        return {}
    out = {}
    for tk in tickers:
        try:
            result = (rest.get_market(tk).get("market", {}).get("result") or "").lower()
        except Exception:
            continue
        if result in ("yes", "no"):
            out[tk] = result == "yes"
    return out


def fill_curve(conn, max_offset: int, since: float, quote_size: int, max_inventory: int,
               settle_via_api: bool = True):
    """
    Replay the real trade tape against a quote held at each offset.

    Exact rather than modelled: process_trade_for_inventory is the same
    function the live strategy uses, fed the same fields. Each trade's book
    is loaded once and reused for every offset.

    The quote is held continuously and NEVER exits by crossing the spread,
    so this measures pure passive fill cost. Positions still open at the
    end are closed at settlement when Kalshi has resolved the market, and
    marked at the last observed mid otherwise. Without that, a strategy
    that accumulates losing inventory looks better than one that closes it.
    """
    params = _program_params(conn)
    have, = conn.execute("SELECT COUNT(*) FROM trades WHERE ts > ?", (since,)).fetchone()
    if not have:
        return 0, {}, 0.0

    span = conn.execute("SELECT MIN(ts) a, MAX(ts) b FROM trades WHERE ts > ?", (since,)).fetchone()
    hours = max((span["b"] - span["a"]) / 3600.0, 1e-9)

    inv = {d: {} for d in range(max_offset + 1)}
    filled = defaultdict(int)
    peak = defaultdict(int)
    last_mid: dict[str, float] = {}

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
        target_size = pp[0]
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
        mid = _mid(book)
        if mid is not None:
            last_mid[tk] = mid
        yes_ref = compute_reference_price(book.yes_side_sorted_desc(), target_size)
        no_ref = compute_reference_price(book.no_side_sorted_desc(), target_size)
        if yes_ref is None or no_ref is None:
            continue
        # share of the touched level, same estimator the live path uses
        if r["taker_side"] == "no":
            resting = book.yes_levels.get(r["trade_yes_price"], 0.0)
        else:
            resting = book.no_levels.get(100 - r["trade_yes_price"], 0.0)
        share = quote_size / (resting + quote_size)

        for d in inv:
            quote = HypotheticalQuote(
                yes_price=max(1, yes_ref - d), yes_size=quote_size,
                no_price=max(1, no_ref - d), no_size=quote_size,
            )
            state = inv[d].setdefault(tk, InventoryState(ticker=tk))
            n_events = len(state.fill_events)
            process_trade_for_inventory(
                state, quote, r["trade_yes_price"], r["trade_count"],
                r["taker_side"], share, max_inventory,
            )
            # Count contracts actually filled, not the change in |position|:
            # a fill that flips +10 to -10 is 20 contracts, not 0.
            filled[d] += sum(e["size"] for e in state.fill_events[n_events:])
            peak[d] = max(peak[d], abs(state.net_yes_contracts))

    open_tickers = {tk for d in inv for tk, s in inv[d].items() if s.net_yes_contracts}
    results_yes = _settlement_results(sorted(open_tickers)) if settle_via_api else {}

    results = {}
    for d in inv:
        realized = settled = marked = 0.0
        for tk, s in inv[d].items():
            realized += s.realized_pnl_cents
            if not s.net_yes_contracts:
                continue
            if tk in results_yes:
                settled += s.settle(results_yes[tk])  # books into realized_pnl_cents
            elif tk in last_mid:
                marked += s.mark_to_market_pnl_cents(last_mid[tk])
        results[d] = {
            "filled": filled[d],
            "rate": filled[d] / hours,
            "peak": peak[d],
            "passive": realized / 100.0,
            "settled": settled / 100.0,
            "marked": marked / 100.0,
            "total": (realized + settled + marked) / 100.0,
        }
    return have, results, hours


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-offset", type=int, default=5)
    ap.add_argument("--sample", type=int, default=20, help="use every Nth book snapshot")
    ap.add_argument("--since", type=float, default=0.0, help="unix ts floor")
    ap.add_argument("--no-settle", action="store_true",
                    help="skip the Kalshi settlement lookup; mark open positions at last mid")
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
    print(f"  {'offset':>7} {'mean score':>11} {'vs d=0':>8} {'both qualify':>13} {'reward $':>10}")
    base = curve.get(0, (0.0, 0.0, 0.0))[0]
    for d in sorted(curve):
        sc, q, usd = curve[d]
        rel = f"{100*sc/base:6.1f}%" if base else "    n/a"
        print(f"  {d:>7} {sc:11.5f} {rel:>8} {q:12.1f}% {usd:10.2f}")
    print("  reward $ is an UPPER BOUND: it assumes no competitor reacts to our size.")

    print()
    print("=== FILL COST vs OFFSET (needs the trade tape) ===")
    have, fills, hours = fill_curve(conn, args.max_offset, args.since, qs, mi,
                                    settle_via_api=not args.no_settle)
    if not have:
        print("  NO TRADE TAPE YET for this window.")
        print("  The tape started being recorded 2026-10-13; re-run once it has")
        print("  accumulated. Until then the fill half of the gate is UNANSWERED,")
        print("  and the score half alone cannot decide it -- backing off is only")
        print("  worth it if fills fall FASTER than score.")
        return 0
    print(f"  {have} trades over {hours:.1f}h; quote held throughout, no taker exits")
    print(f"  {'offset':>7} {'filled ct':>10} {'fills/hr':>9} {'peak inv':>9} "
          f"{'passive $':>10} {'settled $':>10} {'marked $':>9} {'fill P&L $':>11}")
    for d in sorted(fills):
        f = fills[d]
        print(f"  {d:>7} {f['filled']:>10} {f['rate']:9.1f} {f['peak']:>9} "
              f"{f['passive']:10.2f} {f['settled']:10.2f} {f['marked']:9.2f} {f['total']:11.2f}")

    print()
    print("=== NET (reward upper bound + fill P&L) ===")
    print(f"  {'offset':>7} {'reward $':>10} {'fill P&L $':>11} {'net $':>10} {'net if reward halved':>21}")
    best = None
    for d in sorted(fills):
        usd = curve.get(d, (0.0, 0.0, 0.0))[2]
        fp = fills[d]["total"]
        net, net_half = usd + fp, usd / 2 + fp
        print(f"  {d:>7} {usd:10.2f} {fp:11.2f} {net:10.2f} {net_half:21.2f}")
        if best is None or net_half > best[1]:
            best = (d, net_half)
    print()
    print("  DECISION RULE (BLUEPRINT.md section 6, Phase 0.5): pick the offset")
    print("  that stays net positive under a competitive haircut. Halving reward")
    print("  is a stress test, not an estimate -- Phase 1 measures the real ratio.")
    if best and best[1] > 0:
        print(f"  -> offset {best[0]} (net ${best[1]:,.2f} with reward halved)")
    else:
        print("  -> NO offset survives the haircut. Stop; do not run Phase 1.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
