"""
Sanity tests for the pieces that don't require network access: order book
reconstruction, the reward-scoring formula, and the inventory simulator.
Run with: python3 test_logic.py
"""

import sqlite3
import time
from datetime import datetime, timedelta, timezone

from logger import SCHEMA
from orderbook import OrderbookManager, MarketBook
from reward_simulator import compute_reference_price, score_hypothetical_order, score_both_sides
from adverse_selection_sim import InventoryState, HypotheticalQuote, process_trade_for_inventory
from incentive_screener import ScreenedProgram
from main import build_market_states, compute_my_share_at_touched_price
from risk_engine import RiskEngine, LOW, ELEVATED, HIGH
from quoting_policy import (
    compute_quote,
    maybe_execute_stop_loss,
    maybe_execute_time_based_exit,
    effective_take_profit_margin,
    classify_fill_exit_reason,
)
from news_feed import score_relevance, is_relevant, derive_keywords, _parse_articles, _build_news_query
from market_switcher import (
    MarketSwitcher, compute_crowding_ratio,
    is_worthwhile_crowding_switch, is_reward_preserving_switch,
)
from event_calendar import hours_until_close
from incentive_screener import _adjusted_score, _estimate_crowding, _estimate_recent_volatility, select_watchlist
from main import _select_initial_watchlist, MarketRuntimeState
from capital_allocator import allocate_capital
from server import (
    _risk_reason, _is_recent, _capital_allocation, _performance_history,
    _program_reward_per_second,
)
import quoting_policy
import server
from incentive_screener import _reward_density
import config


def test_orderbook_snapshot_and_delta():
    # Shape CONFIRMED live 2026-09-01: sid/seq top-level, price/size as
    # "_dollars_fp" decimal-string pairs. See orderbook.py's handle_message.
    mgr = OrderbookManager()
    mgr.handle_message({
        "type": "orderbook_snapshot",
        "sid": 1,
        "seq": 100,
        "msg": {
            "market_ticker": "TEST",
            "yes_dollars_fp": [["0.4500", "200.00"], ["0.4400", "150.00"]],
            "no_dollars_fp": [["0.4000", "300.00"], ["0.3900", "100.00"]],
        },
    })
    book = mgr.books["TEST"]
    assert not book.stale
    assert book.best_yes_bid() == (45, 200.0)
    assert book.best_no_bid() == (40, 300.0)

    # clean delta (best-effort inferred field names -- not yet confirmed
    # against a live delta message, see orderbook.py's docstring)
    mgr.handle_message({
        "type": "orderbook_delta",
        "sid": 1,
        "seq": 101,
        "msg": {"market_ticker": "TEST", "side": "yes", "price_dollars": "0.4500", "delta_fp": "50.00"},
    })
    assert book.yes_levels[45] == 250.0
    assert not book.stale

    # gap -> should mark stale
    mgr.handle_message({
        "type": "orderbook_delta",
        "sid": 1,
        "seq": 105,
        "msg": {"market_ticker": "TEST", "side": "yes", "price_dollars": "0.4500", "delta_fp": "10.00"},
    })
    assert book.stale, "expected sequence gap to mark book stale"
    print("test_orderbook_snapshot_and_delta: PASS")


def test_orderbook_delta_to_zero_removes_level():
    """A level cancelled to exactly zero must not stay in the book."""
    book = MarketBook(ticker="TEST")
    book.apply_snapshot(sid=1, seq=1, yes=[(50, 100.0), (49, 20.0)], no=[(48, 40.0)])
    book.apply_delta(side="yes", price=50, delta=-100.0)
    assert 50 not in book.yes_levels, book.yes_levels
    assert book.best_yes_bid() == (49, 20.0), book.best_yes_bid()
    print("test_orderbook_delta_to_zero_removes_level: PASS")


def test_orderbook_float_residue_does_not_leave_phantom_level():
    """
    THE 2026-10-03 BUG. Sizes are floats (Kalshi's _fp fields are
    fractional), so cancelling a level down to "nothing" lands on binary
    residue, not 0.0 -- 0.1+0.1+0.1-0.3 is 2.8e-17, not zero. The old
    `new_size <= 0` test left that level in the book forever, and since
    best_yes_bid() is max(price) over existing keys, the phantom became the
    best bid and poisoned the mid, the quote placement, the fill test and
    the P&L mark all at once.
    """
    book = MarketBook(ticker="TEST")
    book.apply_snapshot(sid=1, seq=1, yes=[(9, 634.0)], no=[(90, 182.0)])
    # A fractional level appears at a BETTER price, then is fully cancelled.
    for _ in range(3):
        book.apply_delta(side="yes", price=12, delta=0.1)
    book.apply_delta(side="yes", price=12, delta=-0.3)

    residue = book.yes_levels.get(12)
    assert residue is None, f"phantom level survived at size {residue!r}"
    assert book.best_yes_bid() == (9, 634.0), book.best_yes_bid()
    assert 12 not in dict(book.yes_side_sorted_desc())
    print("test_orderbook_float_residue_does_not_leave_phantom_level: PASS")


def test_orderbook_preserves_legitimately_fractional_sizes():
    """
    Guards the fix against being "simplified" into integer rounding. Kalshi
    really does send fractional sizes (11% of observed levels), so a size
    like 5787.01 must survive untouched -- only sub-epsilon residue is
    dropped.
    """
    book = MarketBook(ticker="TEST")
    book.apply_snapshot(sid=1, seq=1, yes=[(50, 5787.01)], no=[(48, 0.25)])
    assert book.yes_levels[50] == 5787.01, book.yes_levels
    assert book.no_levels[48] == 0.25, book.no_levels
    assert book.best_yes_bid() == (50, 5787.01)
    assert book.best_no_bid() == (48, 0.25)
    # a small-but-real level is still real
    book.apply_delta(side="yes", price=51, delta=0.5)
    assert book.best_yes_bid() == (51, 0.5), book.best_yes_bid()
    print("test_orderbook_preserves_legitimately_fractional_sizes: PASS")


def test_orderbook_snapshot_filters_zero_levels():
    """A snapshot carrying a zero level must not seed a phantom best bid."""
    book = MarketBook(ticker="TEST")
    book.apply_snapshot(sid=1, seq=1, yes=[(12, 0.0), (9, 634.0)], no=[(90, 182.0)])
    assert 12 not in book.yes_levels, book.yes_levels
    assert book.best_yes_bid() == (9, 634.0), book.best_yes_bid()
    print("test_orderbook_snapshot_filters_zero_levels: PASS")


def test_orderbook_delta_unrecognized_shape_marks_stale_not_crash():
    mgr = OrderbookManager()
    mgr.handle_message({
        "type": "orderbook_snapshot",
        "sid": 1,
        "seq": 1,
        "msg": {
            "market_ticker": "TEST2",
            "yes_dollars_fp": [["0.5000", "100.00"]],
            "no_dollars_fp": [["0.4800", "100.00"]],
        },
    })
    book = mgr.books["TEST2"]
    assert not book.stale

    # If a real delta doesn't match our best-effort field-name guess, this
    # must be logged and the book marked stale (triggering a resync) --
    # NOT an unhandled crash that kills the whole WebSocket session.
    mgr.handle_message({
        "type": "orderbook_delta",
        "sid": 1,
        "seq": 2,
        "msg": {"market_ticker": "TEST2", "totally_unexpected_field": 123},
    })
    assert book.stale, "expected unrecognized delta shape to mark book stale, not crash"
    print("test_orderbook_delta_unrecognized_shape_marks_stale_not_crash: PASS")


def test_orderbook_seq_is_shared_across_tickers_under_one_sid():
    """
    Regression test for a real bug found live 2026-09-01: `seq` is a
    single counter shared across the WHOLE subscription (sid), not
    independent per ticker. A quiet ticker interleaved with a busy one
    must NOT be spuriously marked stale just because the busy ticker
    consumed several seq numbers in between the quiet ticker's updates.
    """
    mgr = OrderbookManager()
    mgr.handle_message({
        "type": "orderbook_snapshot", "sid": 1, "seq": 1,
        "msg": {"market_ticker": "QUIET", "yes_dollars_fp": [["0.5000", "100.00"]], "no_dollars_fp": [["0.4800", "100.00"]]},
    })
    mgr.handle_message({
        "type": "orderbook_snapshot", "sid": 1, "seq": 2,
        "msg": {"market_ticker": "BUSY", "yes_dollars_fp": [["0.5000", "100.00"]], "no_dollars_fp": [["0.4800", "100.00"]]},
    })
    quiet_book = mgr.books["QUIET"]
    busy_book = mgr.books["BUSY"]
    assert not quiet_book.stale and not busy_book.stale

    # BUSY receives several real deltas, consuming seq 3, 4, 5.
    for seq in (3, 4, 5):
        mgr.handle_message({
            "type": "orderbook_delta", "sid": 1, "seq": seq,
            "msg": {"market_ticker": "BUSY", "side": "yes", "price_dollars": "0.5000", "delta_fp": "1.00"},
        })
    assert not busy_book.stale
    assert not quiet_book.stale, "QUIET should be unaffected by BUSY's activity"

    # QUIET's own next delta correctly continues the SHARED counter at 6
    # -- under the old (buggy) per-ticker tracking this would have looked
    # like a huge gap (QUIET's own last seq was 1) and wrongly marked it
    # stale.
    mgr.handle_message({
        "type": "orderbook_delta", "sid": 1, "seq": 6,
        "msg": {"market_ticker": "QUIET", "side": "yes", "price_dollars": "0.5000", "delta_fp": "5.00"},
    })
    assert not quiet_book.stale, "a valid shared-sequence delta must not be treated as a gap"
    assert quiet_book.yes_levels[50] == 105.0
    print("test_orderbook_seq_is_shared_across_tickers_under_one_sid: PASS")


def test_reference_price():
    # Target size 100 -> threshold is 20 (1/5 of target).
    levels_desc = [(50, 10), (49, 15), (48, 5)]  # cumulative: 10, 25, 30
    ref = compute_reference_price(levels_desc, target_size=100)
    assert ref == 49, f"expected reference price 49, got {ref}"

    # Never reaches threshold
    ref2 = compute_reference_price([(50, 1)], target_size=100)
    assert ref2 is None
    print("test_reference_price: PASS")


def test_score_hypothetical_order_qualifies_and_shares_correctly():
    # Existing book: 50 contracts at 50c, 50 at 49c -> total 100, meets target of 100.
    levels_desc = [(50, 50), (49, 50)]
    target_size = 100
    discount_factor = 0.9

    # We add 50 contracts at 50c (at-or-better than reference -> full weight).
    result = score_hypothetical_order(levels_desc, target_size, discount_factor, my_price=50, my_size=50)
    assert result.qualifies
    # total resting after adding ours: 100 at 50c, 50 at 49c = 150 total contracts.
    # reference price: threshold = 100/5 = 20. cumulative from top: 100 (at 50c) >= 20 -> ref = 50c.
    assert result.reference_price == 50
    # distance multiplier at 50c (>= ref) = 1.0 for everyone; at 49c (< ref) discounted.
    # raw scores: 100*1.0 (at 50c, includes ours) + 50*0.9 (at 49c) = 100 + 45 = 145
    # our raw score = our 50 contracts * 1.0 = 50
    expected_share = 50 / 145
    assert abs(result.my_share - expected_share) < 1e-9, (result.my_share, expected_share)
    print("test_score_hypothetical_order_qualifies_and_shares_correctly: PASS")


def test_score_hypothetical_order_fails_target():
    levels_desc = [(50, 5)]
    result = score_hypothetical_order(levels_desc, target_size=100, discount_factor=0.9, my_price=50, my_size=5)
    assert not result.qualifies
    assert result.my_share == 0.0
    print("test_score_hypothetical_order_fails_target: PASS")


def test_inventory_yes_fill_and_pnl():
    inv = InventoryState(ticker="TEST")
    applied = inv.apply_yes_fill(price=40, size=100, max_inventory=500)
    assert applied == 100
    assert inv.net_yes_contracts == 100
    assert inv.avg_entry_price == 40

    # mark to market: price rose to 45 -> unrealized gain of 5c * 100 = 500
    pnl = inv.mark_to_market_pnl_cents(current_mid_yes_price=45)
    assert pnl == 500, pnl

    # now sell (apply_no_fill) 60 contracts at no_price=57 (equiv yes price 43)
    inv.apply_no_fill(no_price=57, size=60, max_inventory=500)
    assert inv.net_yes_contracts == 40
    # realized pnl on 60 contracts covered: (43 - 40) * 60 = 180
    assert abs(inv.realized_pnl_cents - 180) < 1e-9, inv.realized_pnl_cents
    print("test_inventory_yes_fill_and_pnl: PASS")


def test_inventory_respects_max_position():
    inv = InventoryState(ticker="TEST")
    applied = inv.apply_yes_fill(price=50, size=1000, max_inventory=500)
    assert applied == 500, "should clamp fill to max_inventory"
    assert inv.net_yes_contracts == 500
    print("test_inventory_respects_max_position: PASS")


def test_process_trade_for_inventory_end_to_end():
    inv = InventoryState(ticker="TEST")
    quote = HypotheticalQuote(yes_price=50, yes_size=100, no_price=48, no_size=100)

    # A trade prints at yes_price=50 with taker_side="no" (someone sold yes,
    # hitting bids at or below 50) -> should fill our yes bid.
    process_trade_for_inventory(
        inventory=inv, quote=quote, trade_yes_price=50, trade_count=100,
        taker_side="no", my_share_at_price=0.2, max_inventory=500,
    )
    assert inv.net_yes_contracts == 20, inv.net_yes_contracts  # 100 * 0.2 share

    # A trade prints where taker buys yes at yes_price=53 (implied no side
    # price = 47, which is <= our no_price=48) -> should fill our no bid.
    process_trade_for_inventory(
        inventory=inv, quote=quote, trade_yes_price=53, trade_count=50,
        taker_side="yes", my_share_at_price=0.2, max_inventory=500,
    )
    assert inv.net_yes_contracts == 10, inv.net_yes_contracts  # 20 - (50*0.2)
    print("test_process_trade_for_inventory_end_to_end: PASS")


def test_build_market_states_uses_real_incentive_program_values():
    now = datetime.now(timezone.utc)
    program = ScreenedProgram(
        market_ticker="FOUND-TICKER",
        period_reward=500.0,
        target_size=750.0,
        discount_factor_bps=8500,
        start_date=now - timedelta(days=4),
        end_date=now,
        days_remaining=3.0,
        reward_density_score=1.0,
        raw={},
    )

    states = build_market_states(
        watched_tickers=["FOUND-TICKER", "MISSING-TICKER"],
        screened_programs=[program],
    )

    assert "FOUND-TICKER" in states
    assert states["FOUND-TICKER"].target_size == 750.0
    assert abs(states["FOUND-TICKER"].discount_factor - 0.85) < 1e-9

    # No active program for this ticker -> should be skipped, not defaulted.
    assert "MISSING-TICKER" not in states

    # Regression check for the 2026-09-01 estimate-inflation bug: total
    # program period length must be derivable (here, 4 days).
    assert abs(program.total_period_days - 4.0) < 1e-6
    print("test_build_market_states_uses_real_incentive_program_values: PASS")


def test_compute_my_share_at_touched_price_level():
    book = MarketBook(ticker="TEST")
    book.apply_snapshot(sid=1, seq=1, yes=[[50, 100]], no=[[48, 40]])
    quote = HypotheticalQuote(yes_price=50, yes_size=100, no_price=48, no_size=100)

    # taker_side "no" touches the YES side at trade_yes_price=50, where 100
    # is already resting -- our hypothetical 100 should be exactly half.
    share_yes_side = compute_my_share_at_touched_price(book, quote, trade_yes_price=50, taker_side="no")
    assert abs(share_yes_side - 0.5) < 1e-9, share_yes_side

    # taker_side "yes" touches the NO side at (100 - 48) = 52, where
    # nothing is resting -- our hypothetical size should be the whole share.
    share_no_side = compute_my_share_at_touched_price(book, quote, trade_yes_price=48, taker_side="yes")
    assert abs(share_no_side - 1.0) < 1e-9, share_no_side
    print("test_compute_my_share_at_touched_price_level: PASS")


def test_compute_quote_skews_toward_flattening_long_inventory():
    book = MarketBook(ticker="TEST")
    book.apply_snapshot(sid=1, seq=1, yes=[[50, 100]], no=[[48, 100]])

    flat_inventory = InventoryState(ticker="TEST")
    long_inventory = InventoryState(ticker="TEST", net_yes_contracts=400)

    flat_quote = compute_quote(book, LOW, flat_inventory, base_size=100, max_inventory=500, target_size=100)
    long_quote = compute_quote(book, LOW, long_inventory, base_size=100, max_inventory=500, target_size=100)

    assert flat_quote is not None and long_quote is not None
    # Long yes should skew the quote to encourage flattening: worse (lower)
    # yes bid, better (higher) no bid.
    assert long_quote.yes_price <= flat_quote.yes_price
    assert long_quote.no_price >= flat_quote.no_price
    assert (long_quote.yes_price, long_quote.no_price) != (flat_quote.yes_price, flat_quote.no_price)
    print("test_compute_quote_skews_toward_flattening_long_inventory: PASS")


def test_compute_quote_rests_at_reference_price_not_touch_when_they_differ():
    # target_size=1000 -> threshold = 200. Neither side's touch alone
    # reaches that; walking down, cumulative crosses 200 at the 3rd level
    # on each side -- so Reference Price sits 2 ticks below the touch on
    # both sides. compute_quote should rest there (free safety margin,
    # same 1.0 multiplier), not at the touch.
    book = MarketBook(ticker="TEST")
    book.apply_snapshot(
        sid=1, seq=1,
        yes=[[50, 50], [49, 60], [48, 100]],   # cumulative: 50, 110, 210
        no=[[48, 50], [47, 60], [46, 100]],    # cumulative: 50, 110, 210
    )
    inventory = InventoryState(ticker="TEST")

    quote = compute_quote(book, LOW, inventory, base_size=100, max_inventory=500, target_size=1000)

    assert quote is not None
    assert quote.yes_price == 48, quote.yes_price  # NOT the touch (50)
    assert quote.no_price == 46, quote.no_price     # NOT the touch (48)

    # Sanity-check this is genuinely free: the multiplier at the Reference
    # Price itself is always 1.0 by definition (price >= reference_price),
    # already covered by test_score_hypothetical_order_qualifies_and_shares_correctly --
    # this test only needs to confirm compute_quote actually picks that
    # price instead of the touch, which the asserts above already do.
    print("test_compute_quote_rests_at_reference_price_not_touch_when_they_differ: PASS")


def test_risk_engine_escalates_to_high_on_volatile_mid():
    engine = RiskEngine(ticker="TEST")
    inventory = InventoryState(ticker="TEST")

    # Feed a stable sequence, long enough to fill the rolling window --
    # should remain LOW throughout.
    stable_mid = 50.0
    for _ in range(40):
        engine.update(stable_mid, inventory, max_inventory=500)
    assert engine.risk_level == LOW, engine.risk_level

    # Now feed large alternating swings that exceed VOLATILITY_HIGH_THRESHOLD.
    mid = stable_mid
    for i in range(40):
        mid = 50.0 + (10.0 if i % 2 == 0 else -10.0)
        engine.update(mid, inventory, max_inventory=500)
    assert engine.risk_level == HIGH, engine.risk_level
    print("test_risk_engine_escalates_to_high_on_volatile_mid: PASS")


def test_maybe_execute_stop_loss_flattens_at_hard_stop_and_not_otherwise():
    book = MarketBook(ticker="TEST")
    book.apply_snapshot(sid=1, seq=1, yes=[[50, 200]], no=[[48, 200]])

    # Long yes at/above the hard-stop fraction, risk HIGH -> should fire.
    inv = InventoryState(ticker="TEST", net_yes_contracts=420, avg_entry_price=45.0)
    fill = maybe_execute_stop_loss(
        book, inv, risk_level=HIGH, max_inventory=500, hard_stop_fraction=0.8,
    )
    assert fill is not None
    assert fill["side"] == "no"
    assert fill["price"] == 100 - 50  # crossed the best yes bid (50c) -> equiv no_price
    assert fill["size"] == 420
    assert fill["net_yes_contracts_before"] == 420
    assert fill["net_yes_contracts_after"] == 0
    assert inv.net_yes_contracts == 0, inv.net_yes_contracts

    # Negative case: same inventory fraction, but risk not HIGH -> no fire.
    inv2 = InventoryState(ticker="TEST", net_yes_contracts=420, avg_entry_price=45.0)
    assert maybe_execute_stop_loss(book, inv2, risk_level=LOW, max_inventory=500, hard_stop_fraction=0.8) is None
    assert inv2.net_yes_contracts == 420

    # Negative case: risk HIGH, but inventory below the hard-stop threshold.
    inv3 = InventoryState(ticker="TEST", net_yes_contracts=100, avg_entry_price=45.0)
    assert maybe_execute_stop_loss(book, inv3, risk_level=HIGH, max_inventory=500, hard_stop_fraction=0.8) is None
    assert inv3.net_yes_contracts == 100
    print("test_maybe_execute_stop_loss_flattens_at_hard_stop_and_not_otherwise: PASS")


def test_maybe_execute_stop_loss_refined_threshold_protects_small_allocations():
    # A $150 max_inventory market with a 100-contract quote size: the
    # pure-fraction threshold (0.8 * 150 = 120) would fire after barely
    # more than a single fill. The refined threshold --
    # max(120, order_size + min_buffer) = max(120, 100+50=150) = 150 --
    # must NOT fire at 120, only once inventory actually reaches 150.
    book = MarketBook(ticker="TEST")
    book.apply_snapshot(sid=1, seq=1, yes=[[50, 200]], no=[[48, 200]])

    inv = InventoryState(ticker="TEST", net_yes_contracts=120, avg_entry_price=45.0)
    assert maybe_execute_stop_loss(
        book, inv, risk_level=HIGH, max_inventory=150, hard_stop_fraction=0.8,
        order_size=100, min_buffer=50,
    ) is None, "must not fire at the old 80%-of-cap point once the refined floor is higher"
    assert inv.net_yes_contracts == 120

    inv2 = InventoryState(ticker="TEST", net_yes_contracts=150, avg_entry_price=45.0)
    fill = maybe_execute_stop_loss(
        book, inv2, risk_level=HIGH, max_inventory=150, hard_stop_fraction=0.8,
        order_size=100, min_buffer=50,
    )
    assert fill is not None, "must fire once inventory reaches the refined (higher) floor"

    # A LARGE max_inventory market: the fraction term dominates and the
    # refined threshold reduces to the original behavior -- unaffected.
    inv3 = InventoryState(ticker="TEST", net_yes_contracts=420, avg_entry_price=45.0)
    fill3 = maybe_execute_stop_loss(
        book, inv3, risk_level=HIGH, max_inventory=500, hard_stop_fraction=0.8,
        order_size=100, min_buffer=50,
    )
    assert fill3 is not None, "large max_inventory should still fire at 80% (420/500), unaffected by the buffer"
    print("test_maybe_execute_stop_loss_refined_threshold_protects_small_allocations: PASS")


def test_maybe_execute_time_based_exit_fires_below_critical_and_not_above():
    book = MarketBook(ticker="TEST")
    book.apply_snapshot(sid=1, seq=1, yes=[[50, 200]], no=[[48, 200]])

    # Well above the critical window -> no fire, regardless of risk level
    # (this trigger is time-driven, not risk-driven).
    inv = InventoryState(ticker="TEST", net_yes_contracts=50, avg_entry_price=45.0)
    assert maybe_execute_time_based_exit(book, inv, minutes_until_close=60.0, max_inventory=500, critical_minutes=15) is None
    assert inv.net_yes_contracts == 50

    # At/below the critical threshold -> forced flatten.
    fill = maybe_execute_time_based_exit(book, inv, minutes_until_close=10.0, max_inventory=500, critical_minutes=15)
    assert fill is not None
    assert fill["reason"] == "pre_resolution_flatten"
    assert inv.net_yes_contracts == 0

    # Unknown or already-past-close (negative) -> no fire.
    inv2 = InventoryState(ticker="TEST", net_yes_contracts=50, avg_entry_price=45.0)
    assert maybe_execute_time_based_exit(book, inv2, minutes_until_close=None, max_inventory=500, critical_minutes=15) is None
    assert maybe_execute_time_based_exit(book, inv2, minutes_until_close=-5.0, max_inventory=500, critical_minutes=15) is None
    print("test_maybe_execute_time_based_exit_fires_below_critical_and_not_above: PASS")


def test_effective_take_profit_margin_decays_with_time_and_hits_zero_at_critical():
    full, window, critical = 4.0, 60.0, 15.0

    # Outside the window (or unknown / already past close) -> full margin.
    assert effective_take_profit_margin(120.0, full, window, critical) == full
    assert effective_take_profit_margin(None, full, window, critical) == full
    assert effective_take_profit_margin(-5.0, full, window, critical) == full

    # At the window's outer edge -> full margin; at/below critical -> 0;
    # halfway through the decay span -> half the margin.
    assert abs(effective_take_profit_margin(window, full, window, critical) - full) < 1e-9
    assert effective_take_profit_margin(critical, full, window, critical) == 0.0
    assert effective_take_profit_margin(5.0, full, window, critical) == 0.0
    midpoint = critical + (window - critical) / 2
    assert abs(effective_take_profit_margin(midpoint, full, window, critical) - full / 2) < 1e-9
    print("test_effective_take_profit_margin_decays_with_time_and_hits_zero_at_critical: PASS")


def test_compute_quote_applies_take_profit_floor_on_exit_side():
    # target_size=100 -> reference price == touch (50/48) on both sides
    # here, so the pre-skew base quote is just the touch.
    book = MarketBook(ticker="TEST")
    book.apply_snapshot(sid=1, seq=1, yes=[[50, 500]], no=[[48, 500]])

    # Fully at cap (net_yes_contracts == max_inventory) -> skew is
    # capped at MAX_INVENTORY_SKEW_TICKS (5), pushing no_price from the
    # touch (48) up to 53. Off a 46c entry, that's only 1c of profit
    # (100-53=47, 47-46=1) -- well under the 4c margin -- so the
    # take-profit floor must cap it back down to 100-(46+4)=50.
    inv = InventoryState(ticker="TEST", net_yes_contracts=100, avg_entry_price=46.0)
    quote = compute_quote(book, LOW, inv, base_size=100, max_inventory=100, target_size=100)
    assert quote is not None
    assert quote.no_price == 50, quote.no_price  # capped, not the naturally-skewed 53

    # Same skew (no_price naturally 53), but a lower entry price (40c)
    # where even the fully-skewed price already clears the margin
    # (100-53=47, 47-40=7 >= 4) -- take-profit should be a no-op.
    inv2 = InventoryState(ticker="TEST", net_yes_contracts=100, avg_entry_price=40.0)
    quote2 = compute_quote(book, LOW, inv2, base_size=100, max_inventory=100, target_size=100)
    assert quote2 is not None
    assert quote2.no_price == 53, quote2.no_price  # unconstrained by take-profit

    # Flat inventory -> no take-profit floor applied at all (nothing to
    # take profit on); quote is just the touch, unaffected by avg_entry_price.
    flat_inv = InventoryState(ticker="TEST", net_yes_contracts=0, avg_entry_price=0.0)
    flat_quote = compute_quote(book, LOW, flat_inv, base_size=100, max_inventory=100, target_size=100)
    assert flat_quote.no_price == 48, flat_quote.no_price
    print("test_compute_quote_applies_take_profit_floor_on_exit_side: PASS")


def test_classify_fill_exit_reason():
    # Flat to start -> never an exit, regardless of side/price.
    assert classify_fill_exit_reason(0, 0.0, "no", 52, 4.0) is None

    # Long yes, entry-side fill (more YES) -> not an exit.
    assert classify_fill_exit_reason(50, 44.0, "yes", 44, 4.0) is None

    # Long yes, exit-side (NO) fill at 52c -> equiv yes-sell 48, exactly
    # the 4c margin off a 44 entry -> take_profit_exit.
    assert classify_fill_exit_reason(50, 44.0, "no", 52, 4.0) == "take_profit_exit"

    # Long yes, exit-side (NO) fill at 55c -> equiv yes-sell 45, only 1c
    # of profit off a 44 entry -- below the 4c margin.
    assert classify_fill_exit_reason(50, 44.0, "no", 55, 4.0) == "exit"

    # Short yes (long no), exit-side (YES) fill at 40c, entry 46c -> 6c
    # profit, meets margin.
    assert classify_fill_exit_reason(-50, 46.0, "yes", 40, 4.0) == "take_profit_exit"
    # Short yes, exit fill at 44c -> only 2c profit, below margin.
    assert classify_fill_exit_reason(-50, 46.0, "yes", 44, 4.0) == "exit"
    print("test_classify_fill_exit_reason: PASS")


def test_score_relevance_keyword_overlap():
    keywords = ["hurricane", "florida", "landfall", "category"]

    # Synthetic normalized article (see news_feed._parse_articles): title overlaps 3/4 keywords.
    relevant_article = {
        "headline": "Hurricane makes landfall in Florida overnight",
        "source_url": "https://example.com/a",
        "theme_tags": [],
    }
    score = score_relevance(keywords, relevant_article)
    assert abs(score - 0.75) < 1e-9, score
    assert score >= config.NEWS_RELEVANCE_THRESHOLD

    # Unrelated article shares none of the keywords -> score 0.
    unrelated_article = {
        "headline": "Local bakery wins regional pastry award",
        "source_url": "https://example.com/b",
        "theme_tags": [],
    }
    assert score_relevance(keywords, unrelated_article) == 0.0

    # Theme tags count toward the overlap too, not just the title.
    tagged_article = {
        "headline": "Storm update",
        "source_url": "https://example.com/c",
        "theme_tags": ["florida", "category"],
    }
    tagged_score = score_relevance(keywords, tagged_article)
    assert abs(tagged_score - 0.5) < 1e-9, tagged_score
    print("test_score_relevance_keyword_overlap: PASS")


def test_risk_engine_news_signal_escalates_then_decays():
    engine = RiskEngine(ticker="TEST")
    inventory = InventoryState(ticker="TEST")
    t0 = 1_000_000.0

    # Baseline: stable mid, flat inventory, no news yet -> stays LOW.
    engine.update(50.0, inventory, max_inventory=500, now=t0)
    assert engine.risk_level == LOW
    assert engine.news_signal is False

    # A relevant news hit nudges risk up toward ELEVATED even though
    # volatility/inventory alone would still say LOW.
    engine.register_news_event(now=t0)
    engine.update(50.0, inventory, max_inventory=500, now=t0 + 1)
    assert engine.news_signal is True
    assert engine.risk_level == ELEVATED, engine.risk_level

    # After the configured decay window has elapsed, the signal expires
    # and risk level falls back to LOW (inputs otherwise unchanged).
    decayed_ts = t0 + config.NEWS_SIGNAL_DECAY_MINUTES * 60 + 1
    engine.update(50.0, inventory, max_inventory=500, now=decayed_ts)
    assert engine.news_signal is False
    assert engine.risk_level == LOW, engine.risk_level
    print("test_risk_engine_news_signal_escalates_then_decays: PASS")


def test_compute_crowding_ratio():
    book = MarketBook(ticker="TEST")
    book.apply_snapshot(sid=1, seq=1, yes=[[50, 300]], no=[[48, 300]])
    # (300 + 300) / (2 * target_size), target_size=100 -> 600/200 = 3.0
    ratio = compute_crowding_ratio(book, target_size=100)
    assert abs(ratio - 3.0) < 1e-9, ratio
    assert compute_crowding_ratio(book, target_size=0) is None
    print("test_compute_crowding_ratio: PASS")


def test_hours_until_close():
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)

    future = now + timedelta(hours=5)
    assert abs(hours_until_close(future, now=now) - 5.0) < 1e-9

    # A market already past its close_time -> negative hours (near-settled,
    # NOT flagged as risky -- see RiskEngine.set_event_proximity).
    past = now - timedelta(hours=3)
    assert abs(hours_until_close(past, now=now) - (-3.0)) < 1e-9
    print("test_hours_until_close: PASS")


def test_risk_engine_event_proximity_escalates_within_window():
    engine = RiskEngine(ticker="TEST")
    inventory = InventoryState(ticker="TEST")

    # Well outside the warning window -> stays LOW.
    engine.set_event_proximity(config.EVENT_CALENDAR_WARNING_HOURS + 10)
    engine.update(50.0, inventory, max_inventory=500)
    assert engine.event_signal is False
    assert engine.risk_level == LOW, engine.risk_level

    # Within the warning window -> nudges to ELEVATED even though
    # volatility/inventory alone would still say LOW.
    engine.set_event_proximity(config.EVENT_CALENDAR_WARNING_HOURS - 1)
    engine.update(50.0, inventory, max_inventory=500)
    assert engine.event_signal is True
    assert engine.risk_level == ELEVATED, engine.risk_level

    # Already past close (negative hours) -> settled/near-settled, NOT flagged.
    engine.set_event_proximity(-1.0)
    engine.update(50.0, inventory, max_inventory=500)
    assert engine.event_signal is False
    assert engine.risk_level == LOW, engine.risk_level

    # None (couldn't fetch close_time this poll) -> not flagged either.
    engine.set_event_proximity(None)
    engine.update(50.0, inventory, max_inventory=500)
    assert engine.event_signal is False
    print("test_risk_engine_event_proximity_escalates_within_window: PASS")


class _FakeRestClient:
    """Minimal stand-in for KalshiRestClient's get_orderbook/get_trades,
    used to test incentive_screener's crowding/volatility estimators
    without any network access."""

    def __init__(self, orderbook=None, trades=None):
        self._orderbook = orderbook
        self._trades = trades

    def get_orderbook(self, ticker, depth=None):
        return self._orderbook

    def get_trades(self, ticker, limit=100, **params):
        return self._trades


def test_estimate_crowding_and_volatility_from_fake_client():
    fake_orderbook = {
        "orderbook_fp": {
            "yes_dollars": [["0.5000", "150.00"]],
            "no_dollars": [["0.4800", "150.00"]],
        }
    }
    client = _FakeRestClient(orderbook=fake_orderbook)
    # (150 + 150) / (2 * target_size), target_size=100 -> 300/200 = 1.5
    ratio = _estimate_crowding(client, "TEST", target_size=100)
    assert abs(ratio - 1.5) < 1e-9, ratio
    assert _estimate_crowding(client, "TEST", target_size=0) is None

    fake_trades = {"trades": [
        {"yes_price_dollars": "0.5000"},
        {"yes_price_dollars": "0.5200"},
        {"yes_price_dollars": "0.4800"},
    ]}
    client2 = _FakeRestClient(trades=fake_trades)
    vol = _estimate_recent_volatility(client2, "TEST", lookback_trades=50)
    assert vol is not None and vol > 0, vol

    # Fewer than 2 usable prices -> None, not a crash.
    client3 = _FakeRestClient(trades={"trades": [{"yes_price_dollars": "0.5000"}]})
    assert _estimate_recent_volatility(client3, "TEST", lookback_trades=50) is None
    print("test_estimate_crowding_and_volatility_from_fake_client: PASS")


def test_adjusted_score_dampens_for_crowding_and_volatility():
    base = _adjusted_score(1.0, crowding_ratio=None, volatility_cents=None)
    assert base == 1.0

    crowded = _adjusted_score(1.0, crowding_ratio=3.0, volatility_cents=None)
    assert crowded < base

    volatile = _adjusted_score(1.0, crowding_ratio=None, volatility_cents=5.0)
    assert volatile < base

    both = _adjusted_score(1.0, crowding_ratio=3.0, volatility_cents=5.0)
    assert both < crowded and both < volatile
    print("test_adjusted_score_dampens_for_crowding_and_volatility: PASS")


def test_market_switcher_trigger_requires_sustain_and_cooldown():
    switcher = MarketSwitcher()
    t0 = 1_000_000.0

    # Not sustained yet -- must not fire on the very first sighting.
    assert switcher.check_trigger("A", HIGH, None, now=t0) is None
    # Still under the sustain window.
    assert switcher.check_trigger("A", HIGH, None, now=t0 + config.SWITCH_TRIGGER_SUSTAIN_SEC - 1) is None
    # Sustained long enough, no prior switch -> fires.
    trigger = switcher.check_trigger("A", HIGH, None, now=t0 + config.SWITCH_TRIGGER_SUSTAIN_SEC + 1)
    assert trigger == "volatile", trigger

    switch_ts = t0 + config.SWITCH_TRIGGER_SUSTAIN_SEC + 1
    switcher.record_switch(now=switch_ts)

    # Immediately after switching, even though the condition still holds,
    # the global cooldown blocks another switch.
    assert switcher.check_trigger("A", HIGH, None, now=switch_ts + 1) is None

    # A different ticker, crowded (not volatile), sustained past both the
    # sustain window AND the cooldown window from the first switch.
    t1 = switch_ts + config.MARKET_SWITCH_COOLDOWN_SEC + 1
    assert switcher.check_trigger("B", LOW, 10.0, now=t1) is None  # first sighting
    trigger2 = switcher.check_trigger("B", LOW, 10.0, now=t1 + config.SWITCH_TRIGGER_SUSTAIN_SEC + 1)
    assert trigger2 == "crowded", trigger2
    print("test_market_switcher_trigger_requires_sustain_and_cooldown: PASS")


def test_is_worthwhile_crowding_switch_requires_meaningful_improvement():
    # Real incident, 2026-09-30: an absolute crowding threshold alone
    # kept re-triggering a search every cooldown period once EVERY
    # active program exceeded it (observed range ~5.6x-30x+), with no
    # candidate ever being "uncrowded enough" in absolute terms to stop
    # the cycle. The relative comparison must still correctly decline a
    # switch when the "best available" candidate isn't meaningfully
    # better than staying put.
    factor = 0.5  # replacement must be <= 50% as crowded

    # Replacement barely better (8.0 vs 8.5, ratio ~0.94) -- not a
    # meaningful improvement, must decline.
    assert is_worthwhile_crowding_switch(8.5, 8.0, factor) is False

    # Replacement dramatically better (30.0 -> 6.0, ratio 0.2) -- a real
    # improvement, must accept.
    assert is_worthwhile_crowding_switch(30.0, 6.0, factor) is True

    # Exactly at the boundary (replacement == current * factor) -> accept.
    assert is_worthwhile_crowding_switch(10.0, 5.0, factor) is True

    # Unknown crowding on either side -> can't measure an improvement,
    # must decline rather than switch blind.
    assert is_worthwhile_crowding_switch(None, 5.0, factor) is False
    assert is_worthwhile_crowding_switch(10.0, None, factor) is False
    print("test_is_worthwhile_crowding_switch_requires_meaningful_improvement: PASS")


class _FakeScreeningClient:
    """Fake REST client for testing select_watchlist/screen_incentive_programs
    end-to-end (get_all_incentive_programs + per-ticker get_orderbook/
    get_trades) without any network access."""

    def __init__(self, programs, orderbooks, trades=None):
        self._programs = programs
        self._orderbooks = orderbooks
        self._trades = trades or {}

    def get_all_incentive_programs(self, status="active", program_type="liquidity"):
        return self._programs

    def get_orderbook(self, ticker, depth=None):
        return self._orderbooks.get(ticker, {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}})

    def get_trades(self, ticker, limit=100, **params):
        return self._trades.get(ticker, {"trades": []})


def _fake_program(ticker, period_reward_cents, target_size, discount_bps=9000):
    now = datetime.now(timezone.utc)
    return {
        "market_ticker": ticker,
        "start_date": (now - timedelta(days=1)).isoformat(),
        "end_date": (now + timedelta(days=10)).isoformat(),
        "period_reward": period_reward_cents,
        "target_size_fp": target_size,
        "discount_factor_bps": discount_bps,
    }


def _fake_orderbook(yes_size, no_size):
    return {"orderbook_fp": {"yes_dollars": [["0.5000", f"{yes_size:.2f}"]], "no_dollars": [["0.4800", f"{no_size:.2f}"]]}}


def test_select_watchlist_picks_best_available_even_when_all_crowded():
    # CROWDED has by far the highest raw reward density but is very
    # crowded; GOOD-A/B/C are all also crowded, just less so. Regression
    # coverage for the 2026-09-02 fix: select_watchlist must NEVER return
    # fewer than requested (or empty) just because nothing is genuinely
    # uncrowded -- it should rank ALL candidates by adjusted_score and
    # pick the least-bad N, since every currently active real market was
    # observed to already be well-crowded (least-crowded ~8.9x
    # target_size) and a hard cutoff would always find nothing.
    programs = [
        _fake_program("CROWDED", period_reward_cents=100000, target_size=100),
        _fake_program("GOOD-A", period_reward_cents=50000, target_size=100),
        _fake_program("GOOD-B", period_reward_cents=40000, target_size=100),
        _fake_program("GOOD-C", period_reward_cents=30000, target_size=100),
    ]
    orderbooks = {
        "CROWDED": _fake_orderbook(1000, 1000),  # crowding = 2000/200 = 10.0
        "GOOD-A": _fake_orderbook(40, 40),       # 80/200 = 0.4
        "GOOD-B": _fake_orderbook(30, 30),       # 0.3
        "GOOD-C": _fake_orderbook(20, 20),       # 0.2
    }
    client = _FakeScreeningClient(programs, orderbooks)

    selected = select_watchlist(client, num_markets=2)
    tickers = [p.market_ticker for p in selected]
    assert len(tickers) == 2, "must always return num_markets, never fewer, when enough programs exist"
    # CROWDED's much higher raw reward density loses out here because its
    # crowding dampens it below all three GOOD-* candidates -- but it was
    # still RANKED, not excluded outright by a hard cutoff (see
    # test_adjusted_score_dampens_for_crowding_and_volatility for the
    # dampening-not-disqualifying behavior in isolation).
    assert tickers == ["GOOD-A", "GOOD-B"], tickers
    print("test_select_watchlist_picks_best_available_even_when_all_crowded: PASS")


def test_select_watchlist_returns_fewer_than_requested_if_not_enough_programs_exist():
    programs = [_fake_program("ONLY-ONE", period_reward_cents=50000, target_size=100)]
    orderbooks = {"ONLY-ONE": _fake_orderbook(40, 40)}
    client = _FakeScreeningClient(programs, orderbooks)

    selected = select_watchlist(client, num_markets=3)
    assert [p.market_ticker for p in selected] == ["ONLY-ONE"]
    print("test_select_watchlist_returns_fewer_than_requested_if_not_enough_programs_exist: PASS")


def test_select_initial_watchlist_falls_back_only_when_no_active_programs_exist():
    # Zero active programs at all -> _select_initial_watchlist must fall
    # back to config.WATCHED_MARKET_TICKERS rather than starting with an
    # empty watch list. (Crowding alone no longer triggers the fallback --
    # see the two tests above.)
    client = _FakeScreeningClient(programs=[], orderbooks={})

    original_auto = config.AUTO_SELECT_WATCHLIST
    try:
        config.AUTO_SELECT_WATCHLIST = True
        tickers, screened = _select_initial_watchlist(client)
        assert tickers == config.WATCHED_MARKET_TICKERS, tickers
    finally:
        config.AUTO_SELECT_WATCHLIST = original_auto
    print("test_select_initial_watchlist_falls_back_only_when_no_active_programs_exist: PASS")


def _screened(ticker, reward_density_score, adjusted_score=None):
    now = datetime.now(timezone.utc)
    return ScreenedProgram(
        market_ticker=ticker,
        period_reward=0.0,
        target_size=100.0,
        discount_factor_bps=9000,
        start_date=now,
        end_date=now + timedelta(days=10),
        days_remaining=10.0,
        reward_density_score=reward_density_score,
        raw={},
        adjusted_score=adjusted_score,
    )


def test_allocate_capital_weights_by_profit_risk_ratio():
    # No clipping (min=0, max=1) -> allocation should purely follow the
    # adjusted_score (profit/risk) ratio: 3:1 -> 75%/25%.
    programs = [_screened("A", reward_density_score=1.0, adjusted_score=3.0),
                _screened("B", reward_density_score=1.0, adjusted_score=1.0)]
    allocations = allocate_capital(1000.0, programs, min_fraction=0.0, max_fraction=1.0)
    assert allocations["A"] > allocations["B"]
    assert abs(allocations["A"] - 750) <= 1, allocations
    assert abs(allocations["B"] - 250) <= 1, allocations
    print("test_allocate_capital_weights_by_profit_risk_ratio: PASS")


def test_allocate_capital_falls_back_to_reward_density_when_adjusted_score_missing():
    # A's adjusted_score is None (e.g. manual-fallback watch list, which
    # skips the crowding-aware live check) -> must fall back to
    # reward_density_score, not be treated as zero/unweighted.
    programs = [_screened("A", reward_density_score=3.0, adjusted_score=None),
                _screened("B", reward_density_score=1.0, adjusted_score=1.0)]
    allocations = allocate_capital(1000.0, programs, min_fraction=0.0, max_fraction=1.0)
    assert allocations["A"] > allocations["B"], allocations
    print("test_allocate_capital_falls_back_to_reward_density_when_adjusted_score_missing: PASS")


def test_allocate_capital_clips_to_min_and_max_fraction():
    # Heavily skewed weights (100:1:1) -- without clipping, A would get
    # ~98% and starve B/C almost entirely (down to ~1% each). A gets
    # capped at max_fraction=0.60; the remaining 0.40 budget splits
    # evenly across B/C's equal weights (0.20 each), which already
    # clears min_fraction=0.15 on its own -- no further clipping needed.
    programs = [_screened("A", reward_density_score=1.0, adjusted_score=100.0),
                _screened("B", reward_density_score=1.0, adjusted_score=1.0),
                _screened("C", reward_density_score=1.0, adjusted_score=1.0)]
    allocations = allocate_capital(1000.0, programs, min_fraction=0.15, max_fraction=0.60)

    assert allocations == {"A": 600, "B": 200, "C": 200}, allocations
    for ticker in ("A", "B", "C"):
        fraction = allocations[ticker] / 1000.0
        assert 0.15 - 1e-6 <= fraction <= 0.60 + 1e-6, (ticker, fraction, allocations)
    print("test_allocate_capital_clips_to_min_and_max_fraction: PASS")


def test_allocate_capital_min_floor_holds_even_when_max_cap_binds_elsewhere():
    # A dominant ticker capped at max_fraction, PLUS one genuinely
    # under-weighted ticker that needs the min_fraction floor even after
    # the dominant one is capped -- exercises both bounds actually
    # binding in the same allocation, not just one at a time.
    programs = [_screened("A", reward_density_score=1.0, adjusted_score=1000.0),
                _screened("B", reward_density_score=1.0, adjusted_score=10.0),
                _screened("C", reward_density_score=1.0, adjusted_score=0.001)]
    allocations = allocate_capital(1000.0, programs, min_fraction=0.15, max_fraction=0.60)

    assert allocations["A"] == 600, allocations
    assert allocations["C"] == 150, allocations  # floored
    assert allocations["B"] == 250, allocations  # takes the rest: 1000 - 600 - 150
    print("test_allocate_capital_min_floor_holds_even_when_max_cap_binds_elsewhere: PASS")


def test_allocate_capital_empty_returns_empty_dict():
    assert allocate_capital(1000.0, [], min_fraction=0.15, max_fraction=0.60) == {}
    print("test_allocate_capital_empty_returns_empty_dict: PASS")


def test_market_runtime_state_defaults_max_inventory_and_accepts_override():
    default_state = MarketRuntimeState("TEST", target_size=100.0, discount_factor=0.9)
    assert default_state.max_inventory == config.MAX_SIMULATED_INVENTORY

    overridden = MarketRuntimeState("TEST", target_size=100.0, discount_factor=0.9, max_inventory=250)
    assert overridden.max_inventory == 250
    print("test_market_runtime_state_defaults_max_inventory_and_accepts_override: PASS")


def test_reward_density_does_not_inflate_as_a_program_nears_expiry():
    """
    The 2026-10-05 selection bug. Density was reward_per_day computed as
    period_reward / days_remaining -- the WHOLE pool over the time LEFT --
    so a market's apparent attractiveness grew as it approached expiry
    without the pool growing at all. That is backwards twice: a nearly
    expired market has little left to win AND is about to go dead.
    """
    start, end = "2026-10-01T00:00:00+00:00", "2026-10-08T00:00:00+00:00"  # 7-day program
    pool, target = 40000.0, 1000.0

    density = _reward_density(pool, target, start, end)
    assert abs(density - (pool / 7.0) / target) < 1e-9, density

    # identical regardless of how much time is left -- that is the point
    assert _reward_density(pool, target, start, end) == density

    # the old formula with 0.97 days left scored 7.2x higher
    old_formula = (pool / 0.972) / target
    assert old_formula > density * 7.0, (old_formula, density)

    # a higher-paying program must rank above a lower-paying one of equal length
    assert _reward_density(60000.0, target, start, end) > density

    # degenerate inputs -> 0.0, never a crash or a divide-by-zero
    assert _reward_density(pool, 0.0, start, end) == 0.0
    assert _reward_density(pool, target, None, None) == 0.0
    assert _reward_density(pool, target, start, start) == 0.0
    print("test_reward_density_does_not_inflate_as_a_program_nears_expiry: PASS")


def test_switch_is_blocked_when_replacement_pays_far_less():
    """
    The crowding gate asked "is the new market less contested?" and nothing
    asked "does it still pay?", so the bot left $864/day/market approval
    markets for $13/day/market NFL markets. 30.3 market-hours cost an
    estimated $1,074 of foregone reward.
    """
    f = config.MIN_REPLACEMENT_REWARD_DENSITY_FRACTION

    # the real case: replacement pays ~1.5% of the incumbent -> blocked
    assert is_reward_preserving_switch(60.0, 0.9, f) is False
    # a modest give-up to escape crowding is still allowed
    assert is_reward_preserving_switch(60.0, 40.0, f) is True
    # exactly at the boundary is allowed
    assert is_reward_preserving_switch(60.0, 60.0 * f, f) is True
    assert is_reward_preserving_switch(60.0, 60.0 * f - 0.01, f) is False
    # a better-paying replacement is obviously fine
    assert is_reward_preserving_switch(10.0, 99.0, f) is True

    # unknown density must NOT freeze switching -- neutral, like _adjusted_score
    assert is_reward_preserving_switch(None, 1.0, f) is True
    assert is_reward_preserving_switch(1.0, None, f) is True
    # nothing to protect if the incumbent pays nothing
    assert is_reward_preserving_switch(0.0, 0.0, f) is True
    print("test_switch_is_blocked_when_replacement_pays_far_less: PASS")


def test_reward_rate_uses_program_duration_not_time_remaining():
    """
    The 2026-10-05 bug. daily_pool was period_reward / days_remaining, which
    divides the WHOLE pool by the time LEFT -- so it grew without bound as a
    program neared its end. Real case: a $60,000 24-hour program with 0.575
    days left reported $104,408/day, a 74% overstatement.
    """
    prog = {
        "period_reward": 60000.0,
        "start_date": "2026-10-04T14:16:40.819871+00:00",
        "end_date": "2026-10-05T14:16:40.819871+00:00",
        "days_remaining": 0.5747,
    }
    rate = _program_reward_per_second(prog)
    assert rate is not None
    assert abs(rate * 86400.0 - 60000.0) < 1.0, rate * 86400.0

    # the old formula, for contrast -- must NOT be what we report
    old = prog["period_reward"] / prog["days_remaining"]
    assert old > 100000.0
    assert abs(rate * 86400.0 - old) > 40000.0

    # malformed / missing inputs degrade to None rather than raising
    assert _program_reward_per_second(None) is None
    assert _program_reward_per_second({"period_reward": 1, "start_date": None, "end_date": None}) is None
    assert _program_reward_per_second(
        {"period_reward": 1, "start_date": "nonsense", "end_date": "also nonsense"}
    ) is None
    # zero-length program must not divide by zero
    assert _program_reward_per_second(
        {"period_reward": 1, "start_date": "2026-10-04T00:00:00+00:00",
         "end_date": "2026-10-04T00:00:00+00:00"}
    ) is None
    print("test_reward_rate_uses_program_duration_not_time_remaining: PASS")


def test_quote_sides_are_sized_by_remaining_inventory_room():
    """
    Closes a three-way inconsistency: apply_yes_fill clamped fills at
    max_inventory justifying it as "we'd have pulled the quote", but
    compute_quote never pulled and reward_simulator kept scoring the full
    resting size -- so at the cap we collected incentive credit for
    liquidity we had already assumed was withdrawn.
    """
    book = MarketBook(ticker="TEST")
    book.apply_snapshot(sid=1, seq=1, yes=[(40, 5000.0)], no=[(59, 5000.0)])

    flat = InventoryState(ticker="TEST")
    q = quoting_policy.compute_quote(book, LOW, flat, 100, 300, 1000.0, minutes_until_close=600)
    assert q.yes_size == 100 and q.no_size == 100, (q.yes_size, q.no_size)

    # long yes at the cap -> no room to buy more yes, that side is pulled
    maxed_long = InventoryState(ticker="TEST", net_yes_contracts=300, avg_entry_price=40.0)
    q = quoting_policy.compute_quote(book, LOW, maxed_long, 100, 300, 1000.0, minutes_until_close=600)
    assert q.yes_size == 0, q.yes_size
    assert q.no_size == 100, q.no_size      # still room to sell yes

    # partially full -> sized to the room that is actually left
    partial = InventoryState(ticker="TEST", net_yes_contracts=250, avg_entry_price=40.0)
    q = quoting_policy.compute_quote(book, LOW, partial, 100, 300, 1000.0, minutes_until_close=600)
    assert q.yes_size == 50, q.yes_size

    # short yes at the cap -> the NO side is the one with no room
    maxed_short = InventoryState(ticker="TEST", net_yes_contracts=-300, avg_entry_price=40.0)
    q = quoting_policy.compute_quote(book, LOW, maxed_short, 100, 300, 1000.0, minutes_until_close=600)
    assert q.no_size == 0 and q.yes_size == 100, (q.yes_size, q.no_size)
    print("test_quote_sides_are_sized_by_remaining_inventory_room: PASS")


def test_settlement_books_long_yes_at_resolution_not_last_mid():
    """A long YES resolves at 100 if YES wins, 0 if it loses."""
    inv = InventoryState(ticker="TEST")
    inv.apply_yes_fill(price=40, size=100, max_inventory=1000)
    assert inv.net_yes_contracts == 100 and inv.avg_entry_price == 40.0

    delta = inv.settle(result_yes=True)
    assert delta == 100 * (100 - 40), delta           # +6000c = +$60
    assert inv.realized_pnl_cents == 6000.0
    assert inv.net_yes_contracts == 0
    assert inv.mark_to_market_pnl_cents(50.0) == 0.0  # nothing left to mark

    losing = InventoryState(ticker="TEST2")
    losing.apply_yes_fill(price=40, size=100, max_inventory=1000)
    assert losing.settle(result_yes=False) == 100 * (0 - 40)   # -4000c = -$40
    print("test_settlement_books_long_yes_at_resolution_not_last_mid: PASS")


def test_settlement_books_short_yes_and_is_idempotent():
    """A short-yes (long NO) position resolves the other way round."""
    inv = InventoryState(ticker="TEST")
    inv.apply_no_fill(no_price=70, size=50, max_inventory=1000)  # equiv short yes @ 30
    assert inv.net_yes_contracts == -50 and inv.avg_entry_price == 30.0

    # YES loses -> our short wins: (-net) * (entry - 0)
    delta = inv.settle(result_yes=False)
    assert delta == 50 * (30 - 0), delta
    assert inv.net_yes_contracts == 0

    # settling again must change nothing
    before = inv.realized_pnl_cents
    assert inv.settle(result_yes=False) == 0.0
    assert inv.realized_pnl_cents == before
    print("test_settlement_books_short_yes_and_is_idempotent: PASS")


def test_settlement_replaces_the_frozen_unrealized_mark():
    """
    The 2026-10-04 case. 145 contracts long at ~1.8c were left marked at
    -$11.88 UNREALIZED when the market settled, because leaving a dead
    market froze the last mid instead of resolving. Settlement must convert
    that into a realized fact in one direction or the other, and leave
    nothing unrealized either way.
    """
    for result_yes in (True, False):
        inv = InventoryState(ticker="KXTRUMPAPPROVE-26OCT04-E38.1")
        inv.apply_yes_fill(price=2, size=145, max_inventory=1000)
        stale_mid = 1.0
        assert inv.mark_to_market_pnl_cents(stale_mid) != 0.0   # was being frozen

        inv.settle(result_yes=result_yes)
        assert inv.net_yes_contracts == 0
        assert inv.mark_to_market_pnl_cents(stale_mid) == 0.0   # nothing frozen now
        expected = 145 * ((100 if result_yes else 0) - 2)
        assert inv.realized_pnl_cents == expected, (result_yes, inv.realized_pnl_cents)
    print("test_settlement_replaces_the_frozen_unrealized_mark: PASS")


def test_dead_market_detection_requires_sustained_empty_book():
    """
    A settled market's book goes empty and stays empty. A live one refills
    within seconds, so deadness must require DURATION, not a single empty
    tick -- otherwise a momentary gap would evict a healthy market.
    """
    st = MarketRuntimeState("TEST", target_size=100.0, discount_factor=0.9)
    now = time.time()

    # never seen empty
    assert st.empty_book_since is None
    assert st.is_dead(now) is False

    # just went empty -> not dead yet
    st.empty_book_since = now
    assert st.is_dead(now) is False
    assert st.is_dead(now + config.DEAD_MARKET_EMPTY_BOOK_SECONDS - 1) is False

    # empty past the threshold -> dead
    assert st.is_dead(now + config.DEAD_MARKET_EMPTY_BOOK_SECONDS) is True
    assert st.is_dead(now + config.DEAD_MARKET_EMPTY_BOOK_SECONDS + 3600) is True

    # liquidity returned -> no longer a candidate
    st.empty_book_since = None
    assert st.is_dead(now + 86400) is False
    print("test_dead_market_detection_requires_sustained_empty_book: PASS")


def test_empty_book_reads_as_uncrowded_which_is_why_liveness_is_separate():
    """
    THE 2026-10-04 TRAP, pinned as a test. compute_crowding_ratio on an
    EMPTY book reports LOWER (better) crowding than on a busy one, because
    crowding is competing-size-based. So every quality signal improves as a
    market dies, which is exactly why is_dead() cannot be derived from them.
    """
    busy = MarketBook(ticker="BUSY")
    busy.apply_snapshot(sid=1, seq=1, yes=[(40, 5000.0)], no=[(59, 5000.0)])

    dead = MarketBook(ticker="DEAD")
    dead.apply_snapshot(sid=1, seq=1, yes=[], no=[])

    busy_crowding = compute_crowding_ratio(busy, target_size=100.0)
    dead_crowding = compute_crowding_ratio(dead, target_size=100.0)

    assert dead_crowding <= busy_crowding, (dead_crowding, busy_crowding)
    # and the dead book cannot even produce a mid
    assert dead.best_yes_bid() is None and dead.best_no_bid() is None
    print("test_empty_book_reads_as_uncrowded_which_is_why_liveness_is_separate: PASS")


def test_risk_engine_exposes_sub_level_breakdown():
    engine = RiskEngine(ticker="TEST")
    inventory = InventoryState(ticker="TEST", net_yes_contracts=420)  # 420/500 = 0.84 -> HIGH

    engine.update(50.0, inventory, max_inventory=500)
    assert engine.risk_level == HIGH, engine.risk_level
    assert engine.inventory_level == HIGH, engine.inventory_level
    assert engine.volatility_level == LOW, engine.volatility_level  # stable mid so far
    print("test_risk_engine_exposes_sub_level_breakdown: PASS")


def test_risk_reason_attributes_only_the_actual_driving_factor():
    # ELEVATED purely from a news hit -- volatility/inventory both LOW.
    assert _risk_reason("ELEVATED", "LOW", "LOW", True, False) == "Elevated due to: recent news"

    # HIGH from volatility alone -- an ELEVATED inventory_level must NOT
    # be listed as a cause, since it isn't independently at HIGH severity
    # (the actual composite-driving factor, per risk_engine.py's max-
    # severity composition).
    assert _risk_reason("HIGH", "HIGH", "ELEVATED", False, False) == "High due to: price volatility"

    # Both volatility AND inventory independently at HIGH -- both listed.
    assert _risk_reason("HIGH", "HIGH", "HIGH", False, False) == "High due to: price volatility, position size"

    # news/event never explain a HIGH reading -- risk_engine.py only lets
    # them nudge to ELEVATED, never straight to HIGH on their own.
    assert _risk_reason("HIGH", "LOW", "LOW", True, True) == "HIGH (sub-signal data unavailable)"

    assert _risk_reason("LOW", "LOW", "LOW", False, False) == "Normal conditions"
    assert _risk_reason(None, None, None, None, None) is None
    print("test_risk_reason_attributes_only_the_actual_driving_factor: PASS")


def test_recency_must_be_measured_from_build_start_not_after_queries():
    """
    The 2026-10-05 dashboard bug. build_dashboard_payload captured its
    `now` AFTER running every query. A build takes 9-25s on the Droplet
    against RECENCY_CUTOFF_SEC = 20s, so on a slow build rows that were
    fresh when READ were judged stale, active_tickers came out empty, and
    the markets table silently fell back to config.WATCHED_MARKET_TICKERS
    -- three unrelated markets rendered as score 0 / pool $0 / risk None.

    This pins the arithmetic that made it possible: identical data, two
    different reference times, opposite verdicts.
    """
    build_start = time.time()
    row = {"ts": build_start - 0.5}          # fresh when the build began

    assert _is_recent(row, build_start) is True

    # what the old code effectively did: judge it after a slow build
    after_slow_build = build_start + server.RECENCY_CUTOFF_SEC + 5.0
    assert _is_recent(row, after_slow_build) is False

    # and a genuinely stale row is still caught from build start, so the
    # fix does not mask a stalled collector
    stale = {"ts": build_start - (server.RECENCY_CUTOFF_SEC + 5.0)}
    assert _is_recent(stale, build_start) is False
    print("test_recency_must_be_measured_from_build_start_not_after_queries: PASS")


def test_is_recent_uses_cutoff():
    now = time.time()
    assert _is_recent({"ts": now - 1.0}, now) is True
    assert _is_recent({"ts": now - 1000.0}, now) is False
    assert _is_recent(None, now) is False
    print("test_is_recent_uses_cutoff: PASS")


def test_capital_allocation_drops_stale_tickers():
    # Regression test for a live incident (2026-09-02): a ticker
    # market_switcher.py had just switched AWAY from was still showing up
    # in the allocation panel with its old allocation (345s stale by the
    # time it was observed), duplicating the dollar figure of whichever
    # ticker it had been switched away from/into. "Latest per ticker"
    # alone means "latest EVER" -- must also be recent.
    now = time.time()
    inventory = {
        "FRESH": {"ts": now - 0.5, "max_inventory": 400},
        "STALE": {"ts": now - 345.0, "max_inventory": 562},
    }
    allocation = _capital_allocation(None, inventory, now)
    tickers = {a["ticker"] for a in allocation}
    assert tickers == {"FRESH"}, tickers
    assert allocation[0]["pct"] == 100.0
    print("test_capital_allocation_drops_stale_tickers: PASS")


def test_performance_history_merges_per_ticker_rows_into_one_point_per_bucket():
    # Regression test for a live incident (2026-09-02): pnl_dollars logged
    # one point per (ticker, bucket) row -- with 3 tickers, ~3x as many
    # points as reward_score's one-per-bucket. Chart.js plotted the two
    # series against a shared x-axis sized to the LONGER series, so the
    # shorter (reward_score) line visibly stopped partway across the
    # chart instead of reaching "now", even though its data was current.
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)

    bucket = config.PERFORMANCE_HISTORY_BUCKET_SECONDS
    base = time.time() - 3 * bucket

    for k in range(3):  # 3 distinct buckets
        ts = base + k * bucket + 1.0
        conn.execute(
            "INSERT INTO snapshot_scores (ts, ticker, combined_score) VALUES (?, 'ANY', ?)",
            (ts, 1.0),
        )
        for ticker in ("A", "B"):  # 2 tickers per bucket -> 6 rows total
            conn.execute(
                "INSERT INTO inventory_marks (ts, ticker, unrealized_pnl_cents, realized_pnl_cents) "
                "VALUES (?, ?, 0, 0)",
                (ts, ticker),
            )
    conn.commit()

    history = _performance_history(conn)
    assert len(history["reward_score"]) == 3, history["reward_score"]
    assert len(history["pnl_dollars"]) == 3, history["pnl_dollars"]  # NOT 6
    assert len(history["balance_dollars"]) == 3, history["balance_dollars"]
    conn.close()
    print("test_performance_history_merges_per_ticker_rows_into_one_point_per_bucket: PASS")


def test_derive_keywords_ranks_capitalized_proper_nouns_first():
    # Real title observed live 2026-09-02 (KXMAMDANIEO-26SEP05-T0). The
    # old length-only ranking put "executive" (9 chars, generic) ahead
    # of "mamdani" (7 chars, the market's actual distinctive subject) --
    # capitalization is the fix, not length.
    keywords = derive_keywords("Will Mamdani sign above 0 executive orders between Aug 30, 2026 and Sep 5, 2026?", "")
    assert keywords[0] == "mamdani", keywords
    assert set(keywords) == {"mamdani", "executive", "orders", "sign"}, keywords
    print("test_derive_keywords_ranks_capitalized_proper_nouns_first: PASS")


def test_is_relevant_requires_distinctive_keyword_not_just_ratio():
    # Same real keyword list (mamdani first after the fix above).
    keywords = ["mamdani", "executive", "orders", "sign"]

    # Corroborated by a second keyword ("sign") -> relevant.
    true_positive = {"headline": "Mamdani to sign executive order on AI in schools", "theme_tags": []}
    assert is_relevant(keywords, true_positive, threshold=0.2) is True

    # Regression coverage for the live flood (2026-09-02): these each
    # clear the ratio threshold (1/4 = 0.25 >= 0.2) on a generic-word
    # hit alone, with zero connection to the actual market -- must be
    # rejected now that the distinctive-keyword gate is required.
    false_positive_1 = {"headline": "New Orleans Saints sign Kevin Austin Jr. to practice squad", "theme_tags": []}
    false_positive_2 = {"headline": "Justice department orders US states to expand reporting", "theme_tags": []}
    assert is_relevant(keywords, false_positive_1, threshold=0.2) is False
    assert is_relevant(keywords, false_positive_2, threshold=0.2) is False

    # Mentions the distinctive keyword ALONE, no corroborating keyword --
    # deliberately rejected now too (see is_relevant's docstring, part
    # (b)/tradeoff note: a second live incident, 2026-09-29, needed a
    # second corroborating keyword requirement, not just the distinctive
    # one, which costs exactly this kind of borderline true positive).
    uncorroborated = {"headline": "Mamdani imposes one-year ban on AI for most NYC students", "theme_tags": []}
    assert is_relevant(keywords, uncorroborated, threshold=0.2) is False

    assert is_relevant([], true_positive, threshold=0.2) is False
    print("test_is_relevant_requires_distinctive_keyword_not_just_ratio: PASS")


def test_is_relevant_requires_second_keyword_for_ubiquitous_distinctive_term():
    # Real incident, 2026-09-29: KXTRUMPAPPROVE-* markets all derived
    # keywords=['trump', 'approval', 'average', 'exactly'] -- "trump" is
    # the sole capitalized word, but as a sitting president's name it
    # matches roughly 100% of current political news regardless of
    # topic, flooding news_events within the first 90s of a live
    # deployment. Requiring a SECOND keyword fixes this generically
    # (no hardcoded per-entity stoplist).
    keywords = ["trump", "approval", "average", "exactly"]

    # "trump" alone, totally unrelated to his approval rating -> rejected.
    unrelated = {"headline": "Trump, AI CEOs sign voluntary safety pact, back data center expansion", "theme_tags": []}
    assert is_relevant(keywords, unrelated, threshold=0.2) is False

    # "trump" + "approval" -> genuinely on-topic, still accepted.
    on_topic = {"headline": "Trump's approval rating drops to new low in latest poll", "theme_tags": []}
    assert is_relevant(keywords, on_topic, threshold=0.2) is True

    # A market that derives only ONE keyword (nothing to corroborate
    # with) must NOT be penalized -- falls back to the single-keyword
    # bar, same as before this fix.
    assert is_relevant(["mamdani"], {"headline": "Mamdani speaks at rally", "theme_tags": []}, threshold=0.2) is True
    print("test_is_relevant_requires_second_keyword_for_ubiquitous_distinctive_term: PASS")


def test_build_news_query_ors_keywords():
    assert _build_news_query(["hurricane", "florida", "landfall"]) == "hurricane OR florida OR landfall"
    print("test_build_news_query_ors_keywords: PASS")


def test_parse_articles_filters_by_lookback_keeps_undated():
    # Regression coverage for the 2026-09-02 GDELT -> Google News RSS
    # swap: real <item> elements, a stale one that must be filtered by
    # pubDate, and one with no pubDate at all (kept, not dropped -- a
    # missing date is a parsing edge case, not evidence of staleness).
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(hours=1)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    stale = (now - timedelta(hours=100)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    xml = f"""<?xml version="1.0"?>
<rss><channel>
<item><title>Recent article</title><link>https://example.com/a</link><pubDate>{recent}</pubDate></item>
<item><title>Stale article</title><link>https://example.com/b</link><pubDate>{stale}</pubDate></item>
<item><title>No date article</title><link>https://example.com/c</link></item>
</channel></rss>"""
    parsed = _parse_articles(xml, lookback_hours=6, max_records=20)
    headlines = {a["headline"] for a in parsed}
    assert headlines == {"Recent article", "No date article"}, headlines
    assert all(a["theme_tags"] == [] for a in parsed)
    print("test_parse_articles_filters_by_lookback_keeps_undated: PASS")


def test_parse_articles_respects_max_records():
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(hours=1)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    items = "".join(
        f"<item><title>A{i}</title><link>https://example.com/{i}</link><pubDate>{recent}</pubDate></item>"
        for i in range(5)
    )
    xml = f"<rss><channel>{items}</channel></rss>"
    parsed = _parse_articles(xml, lookback_hours=6, max_records=3)
    assert len(parsed) == 3, parsed
    print("test_parse_articles_respects_max_records: PASS")


def test_parse_articles_handles_malformed_xml_gracefully():
    assert _parse_articles("not xml at all <<<", lookback_hours=6, max_records=20) == []
    print("test_parse_articles_handles_malformed_xml_gracefully: PASS")


def test_market_switcher_backs_off_after_failed_search_not_just_successful_switch():
    """
    Regression test for a live incident (2026-09-02): both watched
    markets were far more crowded than any replacement candidate, so
    find_replacement kept returning None, and a sustained-triggered
    ticker with no qualifying replacement re-ran the full search
    (screen_incentive_programs + up to N live get_orderbook calls) every
    check interval indefinitely -- because previously only a SUCCESSFUL
    switch reset the cooldown. record_search_attempt() must throttle
    retries the same way, called on every attempt whether or not it
    succeeds (see main.py's market_switch_loop).
    """
    switcher = MarketSwitcher()
    t0 = 1_000_000.0

    assert switcher.check_trigger("A", HIGH, None, now=t0) is None
    trigger = switcher.check_trigger("A", HIGH, None, now=t0 + config.SWITCH_TRIGGER_SUSTAIN_SEC + 1)
    assert trigger == "volatile", trigger

    # Simulate find_replacement returning None -- no successful switch,
    # but the search attempt itself must still start the cooldown.
    switcher.record_search_attempt(now=t0 + config.SWITCH_TRIGGER_SUSTAIN_SEC + 1)

    # Immediately after, even though the condition still holds, retrying
    # must be blocked by the cooldown -- NOT fire again right away.
    assert switcher.check_trigger("A", HIGH, None, now=t0 + config.SWITCH_TRIGGER_SUSTAIN_SEC + 2) is None
    print("test_market_switcher_backs_off_after_failed_search_not_just_successful_switch: PASS")


if __name__ == "__main__":
    test_orderbook_snapshot_and_delta()
    test_orderbook_delta_to_zero_removes_level()
    test_orderbook_float_residue_does_not_leave_phantom_level()
    test_orderbook_preserves_legitimately_fractional_sizes()
    test_orderbook_snapshot_filters_zero_levels()
    test_orderbook_delta_unrecognized_shape_marks_stale_not_crash()
    test_orderbook_seq_is_shared_across_tickers_under_one_sid()
    test_reference_price()
    test_score_hypothetical_order_qualifies_and_shares_correctly()
    test_score_hypothetical_order_fails_target()
    test_inventory_yes_fill_and_pnl()
    test_inventory_respects_max_position()
    test_process_trade_for_inventory_end_to_end()
    test_build_market_states_uses_real_incentive_program_values()
    test_compute_my_share_at_touched_price_level()
    test_compute_quote_skews_toward_flattening_long_inventory()
    test_compute_quote_rests_at_reference_price_not_touch_when_they_differ()
    test_risk_engine_escalates_to_high_on_volatile_mid()
    test_maybe_execute_stop_loss_flattens_at_hard_stop_and_not_otherwise()
    test_maybe_execute_stop_loss_refined_threshold_protects_small_allocations()
    test_maybe_execute_time_based_exit_fires_below_critical_and_not_above()
    test_effective_take_profit_margin_decays_with_time_and_hits_zero_at_critical()
    test_compute_quote_applies_take_profit_floor_on_exit_side()
    test_classify_fill_exit_reason()
    test_score_relevance_keyword_overlap()
    test_risk_engine_news_signal_escalates_then_decays()
    test_compute_crowding_ratio()
    test_market_switcher_trigger_requires_sustain_and_cooldown()
    test_is_worthwhile_crowding_switch_requires_meaningful_improvement()
    test_hours_until_close()
    test_risk_engine_event_proximity_escalates_within_window()
    test_estimate_crowding_and_volatility_from_fake_client()
    test_adjusted_score_dampens_for_crowding_and_volatility()
    test_market_switcher_backs_off_after_failed_search_not_just_successful_switch()
    test_select_watchlist_picks_best_available_even_when_all_crowded()
    test_select_watchlist_returns_fewer_than_requested_if_not_enough_programs_exist()
    test_select_initial_watchlist_falls_back_only_when_no_active_programs_exist()
    test_allocate_capital_weights_by_profit_risk_ratio()
    test_allocate_capital_falls_back_to_reward_density_when_adjusted_score_missing()
    test_allocate_capital_clips_to_min_and_max_fraction()
    test_allocate_capital_min_floor_holds_even_when_max_cap_binds_elsewhere()
    test_allocate_capital_empty_returns_empty_dict()
    test_market_runtime_state_defaults_max_inventory_and_accepts_override()
    test_build_news_query_ors_keywords()
    test_parse_articles_filters_by_lookback_keeps_undated()
    test_parse_articles_respects_max_records()
    test_parse_articles_handles_malformed_xml_gracefully()
    test_derive_keywords_ranks_capitalized_proper_nouns_first()
    test_is_relevant_requires_distinctive_keyword_not_just_ratio()
    test_is_relevant_requires_second_keyword_for_ubiquitous_distinctive_term()
    test_reward_density_does_not_inflate_as_a_program_nears_expiry()
    test_switch_is_blocked_when_replacement_pays_far_less()
    test_reward_rate_uses_program_duration_not_time_remaining()
    test_quote_sides_are_sized_by_remaining_inventory_room()
    test_settlement_books_long_yes_at_resolution_not_last_mid()
    test_settlement_books_short_yes_and_is_idempotent()
    test_settlement_replaces_the_frozen_unrealized_mark()
    test_dead_market_detection_requires_sustained_empty_book()
    test_empty_book_reads_as_uncrowded_which_is_why_liveness_is_separate()
    test_risk_engine_exposes_sub_level_breakdown()
    test_risk_reason_attributes_only_the_actual_driving_factor()
    test_recency_must_be_measured_from_build_start_not_after_queries()
    test_is_recent_uses_cutoff()
    test_capital_allocation_drops_stale_tickers()
    test_performance_history_merges_per_ticker_rows_into_one_point_per_bucket()
    print("\nALL TESTS PASSED")
