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
# Safety: read-only mode
# --------------------------------------------------------------------------
def is_read_only_mode() -> bool:
    """
    Structural safety guard, not a runtime toggle. Phase 0 is read-only by
    design -- no orders are ever placed, amended, or canceled, regardless
    of which Kalshi environment (demo/prod) or credentials are configured.
    Hardcoded True until Phase 1 (a small, deliberately-built live pilot)
    exists.
    """
    return True


# --------------------------------------------------------------------------
# Market universe
# --------------------------------------------------------------------------
# Start with an explicit, small list of market tickers to watch while we
# validate the pipeline. The incentive_screener will later suggest additions
# based on live incentive-program data -- but Phase 0 starts manual/explicit
# so we can sanity-check everything by hand.
WATCHED_MARKET_TICKERS: list[str] = [
    # Picked 2026-09-01 via incentive_screener.py against prod, favoring
    # low near-term volatility per the strategy notes below:
    "KXEOWEEK-26AUG22-0",            # exec-order count for a week already past -- likely near-settled
    "KXCITYCHAMPS-MIA30JUL01HDMP-2",  # Miami teams 2+ championships by 2030 -- multi-year horizon
    "KXNFLSTADIUM-28SEP01EVERBRENO-Y",  # Jacksonville stadium renovation before 2028 -- slow-moving fact

    # KXMLBPLAYOFFS-26-SD was added 2026-09-01 as a temporary stress-test
    # ticker and removed the same day after confirming the pipeline (it
    # found and fixed two real bugs: a broken resync request, and a
    # shared-vs-per-ticker sequence tracking bug -- see PROJECT_STATUS.md
    # and git history). Back to just the quiet-market thesis test above.
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
#
# Set 2026-09-01 to represent a ~$1000 pretend bankroll spread across the 3
# watched markets (333/market). This is a rough, CONSERVATIVE proxy, not an
# exact dollar figure: contracts are priced 1-99 cents and a single
# contract's max payout/loss is $1, so "contract count" only equals actual
# dollars at risk in the worst case where every contract simultaneously
# moves against us at $1 -- real dollar exposure at any given moment is
# typically much less than contract_count * $1.
MAX_SIMULATED_INVENTORY = 333

# --------------------------------------------------------------------------
# Active strategy simulation: risk engine + quoting policy
# --------------------------------------------------------------------------
# Rolling window (in scoring_loop snapshots, ~1s cadence) used to estimate
# realized volatility of the mid price. Volatility is measured as the
# stdev of tick-to-tick mid-price CHANGES in cents (not percent returns),
# to avoid divide-by-zero / blowup near the 1c and 99c price extremes.
VOLATILITY_WINDOW_SNAPSHOTS = 30

# Thresholds on stdev-of-mid-changes (cents) that escalate risk level.
VOLATILITY_ELEVATED_THRESHOLD = 1.5
VOLATILITY_HIGH_THRESHOLD = 3.0

# Thresholds on abs(net_yes_contracts) / max_inventory that escalate risk level.
INVENTORY_FRACTION_ELEVATED_THRESHOLD = 0.4
INVENTORY_FRACTION_HIGH_THRESHOLD = 0.7

# At HIGH risk, if our inventory fraction is at/above this, actively flatten
# (cross the spread) instead of just pulling the quote.
HARD_STOP_INVENTORY_FRACTION = 0.8

# Quote widening (in cents/ticks off best bid, after inventory skew is
# applied) when risk_level == ELEVATED.
WIDEN_TICKS_ELEVATED = 2

# Maximum number of cents/ticks the quoting policy will skew yes/no prices
# away from the touch in response to inventory, regardless of how large
# net_yes_contracts / max_inventory becomes.
MAX_INVENTORY_SKEW_TICKS = 5

# --------------------------------------------------------------------------
# News-awareness signal (zero-cost, purely advisory)
# --------------------------------------------------------------------------
# GDELT's free Doc 2.0 API (no key required) is polled for recent articles
# whose title/theme tags overlap with keywords derived from each watched
# market's title/subtitle. This is a coarse rule-based heuristic, NOT an
# NLP/LLM classifier -- see news_feed.py for the scoring logic and its
# documented limitations. Set NEWS_FEED_ENABLED = False to disable this
# signal entirely (e.g. to A/B test against the volatility-only baseline).
NEWS_FEED_ENABLED = True

GDELT_DOC_API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

# How often to poll GDELT for each watched market. In seconds.
NEWS_POLL_INTERVAL_SEC = 300  # 5 minutes

# How far back (hours) each GDELT query looks for articles.
NEWS_LOOKBACK_HOURS = 6

# Max number of keywords derived from a market's title/subtitle, used to
# build the GDELT query and to compute keyword-overlap relevance.
NEWS_MAX_KEYWORDS = 8

# Fraction of a market's derived keywords that must appear in an article's
# title/theme tags for that article to be flagged "relevant". Coarse by
# design -- see news_feed.py's score_relevance() docstring.
NEWS_RELEVANCE_THRESHOLD = 0.2

# How long (minutes) a relevant news hit keeps the risk engine's
# news_signal active before it decays back off.
NEWS_SIGNAL_DECAY_MINUTES = 45

# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
DB_PATH = DATA_DIR / "paper_trading.sqlite"

DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)
