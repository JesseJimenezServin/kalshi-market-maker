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

import config
from auth import KalshiSigner
from rest_client import KalshiRestClient
from orderbook import OrderbookManager
from ws_client import KalshiWebSocketClient
from incentive_screener import screen_incentive_programs, print_report
from reward_simulator import score_both_sides
from adverse_selection_sim import HypotheticalQuote, InventoryState, process_trade_for_inventory
from logger import SimulationLogger

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("main")


class MarketRuntimeState:
    """Per-market hypothetical quote + inventory + program parameters."""

    def __init__(self, ticker: str, target_size: float, discount_factor: float):
        self.ticker = ticker
        self.target_size = target_size
        self.discount_factor = discount_factor
        self.inventory = InventoryState(ticker=ticker)
        self.quote: HypotheticalQuote | None = None  # set once we have book data

    def refresh_quote_from_book(self, book) -> None:
        """
        Naive default quoting rule for Phase 0 simulation: rest at the
        current best bid on each side (i.e. join the touch), sized at
        config.DEFAULT_SIMULATED_QUOTE_SIZE. This is intentionally simple;
        the point of Phase 0 is to measure outcomes of a baseline policy,
        which later phases can improve on (e.g. inventory skew).
        """
        best_yes = book.best_yes_bid()
        best_no = book.best_no_bid()
        if best_yes is None or best_no is None:
            return
        self.quote = HypotheticalQuote(
            yes_price=best_yes[0],
            yes_size=config.DEFAULT_SIMULATED_QUOTE_SIZE,
            no_price=best_no[0],
            no_size=config.DEFAULT_SIMULATED_QUOTE_SIZE,
        )


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

            state.refresh_quote_from_book(book)
            if state.quote is None:
                continue

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
            best_yes = book.best_yes_bid()
            best_no = book.best_no_bid()
            if best_yes and best_no:
                mid = (best_yes[0] + (100 - best_no[0])) / 2.0
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


async def incentive_refresh_loop(rest_client: KalshiRestClient, interval_sec: float) -> None:
    while True:
        try:
            results = screen_incentive_programs(rest_client)
            logger.info("Incentive screen refreshed: %d active liquidity programs found", len(results))
            print_report(results)
        except Exception:
            logger.exception("Incentive screen failed")
        await asyncio.sleep(interval_sec)


def make_trade_handler(states: dict[str, MarketRuntimeState], sim_logger: SimulationLogger):
    async def on_trade(trade_msg: dict) -> None:
        ticker = trade_msg.get("market_ticker")
        state = states.get(ticker)
        if state is None or state.quote is None:
            return

        trade_yes_price = trade_msg.get("yes_price")
        trade_count = trade_msg.get("count", 0)
        taker_side = trade_msg.get("taker_side")
        if trade_yes_price is None or not trade_count or taker_side is None:
            return

        # Simplification: assume our hypothetical share of the touched
        # price level is proportional to our configured quote size vs.
        # the size that was resting there. Without that book snapshot at
        # trade time we fall back to a conservative fixed estimate.
        my_share_estimate = 0.15  # placeholder -- tune using Phase 1 real fill data

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

    # Bootstrap per-market state with placeholder target size / discount
    # factor; the incentive_refresh_loop below will keep the screener
    # informed, but for now we seed reasonable defaults if a market isn't
    # found in the live incentive list.
    states: dict[str, MarketRuntimeState] = {
        ticker: MarketRuntimeState(ticker, target_size=1000.0, discount_factor=0.9)
        for ticker in config.WATCHED_MARKET_TICKERS
    }

    ws_client = KalshiWebSocketClient(
        tickers=config.WATCHED_MARKET_TICKERS,
        orderbook_manager=orderbook_manager,
        signer=signer,
        on_trade=make_trade_handler(states, sim_logger),
    )

    logger.info("Starting Phase 0 paper-trading pipeline (NO live orders will be placed).")

    await asyncio.gather(
        ws_client.run_forever(),
        scoring_loop(orderbook_manager, states, sim_logger),
        incentive_refresh_loop(rest_client, config.INCENTIVE_POLL_INTERVAL_SEC),
    )


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Shutting down.")
