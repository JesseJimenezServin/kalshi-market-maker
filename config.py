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
    #
    # NOTE (2026-09-02): with AUTO_SELECT_WATCHLIST=True below (the
    # default), this manual list is no longer what actually runs --
    # main.py picks its own watch list live at startup instead. This list
    # is now only a FALLBACK, used only if AUTO_SELECT_WATCHLIST is turned
    # off, or there are no active incentive programs at all to
    # auto-select from.
]

# --------------------------------------------------------------------------
# Fully autonomous watch-list selection
# --------------------------------------------------------------------------
# If True, main.py picks its own watch list at startup via
# incentive_screener.select_watchlist -- the crowding-aware ranking (see
# screen_incentive_programs' adjusted_score) -- instead of the manual
# WATCHED_MARKET_TICKERS list above. This ranking dampens for real
# crowding/volatility but never hard-disqualifies on it (CONFIRMED live
# 2026-09-02: every currently active incentive program is already
# well-crowded, least-crowded observed ~8.9x target_size -- a hard
# cutoff would always find nothing), so it always picks the least-bad
# N available rather than nothing. Falls back to WATCHED_MARKET_TICKERS
# only if there are no active incentive programs at all. Runtime-only,
# same as the market switcher: a restart re-runs selection from scratch,
# it does not persist or rewrite WATCHED_MARKET_TICKERS.
AUTO_SELECT_WATCHLIST = True

# How many markets to auto-select and watch at once.
NUM_MARKETS_TO_WATCH = 3

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
# Capital allocation across the current watch list (profit/risk-weighted)
# --------------------------------------------------------------------------
# The pretend total starting balance main.py allocates across whichever
# markets it's currently watching (see capital_allocator.allocate_capital,
# wired into main.py's run()). Allocation is weighted by each market's
# profit/risk ratio (incentive_screener's adjusted_score -- reward density
# dampened by real crowding and recent volatility), not split evenly --
# a market with a better profit/risk ratio gets more of the balance, one
# with a worse ratio gets less, everyone gets at least MIN_ALLOCATION_FRACTION.
STARTING_CAPITAL_USD = 1000.0

# Bounds on each market's share of STARTING_CAPITAL_USD, as a fraction of
# the total -- prevents any one market from being fully starved or fully
# concentrated. Chosen for a small watch list (config.NUM_MARKETS_TO_WATCH);
# see capital_allocator.allocate_capital's docstring for why this floor is
# a soft preference, not a hard guarantee, if the watch list ever grows large.
MIN_ALLOCATION_FRACTION = 0.15
MAX_ALLOCATION_FRACTION = 0.60

# --------------------------------------------------------------------------
# Risk / adverse-selection simulation parameters
# --------------------------------------------------------------------------
# Fallback max hypothetical inventory (in contracts) for a market that,
# for whatever reason, didn't get a per-market allocation from
# capital_allocator.allocate_capital above (MarketRuntimeState's default,
# also used defensively in main.py if a ticker is somehow missing from
# the allocation result). This is a rough, CONSERVATIVE proxy, not an
# exact dollar figure: contracts are priced 1-99 cents and a single
# contract's max payout/loss is $1, so "contract count" only equals actual
# dollars at risk in the worst case where every contract simultaneously
# moves against us at $1 -- real dollar exposure at any given moment is
# typically much less than contract_count * $1. Same approximation the
# real per-market allocations above use, just as a single flat number.
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
# Google News' public RSS search (no key required) is polled for recent
# articles whose title overlaps with keywords derived from each watched
# market's title/subtitle. This is a coarse rule-based heuristic, NOT an
# NLP/LLM classifier -- see news_feed.py for the scoring logic and its
# documented limitations. Set NEWS_FEED_ENABLED = False to disable this
# signal entirely (e.g. to A/B test against the volatility-only baseline).
#
# NOTE (2026-09-02): originally pointed at GDELT's Doc 2.0 API, which
# turned out to be completely unreachable from this network (confirmed
# via curl -- connect/read/SSL timeouts, while Kalshi's API and other
# HTTPS hosts worked fine) -- the signal had never actually worked.
# Replaced with Google News RSS, verified reachable end-to-end.
NEWS_FEED_ENABLED = True

NEWS_RSS_API_URL = "https://news.google.com/rss/search"

# How often to poll Google News RSS for each watched market. In seconds.
NEWS_POLL_INTERVAL_SEC = 300  # 5 minutes

# How far back (hours) each query looks for articles (filtered client-side
# by each article's pubDate -- see news_feed.py's _parse_articles).
NEWS_LOOKBACK_HOURS = 6

# Max number of keywords derived from a market's title/subtitle, used to
# build the news query and to compute keyword-overlap relevance.
NEWS_MAX_KEYWORDS = 8

# Fraction of a market's derived keywords that must appear in an article's
# title/theme tags for that article to be flagged "relevant". Coarse by
# design -- see news_feed.py's score_relevance() docstring.
NEWS_RELEVANCE_THRESHOLD = 0.2

# How long (minutes) a relevant news hit keeps the risk engine's
# news_signal active before it decays back off.
NEWS_SIGNAL_DECAY_MINUTES = 45

# --------------------------------------------------------------------------
# Event-triggered market switcher (opt-in, off by default)
# --------------------------------------------------------------------------
# Deliberately NOT a continuous re-optimizer: stays in currently-watched
# markets by default, and only searches for a replacement when the
# CURRENT market gives a sustained, clear signal to leave. Runtime-only --
# does not rewrite WATCHED_MARKET_TICKERS above; a restart reverts to
# whatever's configured there. See market_switcher.py.
MARKET_SWITCHER_ENABLED = True

# Real resting depth (both sides, real book only) relative to
# target_size, above which a CURRENTLY watched market is considered
# "crowded" -- diminishing marginal share for a new entrant. 1.0 means
# real depth alone already equals the qualification threshold.
CROWDING_RATIO_THRESHOLD = 3.0

# How long (seconds) a trigger condition (HIGH risk or crowded) must hold
# CONTINUOUSLY before acting -- avoids switching on a single noisy tick.
SWITCH_TRIGGER_SUSTAIN_SEC = 300  # 5 minutes

# Minimum time (seconds) between any two switches, regardless of how many
# tickers are triggering at once -- "does not need to be switching
# rapidly, only when needed."
MARKET_SWITCH_COOLDOWN_SEC = 1800  # 30 minutes

# How often (seconds) the switcher checks trigger conditions. Doesn't need
# to be as tight as the ~1s scoring cadence, since triggers require
# multi-minute sustained conditions anyway.
MARKET_SWITCH_CHECK_INTERVAL_SEC = 30

# --------------------------------------------------------------------------
# Scheduled-event calendar awareness (feeds each watched market's REAL
# close/resolution time into the risk engine as a proximity flag -- see
# event_calendar.py). Independent of the news-awareness signal above:
# this is deterministic (Kalshi's own close_time), not a keyword heuristic.
# --------------------------------------------------------------------------
EVENT_CALENDAR_ENABLED = True

# close_time barely changes once a market is listed -- no need to poll
# anywhere near scoring_loop's ~1s cadence.
EVENT_CALENDAR_POLL_INTERVAL_SEC = 3600  # 1 hour

# A watched market within this many hours of its real close/resolution
# time gets nudged toward ELEVATED risk (event-driven moves/settlement
# mechanics can behave differently than ordinary trading). A market
# already PAST close_time (negative hours) is NOT flagged -- it's settled
# or near-settled, which is safe, not risky (matches the existing
# KXEOWEEK "likely near-settled" thesis in WATCHED_MARKET_TICKERS above).
EVENT_CALENDAR_WARNING_HOURS = 48

# --------------------------------------------------------------------------
# Crowding-aware market screening (opt-in; used by incentive_screener.py's
# manual "python3 incentive_screener.py" workflow -- see README Step 1 --
# NOT the always-running pipeline's incentive_refresh_loop/main.py, so it
# never adds REST call volume to the live run).
# --------------------------------------------------------------------------
# How many of the cheap reward-density-ranked candidates get a live
# crowding/volatility check (one get_orderbook + one get_trades call
# each). Keeps live-call volume bounded regardless of how many active
# programs exist -- mirrors market_switcher.find_replacement's same
# bounded-candidate-check pattern.
SCREENER_LIVE_CHECK_POOL_SIZE = 15

# Number of recent trades used to estimate a candidate's recent
# volatility (stdev of trade prices, in cents) during crowding-aware
# screening.
SCREENER_VOLATILITY_LOOKBACK_TRADES = 50

# --------------------------------------------------------------------------
# Dashboard performance history (server.py)
# --------------------------------------------------------------------------
# How far back the performance/balance chart looks, and at what
# resolution -- downsampled (last sample per bucket, not a raw row cap),
# so the chart stays meaningful across a multi-day/multi-week run
# instead of only covering the last few minutes (see server.py's
# _performance_history). 336h * 3600 / 300 = at most ~4032 chart points
# regardless of how long main.py has been running or how many markets
# it has watched over that time.
PERFORMANCE_HISTORY_LOOKBACK_HOURS = 336  # 14 days
PERFORMANCE_HISTORY_BUCKET_SECONDS = 300  # 5 minutes

# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
DB_PATH = DATA_DIR / "paper_trading.sqlite"

DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)
