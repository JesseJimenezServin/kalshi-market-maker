"""
Sanity tests for the pieces that don't require network access: order book
reconstruction, the reward-scoring formula, and the inventory simulator.
Run with: python3 test_logic.py
"""

from datetime import datetime, timezone

from orderbook import OrderbookManager, MarketBook
from reward_simulator import compute_reference_price, score_hypothetical_order, score_both_sides
from adverse_selection_sim import InventoryState, HypotheticalQuote, process_trade_for_inventory
from incentive_screener import ScreenedProgram
from main import build_market_states, compute_my_share_at_touched_price
from risk_engine import RiskEngine, LOW, HIGH
from quoting_policy import compute_quote, maybe_execute_stop_loss


def test_orderbook_snapshot_and_delta():
    mgr = OrderbookManager()
    mgr.handle_message({
        "type": "orderbook_snapshot",
        "msg": {"market_ticker": "TEST", "sid": 1, "seq": 100,
                 "yes": [[45, 200], [44, 150]], "no": [[40, 300], [39, 100]]},
    })
    book = mgr.books["TEST"]
    assert not book.stale
    assert book.best_yes_bid() == (45, 200)
    assert book.best_no_bid() == (40, 300)

    # clean delta
    mgr.handle_message({
        "type": "orderbook_delta",
        "msg": {"market_ticker": "TEST", "sid": 1, "seq": 101, "side": "yes", "price": 45, "delta": 50},
    })
    assert book.yes_levels[45] == 250
    assert not book.stale

    # gap -> should mark stale
    mgr.handle_message({
        "type": "orderbook_delta",
        "msg": {"market_ticker": "TEST", "sid": 1, "seq": 105, "side": "yes", "price": 45, "delta": 10},
    })
    assert book.stale, "expected sequence gap to mark book stale"
    print("test_orderbook_snapshot_and_delta: PASS")


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
    program = ScreenedProgram(
        market_ticker="FOUND-TICKER",
        period_reward=500.0,
        target_size=750.0,
        discount_factor_bps=8500,
        end_date=datetime.now(timezone.utc),
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

    flat_quote = compute_quote(book, LOW, flat_inventory, base_size=100, max_inventory=500)
    long_quote = compute_quote(book, LOW, long_inventory, base_size=100, max_inventory=500)

    assert flat_quote is not None and long_quote is not None
    # Long yes should skew the quote to encourage flattening: worse (lower)
    # yes bid, better (higher) no bid.
    assert long_quote.yes_price <= flat_quote.yes_price
    assert long_quote.no_price >= flat_quote.no_price
    assert (long_quote.yes_price, long_quote.no_price) != (flat_quote.yes_price, flat_quote.no_price)
    print("test_compute_quote_skews_toward_flattening_long_inventory: PASS")


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


if __name__ == "__main__":
    test_orderbook_snapshot_and_delta()
    test_reference_price()
    test_score_hypothetical_order_qualifies_and_shares_correctly()
    test_score_hypothetical_order_fails_target()
    test_inventory_yes_fill_and_pnl()
    test_inventory_respects_max_position()
    test_process_trade_for_inventory_end_to_end()
    test_build_market_states_uses_real_incentive_program_values()
    test_compute_my_share_at_touched_price_level()
    test_compute_quote_skews_toward_flattening_long_inventory()
    test_risk_engine_escalates_to_high_on_volatile_mid()
    test_maybe_execute_stop_loss_flattens_at_hard_stop_and_not_otherwise()
    print("\nALL TESTS PASSED")
