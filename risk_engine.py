"""
Tracks realized volatility of mid-price and composes it with current
hypothetical inventory to produce a coarse risk level ("LOW" / "ELEVATED" /
"HIGH") that quoting_policy.py uses to widen, skew, or pull hypothetical
quotes, and to decide when to simulate an active flatten.

This module is read-only/derivative: it never places or amends orders, and
it does not mutate InventoryState -- it only reads net_yes_contracts off the
InventoryState object passed into update() each tick.
"""

from __future__ import annotations

import statistics
import time
from collections import deque
from dataclasses import dataclass, field

import config
from adverse_selection_sim import InventoryState

LOW = "LOW"
ELEVATED = "ELEVATED"
HIGH = "HIGH"

_SEVERITY = {LOW: 0, ELEVATED: 1, HIGH: 2}


def _max_severity(a: str, b: str) -> str:
    return a if _SEVERITY[a] >= _SEVERITY[b] else b


@dataclass
class RiskEngine:
    ticker: str
    _last_mid: float | None = field(default=None, repr=False)
    _mid_changes: deque = field(
        default_factory=lambda: deque(maxlen=config.VOLATILITY_WINDOW_SNAPSHOTS),
        repr=False,
    )
    _news_active_until: float | None = field(default=None, repr=False)
    news_signal: bool = False
    event_signal: bool = False
    risk_level: str = LOW
    # Public breakdown of the LAST composite risk_level's inputs -- set
    # every update(), so callers (main.py's scoring_loop, ultimately the
    # dashboard) can explain WHY risk_level is what it is, not just show
    # the final composite badge.
    volatility_level: str = LOW
    inventory_level: str = LOW

    def register_news_event(self, now: float | None = None) -> None:
        """
        Activates the news_signal for config.NEWS_SIGNAL_DECAY_MINUTES,
        called by news_feed.py when a relevant (per its coarse keyword
        heuristic) article is found for this ticker. Purely advisory --
        carries no direction or confidence, just "be more cautious."
        """
        now = now if now is not None else time.time()
        self._news_active_until = now + config.NEWS_SIGNAL_DECAY_MINUTES * 60

    def set_event_proximity(self, hours_until_close: float | None) -> None:
        """
        Called periodically (see event_calendar.event_refresh_loop) with
        this market's real time-until-close/resolution. Unlike news_signal
        above, this needs no artificial decay window: hours_until_close is
        already a live, self-correcting quantity computed fresh from
        Kalshi's own close_time each call. A market already past its close
        (negative hours) is NOT flagged -- that's settled/near-settled,
        which is safe, not risky.
        """
        self.event_signal = (
            hours_until_close is not None and 0 <= hours_until_close <= config.EVENT_CALENDAR_WARNING_HOURS
        )

    def update(
        self,
        mid_price: float,
        inventory: InventoryState,
        max_inventory: int,
        now: float | None = None,
    ) -> None:
        now = now if now is not None else time.time()

        if self._last_mid is not None:
            self._mid_changes.append(mid_price - self._last_mid)
        self._last_mid = mid_price

        self.news_signal = self._news_active_until is not None and now < self._news_active_until

        self.volatility_level = self._volatility_level()
        self.inventory_level = self._inventory_level(inventory, max_inventory)
        # News hits and event-proximity both carry no direction/confidence
        # -- they only ever nudge risk up one notch (toward ELEVATED), never
        # straight to HIGH on their own.
        news_level = ELEVATED if self.news_signal else LOW
        event_level = ELEVATED if self.event_signal else LOW
        composite = _max_severity(_max_severity(self.volatility_level, self.inventory_level), news_level)
        self.risk_level = _max_severity(composite, event_level)

    def _volatility_level(self) -> str:
        # Not enough samples yet for a meaningful stdev -- default to LOW
        # rather than reacting to a noisy/undefined estimate.
        if len(self._mid_changes) < config.VOLATILITY_WINDOW_SNAPSHOTS:
            return LOW
        vol = statistics.pstdev(self._mid_changes)
        if vol >= config.VOLATILITY_HIGH_THRESHOLD:
            return HIGH
        if vol >= config.VOLATILITY_ELEVATED_THRESHOLD:
            return ELEVATED
        return LOW

    @staticmethod
    def _inventory_level(inventory: InventoryState, max_inventory: int) -> str:
        if max_inventory <= 0:
            return LOW
        fraction = abs(inventory.net_yes_contracts) / max_inventory
        if fraction >= config.INVENTORY_FRACTION_HIGH_THRESHOLD:
            return HIGH
        if fraction >= config.INVENTORY_FRACTION_ELEVATED_THRESHOLD:
            return ELEVATED
        return LOW
