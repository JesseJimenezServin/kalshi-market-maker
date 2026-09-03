"""
Scheduled-event calendar awareness: feeds each watched market's REAL
close/resolution time into the risk engine as a proximity flag.
Liquidity Incentive Program markets close to resolution can see abrupt,
event-driven price moves (or, for an already-past-close market like
KXEOWEEK, essentially none) -- exactly the kind of risk today's
volatility-based RiskEngine can't see coming until it's already
happening (it only reacts to realized mid-price swings, after the fact).

Unlike news_feed.py's decaying point-in-time hits, hours_until_close is
already a live, self-correcting quantity computed fresh from Kalshi's
own close_time -- no artificial decay window is needed.

Read-only: this module only issues GET requests (via
rest_client.KalshiRestClient) and writes rows to SQLite via logger.py.
No order-placing code anywhere.

CONFIRMED live 2026-09-02: close_time's field name/ISO8601 format below
matched a real get_market() response on the first production restart
after this module was deployed (KXNFLSTADIUM -> 2028-09-01T03:59:00+00:00,
KXCITYCHAMPS -> 2030-07-01T03:59:00+00:00 -- both correctly far outside
EVENT_CALENDAR_WARNING_HOURS, so neither flagged).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import config
from logger import SimulationLogger
from rest_client import KalshiRestClient

logger = logging.getLogger(__name__)


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def get_market_close_time(rest_client: KalshiRestClient, ticker: str) -> datetime | None:
    """
    Fetches `ticker`'s real close/resolution time via the existing
    GET-only rest_client.get_market. Returns None (logging a warning,
    never raising) on any missing/malformed field -- a flaky or
    unexpected response must not crash the paper-trading pipeline.
    """
    try:
        response = rest_client.get_market(ticker)
    except Exception:
        logger.warning("Failed to fetch market metadata for %s", ticker, exc_info=True)
        return None

    market = response.get("market", response)
    close_time = market.get("close_time")
    if not close_time:
        return None
    try:
        return _parse_iso(close_time)
    except ValueError:
        logger.warning("%s: unparseable close_time %r", ticker, close_time)
        return None


def hours_until_close(close_time: datetime, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    return (close_time - now).total_seconds() / 3600.0


async def event_refresh_loop(
    rest_client: KalshiRestClient,
    states: dict,
    sim_logger: SimulationLogger,
    interval_sec: float,
) -> None:
    """
    Periodically fetches each watched ticker's real close_time and feeds
    hours_until_close into that ticker's RiskEngine
    (states[ticker].risk_engine) as a proximity flag. Also caches the
    raw close_time on the MarketRuntimeState itself
    (states[ticker].close_time) so main.py's scoring_loop can compute
    minutes_until_close fresh on every ~1s tick (for the take-profit
    time-decay and pre-resolution forced exit -- see quoting_policy.py)
    without needing its own network call, since close_time is static.
    No-ops entirely if config.EVENT_CALENDAR_ENABLED is False. Polls far
    less often than the ~1s scoring cadence -- close_time essentially
    never changes once a market is listed.
    """
    if not config.EVENT_CALENDAR_ENABLED:
        logger.info("Event calendar disabled (config.EVENT_CALENDAR_ENABLED = False) -- event_refresh_loop is a no-op.")
        return

    while True:
        for ticker, state in states.items():
            try:
                close_time = await asyncio.to_thread(get_market_close_time, rest_client, ticker)
                state.close_time = close_time
                hours = hours_until_close(close_time) if close_time is not None else None
                state.risk_engine.set_event_proximity(hours)
                sim_logger.log_event_calendar_snapshot(
                    ticker=ticker,
                    close_time=close_time.isoformat() if close_time else None,
                    hours_until_close=hours,
                    flagged=state.risk_engine.event_signal,
                )
                if state.risk_engine.event_signal:
                    logger.info(
                        "%s: within event-proximity window (%.1fh until close) -- nudging risk up",
                        ticker, hours,
                    )
            except Exception:
                logger.exception("Event calendar poll failed for %s", ticker)

        await asyncio.sleep(interval_sec)
