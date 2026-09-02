"""
Event-triggered market switcher: stays in currently-watched markets by
default, and only searches for a replacement when the CURRENT market
gives a clear, SUSTAINED signal to leave -- sustained HIGH risk
(risk_engine.py) or sustained crowding (real resting depth well past
what Target Size requires, meaning diminishing marginal share for a new
entrant). Deliberately NOT a continuous re-optimizer: switches require a
condition to hold for config.SWITCH_TRIGGER_SUSTAIN_SEC and respect a
global config.MARKET_SWITCH_COOLDOWN_SEC between switches, per the
explicit design brief -- "does not need to be switching rapidly, only
when needed."

Runtime-only: does NOT rewrite config.WATCHED_MARKET_TICKERS. If main.py
restarts, the watch list reverts to whatever's configured there. A switch
away from a market simulates flattening any open hypothetical position
first, reusing quoting_policy.flatten_position (the exact same crossing
logic as the HIGH-risk hard stop) -- still no real orders anywhere.

Candidate search: re-runs incentive_screener.screen_incentive_programs,
then pre-screens each reward-dense candidate's REAL crowding via a
single get_orderbook REST call. Crowding can be checked this way without
ever having watched a candidate; volatility can't (needs a time series),
which is exactly why crowding, not volatility, is the criterion used to
evaluate REPLACEMENT candidates below.
"""

from __future__ import annotations

import logging
import time

import config
from incentive_screener import ScreenedProgram, screen_incentive_programs
from orderbook import MarketBook, fp_to_size
from rest_client import KalshiRestClient
from risk_engine import HIGH

logger = logging.getLogger(__name__)


def compute_crowding_ratio(book: MarketBook, target_size: float) -> float | None:
    """
    Real resting depth (both sides, excluding any hypothetical order of
    ours) relative to target_size. None if target_size isn't positive.
    """
    if target_size <= 0:
        return None
    yes_total = sum(book.yes_levels.values())
    no_total = sum(book.no_levels.values())
    return (yes_total + no_total) / (2.0 * target_size)


def _crowding_ratio_from_raw_orderbook(raw: dict, target_size: float) -> float | None:
    """
    Same computation as compute_crowding_ratio, but from a raw REST
    get_orderbook response, for pre-screening a candidate we haven't
    subscribed to. CONFIRMED live 2026-09-02:
    {"orderbook_fp": {"yes_dollars": [[price_str, size_str], ...], "no_dollars": [...]}}.
    """
    if target_size <= 0:
        return None
    book_fp = raw.get("orderbook_fp", {}) or {}
    yes_total = sum(fp_to_size(size) for _, size in (book_fp.get("yes_dollars") or []))
    no_total = sum(fp_to_size(size) for _, size in (book_fp.get("no_dollars") or []))
    return (yes_total + no_total) / (2.0 * target_size)


class MarketSwitcher:
    """
    Tracks how long each currently-watched ticker has continuously held
    a trigger condition, and enforces the global cooldown between
    switches. Stateful, one instance shared across the whole run.
    """

    def __init__(self):
        self._condition_since: dict[str, float] = {}
        self._last_switch_ts: float = 0.0

    def check_trigger(
        self,
        ticker: str,
        risk_level: str,
        crowding_ratio: float | None,
        now: float | None = None,
    ) -> str | None:
        """
        Returns "volatile", "crowded", or None. Call once per ticker per
        check interval. Only returns non-None once the condition has held
        continuously for config.SWITCH_TRIGGER_SUSTAIN_SEC AND the global
        cooldown since the last switch has elapsed.
        """
        now = now if now is not None else time.time()

        is_volatile = risk_level == HIGH
        is_crowded = crowding_ratio is not None and crowding_ratio >= config.CROWDING_RATIO_THRESHOLD
        condition = "volatile" if is_volatile else ("crowded" if is_crowded else None)

        if condition is None:
            self._condition_since.pop(ticker, None)
            return None

        since = self._condition_since.setdefault(ticker, now)
        sustained = (now - since) >= config.SWITCH_TRIGGER_SUSTAIN_SEC
        cooled_down = (now - self._last_switch_ts) >= config.MARKET_SWITCH_COOLDOWN_SEC

        return condition if (sustained and cooled_down) else None

    def record_switch(self, now: float | None = None) -> None:
        self._last_switch_ts = now if now is not None else time.time()
        self._condition_since.clear()

    def clear_ticker(self, ticker: str) -> None:
        self._condition_since.pop(ticker, None)


def find_replacement(
    rest_client: KalshiRestClient,
    exclude_tickers: set[str],
    max_candidates_to_check: int = 10,
) -> ScreenedProgram | None:
    """
    Re-screens active incentive programs (ranked by the existing
    reward-density heuristic), skipping anything already watched, and
    returns the first candidate whose REAL crowding ratio is at/below
    config.SWITCH_CANDIDATE_MAX_CROWDING_RATIO. None if none qualify
    among the top `max_candidates_to_check` reward-dense candidates.
    """
    screened = screen_incentive_programs(rest_client, top_n=1000)
    candidates = [p for p in screened if p.market_ticker not in exclude_tickers]

    for program in candidates[:max_candidates_to_check]:
        try:
            raw = rest_client.get_orderbook(program.market_ticker)
        except Exception:
            logger.warning("Failed to fetch orderbook for candidate %s", program.market_ticker, exc_info=True)
            continue

        crowding = _crowding_ratio_from_raw_orderbook(raw, program.target_size)
        if crowding is not None and crowding <= config.SWITCH_CANDIDATE_MAX_CROWDING_RATIO:
            logger.info(
                "%s: selected as replacement candidate (crowding=%.2f, reward_density=%.4f)",
                program.market_ticker, crowding, program.reward_density_score,
            )
            return program

    logger.warning(
        "No replacement candidate found under crowding threshold %.2f among top %d checked",
        config.SWITCH_CANDIDATE_MAX_CROWDING_RATIO, max_candidates_to_check,
    )
    return None
