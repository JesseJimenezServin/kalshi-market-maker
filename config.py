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

# --------------------------------------------------------------------------
# Displacement (trend) risk -- added after the 2026-10-06 incident
# --------------------------------------------------------------------------
# The stdev measure above is blind to a large move delivered smoothly. On
# 2026-10-06 KXTRUMPAPPROVE-26OCT06-E38.5 repriced 7.5c -> 84.5c (11x) and
# volatility_level read LOW in 12280 of 12415 snapshots (99.1%), so the
# quote was never pulled: at 15:36, mid 84.50, we were still showing
# YES 100@81c / NO 100@13c. Realised loss -$132.54, 13% of capital in ~3h.
#
# The reason is structural, not a bad threshold: stdev of TICK-TO-TICK
# changes measures CHOPPINESS. A trend, or a gap between samples, produces
# small per-tick changes and so reads calm. Displacement has to be measured
# separately, as the peak-to-trough RANGE over a trailing window.
#
# Measured in CENTS, not percent, because P&L is linear in cents: a
# contract's loss is net_contracts x price_move. An earlier attempt to
# normalise by distance-to-bound was discarded during calibration -- it made
# penny markets look explosive and a NORMAL market scored higher (3.33) than
# the event market (2.14).
#
# Thresholds calibrated against the event and six normal markets
# (~260k snapshots), trailing 300s range:
#       threshold   event market   normal markets
#         >= 8c         4.9%          0.0% - 1.2%
#         >= 15c        1.4%          0.0%  (all six)
# 15c gives clean separation, which matters because HIGH pulls the quote and
# a pulled quote earns no incentive reward -- false positives cost revenue.
#
# HONEST LIMITATION: this is REACTIVE, not predictive. On the 10-06 event it
# first crossed 15c at 12:48:07, by which point P&L was already -$78.35 of
# the eventual -$132.54. It would have prevented roughly $54 of $132 (41%),
# not the whole loss. Worth having; not a solution on its own.
DISPLACEMENT_WINDOW_SECONDS = 300.0
DISPLACEMENT_ELEVATED_THRESHOLD = 8.0
DISPLACEMENT_HIGH_THRESHOLD = 15.0

# Once displacement triggers HIGH, hold HIGH for at least this long.
#
# Without the hold, HIGH lasted EXACTLY one window and then reset, because
# a rolling range cannot see a sustained trend: the big move scrolls out of
# the 300s window and each later slice contains only the incremental step.
# Measured 2026-10-07 on KXTRUMPAPPROVE-26OCT07-E38.5: 10990 snapshots,
# 2 HIGH episodes, both exactly 303s long. The mid fell 59c -> 6c; HIGH
# fired correctly near the start (P&L +$0.78), expired 303s later while the
# price was still collapsing, the quote came back on, and the position was
# adversely filled down to -$75.73. The signal was timely and still
# protected nothing.
#
# Calibrated on how far the mid travels AFTER a trigger:
#       market            +5m   +10m  +15m  +30m  +60m
#       OCT06-E38.5      27.0  28.0  28.2  28.2  28.2
#       OCT07-E38.5       4.5   4.5   5.0  39.5  39.5   <- the $69 loss
#       NCAAFDELAY        6.2   6.5   6.8   7.0  10.5
#       OCT07-E38.4      53.0  53.0  53.0  53.0  53.0
# Most danger lands within 5-15 min, but OCT07-E38.5 -- the market this
# fix exists for -- did most of its damage at the 30 MINUTE mark. A 15-min
# hold would have missed it.
#
# Cost, as % of snapshots with the quote pulled:
#       calm markets (4):  0.0% at EVERY hold duration
#       event markets:     5.6% / 6.4% / 15.3% / 0.2% at 30m
#                          7.8% / 9.7% / 28.4% / 0.2% at 60m
# 30 min is free on quiet markets and costs 6-15% of quoting time on ones
# that genuinely moved -- where quoting is what loses the money. 60 min
# roughly doubles the cost for almost no extra coverage.
#
# Only HIGH latches. ELEVATED merely widens rather than pulling, so it is
# left instantaneous.
# RE-ENABLED 2026-10-09 once flatten_position gained a slippage bound.
#
# Briefly set to 0 after the latch tripled exposure to a pre-existing
# defect: HIGH both pulls the quote AND arms hard_stop_flatten, which
# crossed the spread at ANY price. Holding HIGH ~6x longer took the
# forced-flatten rate from 0.15/h to 0.45/h and the average exit price from
# 54.7c to 86.1c -- 322-404 contract positions dumped into a 1-cent bid.
# P&L went -$244 -> -$707 in 28.6h.
#
# The latch was never the defect; it was correct behaviour sitting on top
# of a broken exit path. With MAX_FLATTEN_SLIPPAGE_CENTS in place the
# ruinous exits are refused, so the latch can do its job: keep the quote
# out of a trending market without the exit path turning that into a
# realised loss.
DISPLACEMENT_HIGH_HOLD_SECONDS = 1800.0

# Thresholds on abs(net_yes_contracts) / max_inventory that escalate risk level.
INVENTORY_FRACTION_ELEVATED_THRESHOLD = 0.4
INVENTORY_FRACTION_HIGH_THRESHOLD = 0.7

# At HIGH risk, if our inventory fraction is at/above this, actively flatten
# (cross the spread) instead of just pulling the quote.
HARD_STOP_INVENTORY_FRACTION = 0.8

# Maximum slippage (cents away from the mid) we will pay to cross out of a
# position. Beyond this, flatten_position REFUSES and the position is held.
#
# Added 2026-10-09 after forced exits did more damage than the moves they
# were protecting against. flatten_position crossed at whatever the best
# resting bid happened to be, with no price bound at all, so on a thin book
# it dumped 322-404 contracts into a 1-cent bid. Measured over 44 historical
# flattens, slippage vs the mid at that moment:
#       p50  2.5c    p75  6.0c    p90 26.5c    max 51.5c    mean 7.2c
# There is a clean break: everything is <= 7.5c or >= 10c, and the tail runs
# 15c, 25c, 26.5c, 26.5c, 39.5c, 41c, 51.5c. 10c keeps every normal exit
# (p75 is 6c) and blocks exactly that tail -- 6 of 44 flattens, the ones
# that cost real money.
#
# Refusing is safe BECAUSE of two other properties: the hard stop only fires
# at HIGH risk, where compute_quote already returns None, so a held position
# cannot grow; and settlement accounting (added 10-04) now resolves a held
# position at its true 0/100 outcome rather than freezing a stale mark.
# Paying 51c of spread to avoid a contract that can only move 8c further is
# not risk management.
MAX_FLATTEN_SLIPPAGE_CENTS = 10.0

# Quote widening (in cents/ticks off best bid, after inventory skew is
# applied) when risk_level == ELEVATED.
WIDEN_TICKS_ELEVATED = 2

# Maximum number of cents/ticks the quoting policy will skew yes/no prices
# away from the touch in response to inventory, regardless of how large
# net_yes_contracts / max_inventory becomes.
MAX_INVENTORY_SKEW_TICKS = 5

# --------------------------------------------------------------------------
# Proactive exit mechanics (take-profit, time-decay, refined hard-stop)
# --------------------------------------------------------------------------
# Once we hold inventory, the exit-side quote (NO if long yes, YES if
# short yes) is never rested more aggressively than the price implying
# at least this many cents of profit per contract vs. avg_entry_price.
# Still a passive limit price (maker) -- this only constrains WHERE we
# rest, never crosses the spread on its own. Previously the inventory
# skew had no connection to actual entry price at all, so the "unwind"
# side could rest at a price that would be a LOSS if filled.
TAKE_PROFIT_MARGIN_CENTS = 4

# Time-based pre-resolution exit (see event_calendar.py for close_time).
# Starting this many minutes before a market's real close/resolution
# time, TAKE_PROFIT_MARGIN_CENTS is gradually relaxed toward 0 as time
# runs out (see quoting_policy.effective_take_profit_margin) -- we
# stop holding out for a full profit margin we're unlikely to get once
# resolution is imminent. Below CRITICAL_EXIT_MINUTES, any remaining
# inventory is actively flattened (crossing the spread, same mechanics
# as the HIGH-risk hard stop) REGARDLESS of current risk_level, so we
# never carry a position into a market's binary resolution.
EXIT_BEFORE_RESOLUTION_MINUTES = 60
CRITICAL_EXIT_MINUTES = 15

# Refined hard-stop threshold for small max_inventory allocations: the
# HIGH-risk hard stop now fires at
# max(HARD_STOP_INVENTORY_FRACTION * max_inventory, DEFAULT_SIMULATED_QUOTE_SIZE + this)
# contracts, not a pure fraction of max_inventory -- otherwise a market
# with a small allocation (e.g. a $150 cap against a 100-contract quote
# size) hits "80% of cap" after a single fill, forcing an immediate,
# unwanted stop-loss before the position ever had a chance to work.
HARD_STOP_MIN_BUFFER_CONTRACTS = 50

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

# Whether news_signal feeds the composite risk_level at all.
#
# FALSE as of 2026-10-06, because it was measured to have NO predictive
# value for the risk it was supposed to warn about. P(mid displacement
# >= 8c within 30 min) after a news hit vs at a random time, per market:
#       OCT06-E38.5   11.7%  vs  14.5%
#       OCT06-E38.4    5.2%  vs   7.5%
#       OCT06-E38.6    3.1%  vs   4.1%
#       OCT05-E38.2    0.0%  vs   0.0%
#       OCT05-E38.4    0.0%  vs   0.0%
#       OCT04-E38.1   18.4%  vs  20.8%
# A news hit made a move LESS likely than a coin-flip moment, in every
# single market. Combined with firing 61.2% of the time, it was not a
# warning -- it was a constant that pinned risk_level to ELEVATED and
# widened every quote for nothing.
#
# news_events collection and the news_signal field are DELIBERATELY KEPT:
# the raw articles are research material for a better relevance heuristic
# later, and the dashboard still shows the flag. Only its effect on
# risk_level is removed. Re-enable once relevance is selective enough to
# beat the baseline above.
NEWS_SIGNAL_AFFECTS_RISK = False

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
# target_size, above which a CURRENTLY watched market is worth
# SEARCHING for a replacement over -- 1.0 means real depth alone
# already equals the qualification threshold. This is now only a loose
# pre-filter (avoids spending REST calls searching when the current
# market is obviously fine); it does NOT by itself decide whether a
# switch actually happens -- see CROWDING_RELATIVE_IMPROVEMENT_FACTOR
# below and market_switcher.is_worthwhile_crowding_switch.
#
# NOTE (2026-09-30): previously this constant ALONE decided whether a
# switch happened. CONFIRMED live: once real market-wide crowding rose
# well above 3.0 across EVERY currently active incentive program
# (observed range ~5.6x-30x+, vs. ~8.9x least-crowded when 3.0 was
# chosen on 2026-09-02), every watched ticker re-triggered "crowded"
# the instant its cooldown cleared -- 50 switches in 24.5 hours,
# averaging exactly MARKET_SWITCH_COOLDOWN_SEC apart. A fixed absolute
# number goes stale as real conditions drift; the relative check below
# doesn't.
CROWDING_RATIO_THRESHOLD = 3.0

# The ACTUAL deciding factor for a "crowded" trigger: the best
# replacement market_switcher.find_replacement finds must have a real
# crowding ratio at/below this fraction of the CURRENT market's
# crowding ratio to be worth switching into -- e.g. 0.5 means the
# replacement must be at least half as crowded. Self-adjusts to
# whatever "normal" crowding looks like at any given time (rising or
# falling), unlike a fixed absolute number. Not applied to a
# "volatile" (HIGH risk) trigger -- that's about escaping OUR OWN risk
# exposure, not chasing lower crowding.
CROWDING_RELATIVE_IMPROVEMENT_FACTOR = 0.5

# A "crowded" switch must not cost us more revenue than the crowding is
# worth. The replacement's reward density must be at least this fraction of
# the market we are leaving, or we stay put.
#
# Why (measured 2026-10-05): the crowding trigger guarded crowding and
# nothing guarded revenue, so the bot left Trump approval markets worth an
# estimated $864/day/market for NFL game markets worth $13/day/market --
# purely because they were less crowded. 30.3 market-hours there cost an
# estimated $1,074 of foregone reward, which dwarfs the entire trading P&L
# the run has produced.
#
# 0.5 mirrors CROWDING_RELATIVE_IMPROVEMENT_FACTOR: we accept giving up
# some revenue to escape genuine crowding, but not an order of magnitude.
# Crowding relief is only worth having if there is still a pool to win.
MIN_REPLACEMENT_REWARD_DENSITY_FRACTION = 0.5

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
# Calibrated 2026-10-06 against real displacement, after discovering this
# signal was active 60.0% of the time and (with news) responsible for 79.9%
# of all ELEVATED risk while volatility and inventory were both LOW.
#
# The cause was that 48h is WIDER THAN THE ENTIRE LIFE of the markets being
# traded: the Trump approval contracts run on a 24-hour cycle, so every
# snapshot was inside the window and the signal could never turn off. An
# always-on risk input is indistinguishable from no input -- except that
# ELEVATED widens the quote by WIDEN_TICKS_ELEVATED, so it was quietly
# costing reward score (quoting away from the Reference Price lowers the
# distance multiplier) for no risk information in return.
#
# Measured: 300s mid displacement, bucketed by hours-until-close, across six
# markets:
#       0-1h    0.0% of snapshots >= 8c   (book already emptying out)
#       1-2h   11.3%
#       2-4h   10.7%
#       4-8h    5.1%
#       8-24h   0.4%
#       24-48h  0.0%
# Risk is concentrated in the 1-4h window. 4h keeps both high-risk buckets
# plus margin and excludes the 8-48h region that caused the always-on
# behaviour.
EVENT_CALENDAR_WARNING_HOURS = 4.0

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
# Dead-market detection
# --------------------------------------------------------------------------
# How long BOTH sides of a market's book must stay empty before we treat it
# as dead (settled/closed) and force a switch out of it.
#
# Why this exists (2026-10-04): the three watched markets settled at
# 16:59 UTC. A settled market has no resting size, and
# compute_crowding_ratio reads "almost no competing size" as "wonderfully
# uncrowded" -- the incumbent scored crowding=14.79 against the best
# replacement's 120.63, so is_worthwhile_crowding_switch refused to move
# and the bot sat on three finalized markets. It produced no scores,
# quotes or inventory marks for 4.4 hours while still looking healthy to
# systemctl, and wrote ~229,000 empty orderbook rows per day.
#
# Crowding, risk and reward all degrade GRACEFULLY toward "attractive" as
# a book empties, so none of them can detect this. Liveness has to be its
# own check.
#
# 120s is comfortably longer than any transient empty book during normal
# trading (a real market refills within seconds) and short enough to lose
# only minutes rather than hours.
DEAD_MARKET_EMPTY_BOOK_SECONDS = 120.0

# --------------------------------------------------------------------------
# P&L measurement epoch -- the Phase 0 reset
# --------------------------------------------------------------------------
# The dashboard's P&L and balance series count only inventory_marks at or
# after this timestamp, so the reported balance restarts from
# STARTING_CAPITAL_USD.
#
# Set 2026-10-13, closing out Phase 0 (see AUTOPSY.md). The -$746.73 that
# preceded it is NOT a measurement of this strategy: $1,267 of it was
# slippage from an unbounded exit path since fixed, $161 was fictitious
# profit from phantom order book levels, and the run spanned six
# behaviour-changing deploys with a longest consistent window of ~28 hours.
# Carrying that balance forward would contaminate every future number with
# defects that no longer exist.
#
# NOTHING IS DELETED. This is the same convention the project already uses
# for pre-run noise (ts >= 1790836411) and CONTAMINATED_TICKERS: a
# documented filter, not destructive cleanup. All 14 days remain queryable,
# and the order book history is REQUIRED as input to offset_sweep.py.
PNL_EPOCH_TS = 1791849600.0  # 2026-10-13 00:00:00 UTC

# --------------------------------------------------------------------------
# Contaminated markets (excluded from dashboard P&L / reward reporting)
# --------------------------------------------------------------------------
# These markets were watched while the orderbook.py float-residue bug was
# live (fixed 2026-10-03, see SIZE_EPSILON there and the INCIDENT entry in
# PROJECT_STATUS.md). Their books were 13-60% phantom levels, so the best
# bid, the mid, the quote placement, the simulated fills and the P&L mark
# were all derived from liquidity that did not exist. They account for
# $161.06 of the $162.04 "profit" the dashboard was reporting; every market
# with a clean book made approximately nothing.
#
# The ROWS ARE DELIBERATELY NOT DELETED -- they are the evidence for that
# incident, and this codebase already prefers documented filters over
# destructive cleanup (cf. the ts >= 1790836411 filter for pre-run testing
# noise). They are excluded at report time instead.
#
# Threshold was >10% phantom levels, which separated cleanly: the six below
# are 13.4-60.3%, the next worst is 5.3%. The kept markets still carry
# ~$0.99 of P&L measured against books with 0.1-5.3% phantoms, so treat
# small non-zero P&L from before 2026-10-03 as noise rather than signal.
CONTAMINATED_TICKERS: frozenset[str] = frozenset({
    "KXTRUMPAPPROVE-26OCT02-E38.8",  # 60.3% phantom, $9.60
    "KXTRUMPAPPROVE-26OCT02-E38.5",  # 46.9% phantom, $132.32
    "KXTRUMPAPPROVE-26OCT02-E38.6",  # 39.2% phantom, $18.90
    "KXTRUMPAPPROVE-26OCT03-E38.0",  # 38.1% phantom, $0.18
    "KXTRUMPAPPROVE-26OCT03-U38.0",  # 35.3% phantom, $0.02
    "KXTRUMPAPPROVE-26OCT03-A38.6",  # 13.4% phantom, $0.04
})

# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"

# DB_PATH defaults to the database main.py writes on whichever box it runs
# on. KALSHI_DB_PATH overrides it so a dashboard can be pointed at a
# different, already-populated database -- specifically, a snapshot pulled
# down by pull_droplet_backup.sh. Without this, running server.py on a
# laptop while main.py runs on the Droplet silently serves whatever stale
# local data/paper_trading.sqlite happens to exist (on 2026-10-02 that was
# a run that had stopped on 09-05, so the dashboard showed 3 simulated
# fills while the Droplet was at 2494 -- see PROJECT_STATUS.md).
# Leave it unset on the box that actually runs main.py.
_DB_PATH_OVERRIDE = os.getenv("KALSHI_DB_PATH", "").strip()
DB_PATH = (
    Path(_DB_PATH_OVERRIDE).expanduser()
    if _DB_PATH_OVERRIDE
    else DATA_DIR / "paper_trading.sqlite"
)

DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)
