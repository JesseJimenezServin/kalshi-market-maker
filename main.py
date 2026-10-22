"""
Phase 0 entry point.

Runs entirely read-only against Kalshi market data:
  1. Periodically screens incentive_programs for live liquidity pools.
  2. Streams the live order book + trade tape for the watched markets.
  3. Every ~1 second, computes what our hypothetical reward SHARE would be
     if we rested a configured quote size at a configured price on each
     side (reward_simulator).
  4. On every real trade print, updates a hypothetical inventory position
     to see what adverse selection WOULD have cost us (adverse_selection_sim).
  5. Logs everything to SQLite for later analysis.

NO ORDERS ARE EVER PLACED. This script only reads public/authenticated
market data.

Run with:
    export KALSHI_ENV=demo
    export KALSHI_API_KEY_ID=...
    export KALSHI_PRIVATE_KEY_PATH=/path/to/key.pem
    python main.py
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

# Configured before the imports below so that any import-time log lines
# (e.g. rest_client's read-only-mode startup notice) are actually emitted
# with the intended format, instead of being silently dropped by Python's
# default no-handlers-configured logging behavior.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

import config
from auth import KalshiSigner
from rest_client import KalshiRestClient
from orderbook import OrderbookManager, dollars_to_cents, fp_to_size
from ws_client import KalshiWebSocketClient
from incentive_screener import ScreenedProgram, screen_incentive_programs, select_watchlist, print_report
from capital_allocator import allocate_capital
from reward_simulator import score_both_sides
from adverse_selection_sim import HypotheticalQuote, InventoryState, process_trade_for_inventory
from logger import SimulationLogger
import quoting_policy
from risk_engine import RiskEngine
from news_feed import news_refresh_loop
from event_calendar import event_refresh_loop, hours_until_close
from singleton_lock import acquire_singleton_lock
from market_switcher import (
    MarketSwitcher, compute_crowding_ratio, find_replacement,
    is_worthwhile_crowding_switch, is_reward_preserving_switch,
)

logger = logging.getLogger("main")


class MarketRuntimeState:
    """Per-market hypothetical quote + inventory + program parameters."""

    def __init__(
        self,
        ticker: str,
        target_size: float,
        discount_factor: float,
        max_inventory: int | None = None,
        reward_density_score: float | None = None,
    ):
        self.ticker = ticker
        self.target_size = target_size
        self.discount_factor = discount_factor
        # First-pass reward-per-day-per-target-size for this market, carried
        # so market_switch_loop can compare what a replacement pays against
        # what we would be giving up. Without it the crowding gate could
        # only reason about competition, never about revenue.
        self.reward_density_score = reward_density_score
        # Per-market position cap. Defaults to the flat fallback constant;
        # run() overwrites this for every state right after construction
        # via capital_allocator.allocate_capital's profit/risk-weighted
        # split of config.STARTING_CAPITAL_USD. A market switched into
        # mid-run (see main.py's market_switch_loop) instead inherits the
        # vacated market's allocation directly, rather than triggering a
        # full re-split across everyone else.
        self.max_inventory = max_inventory if max_inventory is not None else config.MAX_SIMULATED_INVENTORY
        self.inventory = InventoryState(ticker=ticker)
        self.risk_engine = RiskEngine(ticker=ticker)
        # Set by scoring_loop to the time BOTH sides of the book first went
        # empty, cleared as soon as any level returns. market_switch_loop
        # reads it to detect a settled market -- see
        # config.DEAD_MARKET_EMPTY_BOOK_SECONDS.
        self.empty_book_since: float | None = None

    def is_dead(self, now: float) -> bool:
        """Both sides of the book empty for longer than the threshold."""
        return (
            self.empty_book_since is not None
            and (now - self.empty_book_since) >= config.DEAD_MARKET_EMPTY_BOOK_SECONDS
        )
        self.quote: HypotheticalQuote | None = None  # set once we have book data
        # Cached by event_calendar.event_refresh_loop; static once fetched,
        # so scoring_loop can compute minutes_until_close fresh every tick
        # (for take-profit time-decay / the pre-resolution forced exit --
        # see quoting_policy.py) without an extra network call per tick.
        self.close_time: datetime | None = None


def build_market_states(
    watched_tickers: list[str],
    screened_programs: list[ScreenedProgram],
) -> dict[str, MarketRuntimeState]:
    """
    Looks up each watched ticker in the live incentive-screener results and
    initializes MarketRuntimeState with that market's REAL target_size and
    discount_factor (converted from bps to a 0-1 decimal). Watched tickers
    with no active incentive program are logged and skipped -- there's no
    live program to simulate scoring against.
    """
    screened_by_ticker = {p.market_ticker: p for p in screened_programs}

    states: dict[str, MarketRuntimeState] = {}
    for ticker in watched_tickers:
        program = screened_by_ticker.get(ticker)
        if program is None:
            logger.warning(
                "%s: no active incentive program found -- skipping (won't simulate scoring for this market).",
                ticker,
            )
            continue
        states[ticker] = MarketRuntimeState(
            ticker,
            target_size=program.target_size,
            discount_factor=program.discount_factor_bps / 10000.0,
            reward_density_score=program.reward_density_score,
        )
    return states


def fetch_market_metadata(rest_client: KalshiRestClient, ticker: str) -> tuple[str | None, str | None]:
    """
    Fetches `ticker`'s real title/subtitle via the existing GET-only
    rest_client.get_market -- so the dashboard can show a human-readable
    market name instead of just the raw ticker. Title/subtitle are
    effectively static once a market exists, so this is only called
    once per ticker (when it starts being watched -- see run() and
    market_switch_loop below), not on a recurring poll. Returns
    (None, None) (logging a warning, never raising) on any failure.
    """
    try:
        response = rest_client.get_market(ticker)
    except Exception:
        logger.warning("Failed to fetch market metadata for %s", ticker, exc_info=True)
        return None, None

    market = response.get("market", response)
    return market.get("title"), market.get("subtitle")


def compute_my_share_at_touched_price(
    book,
    quote: HypotheticalQuote,
    trade_yes_price: int,
    taker_side: str,
) -> float:
    """
    Estimates our hypothetical share of the exact price level a real trade
    touched, using the same "merge my size into the existing resting size"
    pattern as reward_simulator.score_hypothetical_order.

    taker_side == "no": the aggressor sold yes, touching the YES side at
    trade_yes_price. taker_side == "yes": the aggressor bought yes, touching
    the NO side at (100 - trade_yes_price).
    """
    if taker_side == "no":
        existing_resting_size = book.yes_levels.get(trade_yes_price, 0)
        my_quote_size = quote.yes_size
    else:
        existing_resting_size = book.no_levels.get(100 - trade_yes_price, 0)
        my_quote_size = quote.no_size

    denominator = existing_resting_size + my_quote_size
    if denominator <= 0:
        return 0.0
    return my_quote_size / denominator


async def scoring_loop(
    orderbook_manager: OrderbookManager,
    states: dict[str, MarketRuntimeState],
    sim_logger: SimulationLogger,
    interval_sec: float = 1.0,
) -> None:
    """Mirrors Kalshi's ~once-per-second snapshot cadence."""
    while True:
        for ticker, state in states.items():
            try:
                book = orderbook_manager.books.get(ticker)
                if book is None or book.stale:
                    continue

                # Mid price is needed both by the risk engine (before the quote
                # is computed) and by the inventory mark-to-market below, so
                # compute it once, up front, and reuse it. If either side of the
                # book is empty we can't compute a mid at all this tick.
                best_yes = book.best_yes_bid()
                best_no = book.best_no_bid()

                if best_yes is None and best_no is None:
                    # BOTH sides empty. A live market refills within seconds;
                    # a settled one never does. Record when this started so
                    # market_switch_loop can force an exit, and do NOT log the
                    # snapshot -- on 2026-10-04 three settled markets wrote
                    # 41856 empty rows in 4.4h (90% of all writes, ~229k/day)
                    # purely because this was logged before the emptiness
                    # check.
                    if state.empty_book_since is None:
                        state.empty_book_since = time.time()
                        logger.warning(
                            "%s: both sides of the book are empty -- market may have settled; "
                            "will force a switch after %.0fs",
                            ticker, config.DEAD_MARKET_EMPTY_BOOK_SECONDS,
                        )
                    continue

                state.empty_book_since = None

                sim_logger.log_orderbook_snapshot(
                    ticker=ticker,
                    yes_levels=book.yes_side_sorted_desc(),
                    no_levels=book.no_side_sorted_desc(),
                )

                if best_yes is None or best_no is None:
                    continue
                mid = (best_yes[0] + (100 - best_no[0])) / 2.0

                state.risk_engine.update(mid, state.inventory, state.max_inventory)

                # Computed fresh every tick from the cached close_time (see
                # event_calendar.event_refresh_loop) -- no extra network call,
                # since close_time itself is static once fetched.
                minutes_until_close = (
                    hours_until_close(state.close_time) * 60.0 if state.close_time is not None else None
                )

                state.quote = quoting_policy.compute_quote(
                    book,
                    state.risk_engine.risk_level,
                    state.inventory,
                    config.DEFAULT_SIMULATED_QUOTE_SIZE,
                    state.max_inventory,
                    state.target_size,
                    minutes_until_close=minutes_until_close,
                )

                # Pure instrumentation for the dashboard (server.py) -- main.py
                # and server.py are separate processes with no shared memory,
                # so this is the only way the dashboard can see the current
                # quote/risk level (and, below, WHY: the individual inputs
                # that composed into it, plus our "competition level" proxy).
                # Does not affect any risk/quoting decision.
                sim_logger.log_quote_snapshot(
                    ticker=ticker,
                    risk_level=state.risk_engine.risk_level,
                    quote_yes_price=state.quote.yes_price if state.quote else None,
                    quote_yes_size=state.quote.yes_size if state.quote else None,
                    quote_no_price=state.quote.no_price if state.quote else None,
                    quote_no_size=state.quote.no_size if state.quote else None,
                    volatility_level=state.risk_engine.volatility_level,
                    inventory_level=state.risk_engine.inventory_level,
                    news_signal=state.risk_engine.news_signal,
                    event_signal=state.risk_engine.event_signal,
                    crowding_ratio=compute_crowding_ratio(book, state.target_size),
                    displacement_cents=state.risk_engine.displacement_cents,
                )

                fill = quoting_policy.maybe_execute_stop_loss(
                    book,
                    state.inventory,
                    state.risk_engine.risk_level,
                    state.max_inventory,
                    config.HARD_STOP_INVENTORY_FRACTION,
                    order_size=config.DEFAULT_SIMULATED_QUOTE_SIZE,
                    min_buffer=config.HARD_STOP_MIN_BUFFER_CONTRACTS,
                )
                if fill is not None:
                    sim_logger.log_simulated_stop_loss(ticker=ticker, **fill)
                    logger.warning(
                        "%s: HIGH-risk hard stop fired (%s %d @ %dc, net %d -> %d)",
                        ticker, fill["side"], fill["size"], fill["price"],
                        fill["net_yes_contracts_before"], fill["net_yes_contracts_after"],
                    )
                else:
                    # Independent of risk_level -- only checked when the
                    # hard stop above didn't already flatten us this tick
                    # (flatten_position is a no-op on an already-flat
                    # position anyway, but skipping avoids a redundant call).
                    time_exit_fill = quoting_policy.maybe_execute_time_based_exit(
                        book, state.inventory, minutes_until_close, state.max_inventory, config.CRITICAL_EXIT_MINUTES,
                    )
                    if time_exit_fill is not None:
                        sim_logger.log_simulated_stop_loss(ticker=ticker, **time_exit_fill)
                        logger.warning(
                            "%s: pre-resolution flatten fired (%s %d @ %dc, %.1f min to close, net %d -> %d)",
                            ticker, time_exit_fill["side"], time_exit_fill["size"], time_exit_fill["price"],
                            minutes_until_close,
                            time_exit_fill["net_yes_contracts_before"], time_exit_fill["net_yes_contracts_after"],
                        )

                if state.quote is not None:
                    yes_result, no_result = score_both_sides(
                        book,
                        target_size=state.target_size,
                        discount_factor=state.discount_factor,
                        my_yes_price=state.quote.yes_price,
                        my_yes_size=state.quote.yes_size,
                        my_no_price=state.quote.no_price,
                        my_no_size=state.quote.no_size,
                    )

                    sim_logger.log_snapshot_score(
                        ticker=ticker,
                        qualifies_yes=yes_result.qualifies,
                        qualifies_no=no_result.qualifies,
                        my_share_yes=yes_result.my_share,
                        my_share_no=no_result.my_share,
                        reference_price_yes=yes_result.reference_price,
                        reference_price_no=no_result.reference_price,
                    )

                # Mark inventory to current mid for unrealized P&L tracking.
                # Always logged (even if the quote was pulled or a stop-loss
                # just fired) so inventory_marks stays a continuous timeline.
                unrealized = state.inventory.mark_to_market_pnl_cents(mid)
                sim_logger.log_inventory_mark(
                    ticker=ticker,
                    net_yes_contracts=state.inventory.net_yes_contracts,
                    avg_entry_price=state.inventory.avg_entry_price,
                    mid_price=mid,
                    unrealized_pnl_cents=unrealized,
                    realized_pnl_cents=state.inventory.realized_pnl_cents,
                    realized_passive_cents=state.inventory.realized_passive_cents,
                    realized_taker_cents=state.inventory.realized_taker_cents,
                    realized_settlement_cents=state.inventory.realized_settlement_cents,
                    max_inventory=state.max_inventory,
                )
            except Exception:
                # This is the most critical loop (~1s cadence, all
                # scoring/quoting/inventory logic) -- an unhandled
                # exception here would crash the ENTIRE process
                # (asyncio.gather cancels every other task the moment
                # one raises). OBSERVED LIVE 2026-09-02: exactly this
                # happened in market_switch_loop (a transient
                # ConnectionError), which is why this same guard was
                # added there too. One ticker's failure must never take
                # down the others or the whole run.
                logger.exception("Scoring loop tick failed for %s", ticker)

        await asyncio.sleep(interval_sec)


async def incentive_refresh_loop(
    rest_client: KalshiRestClient,
    states: dict[str, MarketRuntimeState],
    sim_logger: SimulationLogger,
    interval_sec: float,
) -> None:
    """
    `states` (not config.WATCHED_MARKET_TICKERS) is the source of truth
    for which tickers to log program snapshots for -- with
    config.AUTO_SELECT_WATCHLIST on, the actually-watched tickers can
    differ from that static list entirely, and market_switch_loop can
    swap `states`' keys mid-run. Reading states.keys() fresh each
    iteration keeps this in sync with both.
    """
    while True:
        try:
            # screen_incentive_programs makes blocking `requests` calls;
            # run it off the event loop so it doesn't freeze the WebSocket
            # connection / scoring_loop for however long the HTTP call takes.
            results = await asyncio.to_thread(screen_incentive_programs, rest_client)
            logger.info("Incentive screen refreshed: %d active liquidity programs found", len(results))
            print_report(results)
            for program in results:
                if program.market_ticker in states:
                    sim_logger.log_incentive_program_snapshot(
                        ticker=program.market_ticker,
                        period_reward=program.period_reward,
                        target_size=program.target_size,
                        discount_factor_bps=program.discount_factor_bps,
                        days_remaining=program.days_remaining,
                        total_period_days=program.total_period_days,
                        start_date=program.start_date.isoformat(),
                        end_date=program.end_date.isoformat(),
                    )
        except Exception:
            logger.exception("Incentive screen failed")
        await asyncio.sleep(interval_sec)


def _settle_if_resolved(rest_client, ticker: str, state, sim_logger) -> float | None:
    """
    If `ticker` has actually resolved, book the position at 100/0 instead of
    leaving it frozen at its last mid. Returns cents booked, or None if the
    market has not resolved yet (closed-but-unresolved, or the lookup
    failed).

    Blocking `requests` call -- callers must run this off the event loop.
    """
    try:
        market = rest_client.get_market(ticker).get("market", {})
    except Exception:
        logger.exception("%s: could not fetch market to settle -- leaving position as-is", ticker)
        return None

    status = market.get("status")
    result = (market.get("result") or "").lower()
    if result not in ("yes", "no"):
        # Closed but not yet resolved. Do NOT guess -- a wrong settlement is
        # worse than a late one, and the position stays recorded as-is.
        logger.warning(
            "%s: book is dead (status=%s) but no settlement result yet -- "
            "not settling; position stays marked at its last mid",
            ticker, status,
        )
        return None

    delta = state.inventory.settle(result_yes=(result == "yes"))
    final_price = 100.0 if result == "yes" else 0.0
    # Final mark so the dashboard's frozen-last-value behaviour freezes the
    # TRUE resolved number rather than a stale mid.
    sim_logger.log_inventory_mark(
        ticker=ticker,
        net_yes_contracts=state.inventory.net_yes_contracts,
        avg_entry_price=state.inventory.avg_entry_price,
        mid_price=final_price,
        unrealized_pnl_cents=0.0,
        realized_pnl_cents=state.inventory.realized_pnl_cents,
        realized_passive_cents=state.inventory.realized_passive_cents,
        realized_taker_cents=state.inventory.realized_taker_cents,
        realized_settlement_cents=state.inventory.realized_settlement_cents,
        max_inventory=state.max_inventory,
    )
    logger.warning(
        "%s: SETTLED %s -- booked %+.2f cents realized, position flat", ticker, result.upper(), delta
    )
    return delta


async def market_switch_loop(
    rest_client: KalshiRestClient,
    ws_client: KalshiWebSocketClient,
    orderbook_manager: OrderbookManager,
    states: dict[str, MarketRuntimeState],
    sim_logger: SimulationLogger,
    interval_sec: float,
) -> None:
    """
    Periodically checks each currently-watched ticker for a sustained
    trigger condition (see market_switcher.MarketSwitcher), and if one
    fires and a replacement is found, performs the switch: flattens any
    open hypothetical position on the old ticker, unsubscribes it,
    subscribes the replacement, and swaps its MarketRuntimeState into
    `states`. No-ops entirely if config.MARKET_SWITCHER_ENABLED is False.
    Mutates `states` in place -- scoring_loop/make_trade_handler both
    iterate/read the same dict, so a switch takes effect on their very
    next tick.
    """
    if not config.MARKET_SWITCHER_ENABLED:
        logger.info("Market switcher disabled (config.MARKET_SWITCHER_ENABLED = False) -- market_switch_loop is a no-op.")
        return

    switcher = MarketSwitcher()

    while True:
        await asyncio.sleep(interval_sec)

        for ticker in list(states.keys()):
            try:
                state = states[ticker]
                book = orderbook_manager.books.get(ticker)
                if book is None or book.stale:
                    continue

                # Liveness first. A settled market's book is empty, and every
                # other signal reads that as GOOD: crowding falls (no
                # competing size), risk falls (no price movement), reward
                # density rises. check_trigger therefore never fires, and if
                # something else does fire, the crowding gate below refuses
                # the switch because nothing looks less crowded than a dead
                # market. See config.DEAD_MARKET_EMPTY_BOOK_SECONDS for the
                # 2026-10-04 incident this prevents.
                crowding = compute_crowding_ratio(book, state.target_size)
                if state.is_dead(time.time()):
                    trigger = "dead"
                    logger.warning(
                        "%s: book empty for %.0fs -- treating as settled/dead and forcing a switch",
                        ticker, time.time() - state.empty_book_since,
                    )
                else:
                    trigger = switcher.check_trigger(ticker, state.risk_engine.risk_level, crowding)
                    if trigger is None:
                        continue
                    # Leaving a live market drops its position from the
                    # books, and with taker exits off there is no flatten to
                    # close it first. Stay until it fills out or settles; the
                    # dead path above settles before leaving. See
                    # config.TAKER_EXITS_ENABLED.
                    if not config.TAKER_EXITS_ENABLED and state.inventory.net_yes_contracts != 0:
                        switcher.record_search_attempt()
                        logger.info(
                            "%s: '%s' trigger fired but holding %d contracts with taker exits "
                            "disabled -- staying until flat or settled",
                            ticker, trigger, state.inventory.net_yes_contracts,
                        )
                        continue

                logger.warning(
                    "%s: sustained '%s' trigger fired (risk=%s, crowding=%s) -- searching for a replacement market",
                    ticker, trigger, state.risk_engine.risk_level, crowding,
                )

                # screen_incentive_programs + per-candidate get_orderbook calls
                # are blocking `requests` calls -- keep them off the event loop.
                replacement = await asyncio.to_thread(find_replacement, rest_client, set(states.keys()))
                if replacement is None:
                    # Throttle retries via the same cooldown as a successful
                    # switch -- otherwise a sustained-triggered ticker with no
                    # qualifying replacement re-runs this full search (a
                    # paginated incentive-program scan + up to N live
                    # get_orderbook calls) every check interval indefinitely.
                    # See MarketSwitcher.record_search_attempt's docstring for
                    # the live incident this fixes.
                    switcher.record_search_attempt()
                    logger.info("%s: no replacement found -- backing off for the switch cooldown before retrying", ticker)
                    continue

                # A "crowded" trigger only means "worth searching" --
                # still requires the best candidate found to be a
                # MEANINGFUL relative improvement, not just technically
                # ranked best among whatever's available. See
                # is_worthwhile_crowding_switch's docstring for the live
                # incident (2026-09-30) this fixes: without this check,
                # an absolute crowding threshold alone kept re-triggering
                # a search every cooldown period once EVERY active
                # program exceeded it, with no market ever actually
                # qualifying as "better enough" to stop the cycle.
                # "volatile" (HIGH risk) triggers skip this gate --
                # escaping our own risk exposure doesn't require the
                # replacement to specifically be less crowded.
                # "dead" and "volatile" both skip this gate: there is no
                # point requiring a replacement be less crowded than a market
                # we must leave regardless.
                # Revenue gate. Escaping crowding is not worth doing if the
                # replacement barely pays -- see
                # config.MIN_REPLACEMENT_REWARD_DENSITY_FRACTION for the
                # 2026-10-05 case this prevents. "dead" is exempt: we have to
                # leave regardless, so anything beats a settled book.
                if trigger != "dead" and not is_reward_preserving_switch(
                    state.reward_density_score,
                    replacement.reward_density_score,
                    config.MIN_REPLACEMENT_REWARD_DENSITY_FRACTION,
                ):
                    switcher.record_search_attempt()
                    logger.info(
                        "%s: best replacement %s pays reward density %.2f vs our %.2f "
                        "(need >= %.0f%%) -- staying put rather than trading revenue for comfort",
                        ticker, replacement.market_ticker,
                        replacement.reward_density_score or 0.0,
                        state.reward_density_score or 0.0,
                        config.MIN_REPLACEMENT_REWARD_DENSITY_FRACTION * 100,
                    )
                    continue

                if trigger == "crowded" and not is_worthwhile_crowding_switch(
                    crowding, replacement.crowding_ratio, config.CROWDING_RELATIVE_IMPROVEMENT_FACTOR
                ):
                    switcher.record_search_attempt()
                    logger.info(
                        "%s: best replacement %s (crowding=%s) isn't meaningfully less crowded than staying "
                        "put (crowding=%s, need <= %.0f%% of that) -- not switching",
                        ticker, replacement.market_ticker, replacement.crowding_ratio, crowding,
                        config.CROWDING_RELATIVE_IMPROVEMENT_FACTOR * 100,
                    )
                    continue

                if trigger == "dead":
                    # Settle BEFORE flattening. flatten_position needs a side
                    # of the book to cross and a dead market has none, so it
                    # would return None and silently leave the position
                    # frozen at its last mid -- which is the bias this fixes.
                    # After settling, the position is flat and flatten below
                    # is correctly a no-op.
                    await asyncio.to_thread(
                        _settle_if_resolved, rest_client, ticker, state, sim_logger
                    )

                fill = quoting_policy.flatten_position(
                    book, state.inventory, state.max_inventory, reason=f"market_switch_{trigger}",
                )
                if fill is not None:
                    sim_logger.log_simulated_stop_loss(ticker=ticker, **fill)

                removed = await ws_client.remove_ticker(ticker)
                if not removed:
                    logger.error("%s: failed to remove from live subscription -- aborting this switch attempt", ticker)
                    continue

                added = await ws_client.add_ticker(replacement.market_ticker)
                if not added:
                    logger.error(
                        "%s: failed to add replacement to live subscription after removing %s -- "
                        "now watching neither until the next interval retries",
                        replacement.market_ticker, ticker,
                    )
                    continue

                net_flattened = fill["net_yes_contracts_before"] if fill else None
                del states[ticker]
                switcher.clear_ticker(ticker)
                states[replacement.market_ticker] = MarketRuntimeState(
                    replacement.market_ticker,
                    target_size=replacement.target_size,
                    discount_factor=replacement.discount_factor_bps / 10000.0,
                    reward_density_score=replacement.reward_density_score,
                    # Carry the vacated market's capital allocation forward
                    # unchanged rather than re-splitting across everyone else --
                    # a switch is scoped to the one market that triggered it,
                    # not a full portfolio rebalance (see capital_allocator.py).
                    max_inventory=state.max_inventory,
                )

                # fetch_market_metadata makes a blocking `requests` call --
                # keep it off the event loop, same as the other REST calls above.
                title, subtitle = await asyncio.to_thread(fetch_market_metadata, rest_client, replacement.market_ticker)
                sim_logger.upsert_market_metadata(ticker=replacement.market_ticker, title=title, subtitle=subtitle)

                sim_logger.log_market_switch(
                    old_ticker=ticker,
                    new_ticker=replacement.market_ticker,
                    reason=trigger,
                    net_yes_contracts_flattened=net_flattened,
                )
                switcher.record_switch()
                logger.warning("Switched from %s to %s (reason=%s)", ticker, replacement.market_ticker, trigger)
            except Exception:
                # A transient failure anywhere in this block (e.g. a
                # ConnectionError from find_replacement's REST calls --
                # OBSERVED LIVE 2026-09-02: this crashed the entire
                # process, since asyncio.gather propagates any unhandled
                # exception from one task and cancels the whole event
                # loop) must never take down the whole run. Every other
                # loop in this file (incentive_refresh_loop,
                # news_refresh_loop, event_refresh_loop) already guards
                # itself this way; this one didn't. Retried next interval.
                logger.exception("Market switch check failed for %s", ticker)


def make_trade_handler(
    orderbook_manager: OrderbookManager,
    states: dict[str, MarketRuntimeState],
    sim_logger: SimulationLogger,
):
    async def on_trade(trade_msg: dict) -> None:
        ticker = trade_msg.get("market_ticker")
        try:
            # Kalshi's real trade fields are "yes_price_dollars" (dollar string)
            # and "count_fp" (fractional-capable "_fp" string) -- confirmed via
            # a live REST /markets/trades response 2026-09-01. Falling back to
            # the originally-assumed plain "yes_price"/"count" names too, in
            # case the WS trade message differs from the REST trade record.
            raw_price = trade_msg.get("yes_price_dollars", trade_msg.get("yes_price"))
            raw_count = trade_msg.get("count_fp", trade_msg.get("count", 0))
            taker_side = trade_msg.get("taker_side")
            if raw_price is None or not raw_count or taker_side is None:
                # Unlike a missing book side (routine), this means NEITHER of
                # our guessed field-name variants matched -- log loudly so a
                # long unattended run doesn't silently never simulate a fill.
                logger.error("%s: unrecognized trade message shape, skipping -- raw message: %s", ticker, trade_msg)
                return

            trade_yes_price = dollars_to_cents(raw_price) if isinstance(raw_price, str) else raw_price
            trade_count = fp_to_size(raw_count) if isinstance(raw_count, str) else raw_count

            # Persist the tape FIRST, before any check on our own state.
            #
            # This deliberately sits above the state/quote guard below. The
            # tape has to be independent of what we happened to be doing, or
            # it cannot be replayed at a different offset -- and the periods
            # where we have NO quote (HIGH risk, quote pulled) are exactly
            # the ones worth replaying. Logging after the guard would have
            # silently omitted them.
            sim_logger.log_trade(
                ticker=ticker,
                trade_yes_price=trade_yes_price,
                trade_count=trade_count,
                taker_side=taker_side,
            )

            state = states.get(ticker)
            if state is None or state.quote is None:
                return

            book = orderbook_manager.books.get(ticker)
            if book is None:
                return

            my_share_estimate = compute_my_share_at_touched_price(
                book, state.quote, trade_yes_price, taker_side
            )

            # Captured before mutation so classify_fill_exit_reason (below)
            # can tell whether this fill reduced an EXISTING position at a
            # profitable price (take-profit exit) vs. accumulated further,
            # purely for logging -- doesn't affect the fill itself.
            prior_net = state.inventory.net_yes_contracts
            prior_avg_entry = state.inventory.avg_entry_price

            process_trade_for_inventory(
                inventory=state.inventory,
                quote=state.quote,
                trade_yes_price=trade_yes_price,
                trade_count=trade_count,
                taker_side=taker_side,
                my_share_at_price=my_share_estimate,
                max_inventory=state.max_inventory,
            )

            fill_side = "yes" if taker_side == "no" else "no"
            fill_price = trade_yes_price if taker_side == "no" else (100 - trade_yes_price)
            exit_reason = quoting_policy.classify_fill_exit_reason(
                prior_net, prior_avg_entry, fill_side, fill_price, config.TAKE_PROFIT_MARGIN_CENTS,
            )
            sim_logger.log_simulated_fill(
                ticker=ticker,
                side=fill_side,
                price=fill_price,
                size=round(trade_count * my_share_estimate),
                exit_reason=exit_reason,
            )
            if exit_reason == "take_profit_exit":
                logger.info("%s: take-profit exit fill (%s %d @ %dc)", ticker, fill_side, round(trade_count * my_share_estimate), fill_price)
        except Exception:
            # on_trade is invoked directly from ws_client's message loop
            # (see run_forever/_run_once/_handle) -- an unhandled
            # exception here doesn't crash the whole process, but IS
            # caught by run_forever's outer try/except, which tears down
            # and reconnects the ENTIRE WebSocket session (forcing a
            # full resync of every watched book) over a single bad
            # trade message. Guard here instead so one malformed/
            # unexpected trade is simply skipped.
            logger.exception("%s: failed to process trade message: %s", ticker, trade_msg)

    return on_trade


def _select_initial_watchlist(rest_client: KalshiRestClient) -> tuple[list[str], list[ScreenedProgram]]:
    """
    Picks the watch list main.py starts with. If config.AUTO_SELECT_WATCHLIST
    is True (the default), autonomously selects config.NUM_MARKETS_TO_WATCH
    markets via incentive_screener.select_watchlist -- no manual curation
    needed, and never disqualified by crowding alone (see that function's
    docstring: real markets right now are all fairly crowded, so it picks
    the least-bad N available rather than nothing). Falls back to the
    manual config.WATCHED_MARKET_TICKERS list only if auto-selection is
    disabled or there simply aren't config.NUM_MARKETS_TO_WATCH active
    programs to choose from at all.

    Returns (tickers, screened_programs) -- screened_programs is reused
    directly by build_market_states below (no redundant re-screen).
    """
    if config.AUTO_SELECT_WATCHLIST:
        selected = select_watchlist(rest_client, config.NUM_MARKETS_TO_WATCH)
        if selected:
            tickers = [p.market_ticker for p in selected]
            logger.info("Auto-selected watch list (%d markets): %s", len(tickers), tickers)
            return tickers, selected
        logger.warning(
            "Auto-selection found no active incentive programs at all -- falling back to "
            "config.WATCHED_MARKET_TICKERS"
        )

    if not config.WATCHED_MARKET_TICKERS:
        return [], []
    return config.WATCHED_MARKET_TICKERS, screen_incentive_programs(rest_client)


async def run() -> None:
    # Structural protection against ever again running two instances at
    # once (see singleton_lock.py for why this exists) -- checked first,
    # before any network calls, so a duplicate instance fails fast.
    acquire_singleton_lock("main", config.BASE_DIR)

    signer = None
    if config.KALSHI_API_KEY_ID and config.KALSHI_PRIVATE_KEY_PATH:
        signer = KalshiSigner()
    else:
        logger.warning("No credentials found -- attempting to run without auth (public data only).")

    rest_client = KalshiRestClient(signer=signer)
    orderbook_manager = OrderbookManager()
    sim_logger = SimulationLogger()

    # _select_initial_watchlist makes blocking `requests` calls (possibly
    # several, if auto-selecting); run it off the event loop.
    watched_tickers, screened = await asyncio.to_thread(_select_initial_watchlist, rest_client)
    if not watched_tickers:
        logger.error(
            "No markets to watch: auto-selection (if enabled) found nothing eligible, and "
            "config.WATCHED_MARKET_TICKERS is empty. Run incentive_screener.py yourself, pick some "
            "active liquidity-incentivized markets, and add their tickers to config.py, or check "
            "connectivity/credentials."
        )
        return

    # Seed per-market state with the REAL target_size / discount_factor
    # already fetched above -- watched tickers with no active program
    # (only possible via the manual fallback list) are logged and
    # skipped, since there's no live program to simulate scoring against.
    states = build_market_states(watched_tickers, screened)

    # Split the pretend starting balance across whatever we're actually
    # watching (states.keys(), not the full `screened` list, which for
    # the manual-fallback path can include unwatched programs too),
    # weighted by profit/risk -- see capital_allocator.py.
    relevant_programs = [p for p in screened if p.market_ticker in states]
    allocations = allocate_capital(
        config.STARTING_CAPITAL_USD, relevant_programs, config.MIN_ALLOCATION_FRACTION, config.MAX_ALLOCATION_FRACTION
    )
    for ticker, state in states.items():
        state.max_inventory = allocations.get(ticker, config.MAX_SIMULATED_INVENTORY)
    logger.info(
        "Starting balance $%.0f allocated across %d market(s): %s",
        config.STARTING_CAPITAL_USD, len(states), {t: s.max_inventory for t, s in states.items()},
    )

    # One-time fetch (not a recurring poll -- title/subtitle are
    # effectively static) so the dashboard can show a human-readable
    # market name instead of just the raw ticker. Blocking `requests`
    # calls, run off the event loop.
    def _fetch_and_store_all_market_metadata() -> None:
        for t in states:
            title, subtitle = fetch_market_metadata(rest_client, t)
            sim_logger.upsert_market_metadata(ticker=t, title=title, subtitle=subtitle)

    await asyncio.to_thread(_fetch_and_store_all_market_metadata)

    ws_client = KalshiWebSocketClient(
        tickers=watched_tickers,
        orderbook_manager=orderbook_manager,
        signer=signer,
        on_trade=make_trade_handler(orderbook_manager, states, sim_logger),
    )

    logger.info("Starting Phase 0 paper-trading pipeline (NO live orders will be placed).")

    await asyncio.gather(
        ws_client.run_forever(),
        scoring_loop(orderbook_manager, states, sim_logger),
        incentive_refresh_loop(rest_client, states, sim_logger, config.INCENTIVE_POLL_INTERVAL_SEC),
        news_refresh_loop(rest_client, states, sim_logger, config.NEWS_POLL_INTERVAL_SEC),
        event_refresh_loop(rest_client, states, sim_logger, config.EVENT_CALENDAR_POLL_INTERVAL_SEC),
        market_switch_loop(rest_client, ws_client, orderbook_manager, states, sim_logger, config.MARKET_SWITCH_CHECK_INTERVAL_SEC),
    )


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Shutting down.")
