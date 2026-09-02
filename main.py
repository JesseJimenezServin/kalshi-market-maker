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
from incentive_screener import ScreenedProgram, screen_incentive_programs, print_report
from reward_simulator import score_both_sides
from adverse_selection_sim import HypotheticalQuote, InventoryState, process_trade_for_inventory
from logger import SimulationLogger
import quoting_policy
from risk_engine import RiskEngine
from news_feed import news_refresh_loop

logger = logging.getLogger("main")


class MarketRuntimeState:
    """Per-market hypothetical quote + inventory + program parameters."""

    def __init__(self, ticker: str, target_size: float, discount_factor: float):
        self.ticker = ticker
        self.target_size = target_size
        self.discount_factor = discount_factor
        self.inventory = InventoryState(ticker=ticker)
        self.risk_engine = RiskEngine(ticker=ticker)
        self.quote: HypotheticalQuote | None = None  # set once we have book data


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
        )
    return states


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
            book = orderbook_manager.books.get(ticker)
            if book is None or book.stale:
                continue

            sim_logger.log_orderbook_snapshot(
                ticker=ticker,
                yes_levels=book.yes_side_sorted_desc(),
                no_levels=book.no_side_sorted_desc(),
            )

            # Mid price is needed both by the risk engine (before the quote
            # is computed) and by the inventory mark-to-market below, so
            # compute it once, up front, and reuse it. If either side of the
            # book is empty we can't compute a mid at all this tick.
            best_yes = book.best_yes_bid()
            best_no = book.best_no_bid()
            if best_yes is None or best_no is None:
                continue
            mid = (best_yes[0] + (100 - best_no[0])) / 2.0

            state.risk_engine.update(mid, state.inventory, config.MAX_SIMULATED_INVENTORY)

            state.quote = quoting_policy.compute_quote(
                book,
                state.risk_engine.risk_level,
                state.inventory,
                config.DEFAULT_SIMULATED_QUOTE_SIZE,
                config.MAX_SIMULATED_INVENTORY,
            )

            # Pure instrumentation for the dashboard (server.py) -- main.py
            # and server.py are separate processes with no shared memory,
            # so this is the only way the dashboard can see the current
            # quote/risk level. Does not affect any risk/quoting decision.
            sim_logger.log_quote_snapshot(
                ticker=ticker,
                risk_level=state.risk_engine.risk_level,
                quote_yes_price=state.quote.yes_price if state.quote else None,
                quote_yes_size=state.quote.yes_size if state.quote else None,
                quote_no_price=state.quote.no_price if state.quote else None,
                quote_no_size=state.quote.no_size if state.quote else None,
            )

            fill = quoting_policy.maybe_execute_stop_loss(
                book,
                state.inventory,
                state.risk_engine.risk_level,
                config.MAX_SIMULATED_INVENTORY,
                config.HARD_STOP_INVENTORY_FRACTION,
            )
            if fill is not None:
                sim_logger.log_simulated_stop_loss(ticker=ticker, **fill)

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
            )

        await asyncio.sleep(interval_sec)


async def incentive_refresh_loop(
    rest_client: KalshiRestClient,
    sim_logger: SimulationLogger,
    interval_sec: float,
) -> None:
    while True:
        try:
            # screen_incentive_programs makes blocking `requests` calls;
            # run it off the event loop so it doesn't freeze the WebSocket
            # connection / scoring_loop for however long the HTTP call takes.
            results = await asyncio.to_thread(screen_incentive_programs, rest_client)
            logger.info("Incentive screen refreshed: %d active liquidity programs found", len(results))
            print_report(results)
            for program in results:
                if program.market_ticker in config.WATCHED_MARKET_TICKERS:
                    sim_logger.log_incentive_program_snapshot(
                        ticker=program.market_ticker,
                        period_reward=program.period_reward,
                        target_size=program.target_size,
                        discount_factor_bps=program.discount_factor_bps,
                        days_remaining=program.days_remaining,
                    )
        except Exception:
            logger.exception("Incentive screen failed")
        await asyncio.sleep(interval_sec)


def make_trade_handler(
    orderbook_manager: OrderbookManager,
    states: dict[str, MarketRuntimeState],
    sim_logger: SimulationLogger,
):
    async def on_trade(trade_msg: dict) -> None:
        ticker = trade_msg.get("market_ticker")
        state = states.get(ticker)
        if state is None or state.quote is None:
            return

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

        book = orderbook_manager.books.get(ticker)
        if book is None:
            return

        my_share_estimate = compute_my_share_at_touched_price(
            book, state.quote, trade_yes_price, taker_side
        )

        process_trade_for_inventory(
            inventory=state.inventory,
            quote=state.quote,
            trade_yes_price=trade_yes_price,
            trade_count=trade_count,
            taker_side=taker_side,
            my_share_at_price=my_share_estimate,
            max_inventory=config.MAX_SIMULATED_INVENTORY,
        )
        sim_logger.log_simulated_fill(
            ticker=ticker,
            side="yes" if taker_side == "no" else "no",
            price=trade_yes_price if taker_side == "no" else (100 - trade_yes_price),
            size=round(trade_count * my_share_estimate),
        )

    return on_trade


async def run() -> None:
    if not config.WATCHED_MARKET_TICKERS:
        logger.error(
            "config.WATCHED_MARKET_TICKERS is empty. Run incentive_screener.py first, "
            "pick some active liquidity-incentivized markets, and add their tickers to config.py."
        )
        return

    signer = None
    if config.KALSHI_API_KEY_ID and config.KALSHI_PRIVATE_KEY_PATH:
        signer = KalshiSigner()
    else:
        logger.warning("No credentials found -- attempting to run without auth (public data only).")

    rest_client = KalshiRestClient(signer=signer)
    orderbook_manager = OrderbookManager()
    sim_logger = SimulationLogger()

    # Seed per-market state with the REAL target_size / discount_factor from
    # the live incentive screener. Watched tickers with no active program
    # are logged and skipped -- we don't simulate scoring for them.
    screened = screen_incentive_programs(rest_client)
    states = build_market_states(config.WATCHED_MARKET_TICKERS, screened)

    ws_client = KalshiWebSocketClient(
        tickers=config.WATCHED_MARKET_TICKERS,
        orderbook_manager=orderbook_manager,
        signer=signer,
        on_trade=make_trade_handler(orderbook_manager, states, sim_logger),
    )

    logger.info("Starting Phase 0 paper-trading pipeline (NO live orders will be placed).")

    await asyncio.gather(
        ws_client.run_forever(),
        scoring_loop(orderbook_manager, states, sim_logger),
        incentive_refresh_loop(rest_client, sim_logger, config.INCENTIVE_POLL_INTERVAL_SEC),
        news_refresh_loop(rest_client, states, sim_logger, config.NEWS_POLL_INTERVAL_SEC),
    )


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Shutting down.")
