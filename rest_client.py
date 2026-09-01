"""
Minimal REST client for Kalshi's v2 API -- Phase 0 read-only surface.

We deliberately only wrap GET endpoints here. Phase 0 never places,
amends, or cancels orders.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlencode

import requests

import config
from auth import KalshiSigner

logger = logging.getLogger(__name__)

if config.is_read_only_mode():
    logger.info("Running in READ-ONLY mode (Phase 0) -- no orders can be placed.")

# Kalshi's signed path must match the REST path prefix, not the fully
# qualified URL. e.g. base "https://api.elections.kalshi.com/trade-api/v2"
# -> signed path prefix "/trade-api/v2"
_PATH_PREFIX = "/trade-api/v2"


class KalshiRestClient:
    # Structural safety guard: this client may only ever issue GET
    # requests. Phase 0 is read-only by design (config.is_read_only_mode());
    # this makes that explicit and enforced, not just a comment.
    ALLOWED_METHODS = {"GET"}

    def __init__(self, signer: KalshiSigner | None = None, base_url: str | None = None):
        # signer is optional: some endpoints (e.g. public market listings)
        # may work unauthenticated, but Kalshi's own guidance is that it's
        # safest to always sign. We support both modes explicitly.
        self.signer = signer
        self.base_url = base_url or config.REST_BASE_URL
        self.session = requests.Session()

    def _get(self, path: str, params: dict[str, Any] | None = None, max_retries: int = 5) -> dict:
        """
        `path` should be like "/markets" (relative to /trade-api/v2).
        """
        method = "GET"
        assert method in self.ALLOWED_METHODS, f"{method} is not an allowed method -- this client is read-only."

        full_path = f"{_PATH_PREFIX}{path}"
        url = f"{self.base_url}{path}"

        headers = {"Accept": "application/json"}
        if self.signer is not None:
            headers.update(self.signer.sign("GET", full_path))

        backoff = 1.0
        for attempt in range(max_retries):
            resp = self.session.get(url, headers=headers, params=params, timeout=10)

            if resp.status_code == 429:
                logger.warning("Rate limited (429) on %s, backing off %.1fs", path, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue

            if resp.status_code >= 400:
                logger.error("Kalshi API error %s on %s: %s", resp.status_code, path, resp.text[:500])
                resp.raise_for_status()

            return resp.json()

        raise RuntimeError(f"Exceeded retries on {path}")

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------
    def get_markets(self, **params) -> dict:
        """List markets. Supports status, series_ticker, limit, cursor, etc."""
        return self._get("/markets", params=params)

    def get_market(self, ticker: str) -> dict:
        return self._get(f"/markets/{ticker}")

    def get_orderbook(self, ticker: str, depth: int | None = None) -> dict:
        params = {"depth": depth} if depth else None
        return self._get(f"/markets/{ticker}/orderbook", params=params)

    def get_trades(self, ticker: str, limit: int = 100, **params) -> dict:
        return self._get(f"/markets/{ticker}/trades", params={"limit": limit, **params})

    # ------------------------------------------------------------------
    # Incentive programs (the whole point of this project)
    # ------------------------------------------------------------------
    def get_incentive_programs(
        self,
        status: str = "active",
        program_type: str = "liquidity",
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict:
        """
        status: "all" | "active" | "upcoming" | "closed" | "paid_out"
        program_type: "all" | "liquidity" | "volume"
        """
        params = {"status": status, "type": program_type, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._get("/incentive_programs", params=params)

    def get_all_incentive_programs(self, status: str = "active", program_type: str = "liquidity") -> list[dict]:
        """Paginate through all incentive programs matching the filter."""
        results: list[dict] = []
        cursor = None
        while True:
            page = self.get_incentive_programs(status=status, program_type=program_type, cursor=cursor)
            results.extend(page.get("incentive_programs", []))
            cursor = page.get("next_cursor")
            if not cursor:
                break
        return results

    # ------------------------------------------------------------------
    # Account (requires auth)
    # ------------------------------------------------------------------
    def get_account_limits(self) -> dict:
        return self._get("/account/limits")

    def get_balance(self) -> dict:
        return self._get("/portfolio/balance")
