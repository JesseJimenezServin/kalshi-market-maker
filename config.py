"""
Central configuration for the Kalshi Liquidity Lab (Phase 0: paper trading / simulation only).

IMPORTANT: This module never places live orders. Phase 0 is entirely read-only:
it connects to Kalshi's public/authenticated market-data endpoints, logs what
IS happening, and simulates what WOULD happen if we quoted liquidity -- no
capital is ever at risk in this phase.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------
# Kalshi has separate demo and production environments with separate
# credentials. ALWAYS start on demo.
ENVIRONMENT = os.getenv("KALSHI_ENV", "demo")  # "demo" or "prod"

REST_BASE_URLS = {
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
    "prod": "https://api.elections.kalshi.com/trade-api/v2",
}

WS_URLS = {
    "demo": "wss://demo-api.kalshi.co/trade-api/ws/v2",
    "prod": "wss://api.elections.kalshi.com/trade-api/ws/v2",
}

REST_BASE_URL = REST_BASE_URLS[ENVIRONMENT]
WS_URL = WS_URLS[ENVIRONMENT]

# --------------------------------------------------------------------------
# Credentials (never hardcode these -- set as environment variables)
# --------------------------------------------------------------------------
KALSHI_API_KEY_ID = os.getenv("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")

# --------------------------------------------------------------------------
# Market universe
# --------------------------------------------------------------------------
# Start with an explicit, small list of market tickers to watch while we
# validate the pipeline. The incentive_screener will later suggest additions
# based on live incentive-program data -- but Phase 0 starts manual/explicit
# so we can sanity-check everything by hand.
WATCHED_MARKET_TICKERS: list[str] = [
    # Fill in with real tickers once you've identified live incentivized
    # markets via the screener, e.g. "KXHIGHNY-25AUG30-B75"
]

# How often to poll REST endpoints that don't have a WebSocket equivalent
# (e.g. incentive program listings). In seconds.
INCENTIVE_POLL_INTERVAL_SEC = 300  # 5 minutes

# --------------------------------------------------------------------------
# Reward simulation parameters
# --------------------------------------------------------------------------
# These mirror the mechanics Kalshi documents for the Liquidity Incentive
# Program. Per-market Target Size / Discount Factor are pulled live from the
# incentive_programs endpoint; these are just fallback/simulation defaults
# used when we want to test "what if I quoted X contracts" hypotheticals.
DEFAULT_SIMULATED_QUOTE_SIZE = 100  # contracts, per side

# --------------------------------------------------------------------------
# Risk / adverse-selection simulation parameters
# --------------------------------------------------------------------------
# When simulating "what if I had a resting order here", this is the max
# hypothetical inventory (in contracts) we allow the simulator to accumulate
# per market before it assumes we'd have pulled quotes. This should match
# whatever position limit you'd actually run live in Phase 1.
MAX_SIMULATED_INVENTORY = 500

# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
DB_PATH = DATA_DIR / "paper_trading.sqlite"

DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)
