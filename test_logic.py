"""
Sanity tests for the pieces that don't require network access: order book
reconstruction, the reward-scoring formula, and the inventory simulator.
Run with: python3 test_logic.py
"""

from datetime import datetime, timedelta, timezone

from orderbook import OrderbookManager, MarketBook
from reward_simulator import compute_reference_price, score_hypothetical_order, score_both_sides
from adverse_selection_sim import InventoryState, HypotheticalQuote, process_trade_for_inventory
from incentive_screener import ScreenedProgram
from main import build_market_states, compute_my_share_at_touched_price
from risk_engine import RiskEngine, LOW, ELEVATED, HIGH
from quoting_policy import compute_quote, maybe_execute_stop_loss
from news_feed import score_relevance
from market_switcher import MarketSwitcher, compute_crowding_ratio
from event_calendar import hours_until_close
from incentive_screener import _adjusted_score, _estimate_crowding, _estimate_recent_volatility
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


def test_score_relevance_keyword_overlap():
    keywords = ["hurricane", "florida", "landfall", "category"]

    # Synthetic GDELT-ArtList-shaped article: title overlaps 3/4 keywords.
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
    test_score_relevance_keyword_overlap()
    test_risk_engine_news_signal_escalates_then_decays()
    test_compute_crowding_ratio()
    test_market_switcher_trigger_requires_sustain_and_cooldown()
    test_hours_until_close()
    test_risk_engine_event_proximity_escalates_within_window()
    test_estimate_crowding_and_volatility_from_fake_client()
    test_adjusted_score_dampens_for_crowding_and_volatility()
    test_market_switcher_backs_off_after_failed_search_not_just_successful_switch()
    print("\nALL TESTS PASSED")
