# Kalshi Liquidity Lab -- Project Status

git identity configured for contribution tracking (2026-09-02): repo-level
user.name/user.email set to JesseJimenezServin / nuvusbusiness@gmail.com.

## Purpose
Paper-trade (zero capital risk) the Kalshi Liquidity Incentive Program to
determine whether a basic two-sided quoting strategy earns more in reward
income than it loses to adverse selection, on which markets, before any
real capital or live orders are involved.

## Current Phase: Phase 0 (paper trading / simulation, read-only)
Status: core pipeline built and wired correctly. Not yet run for a
sustained data-collection window.

## Environment
- macOS, Python 3.13, repo at ~/Downloads/kalshi_liquidity_lab
- Kalshi demo environment, RSA-PSS auth verified working
- GitHub: JesseJimenezServin/kalshi_liquidity_lab (private)
- .env and private_key.pem are gitignored; .env now loaded via
  python-dotenv (fixed 2026-08-31)

## Architecture (file map)
- config.py -- environment, watch list, thresholds, loads .env
- auth.py -- RSA-PSS request signing
- rest_client.py -- REST wrapper, GET-only, no order-placement methods
- orderbook.py -- local order book from WS snapshot + deltas, gap/resync
- ws_client.py -- authenticated WebSocket client (orderbook_delta + trade)
- incentive_screener.py -- ranks active Liquidity Incentive Program markets
- reward_simulator.py -- implements Kalshi's actual snapshot scoring
  formula (Reference Price, Discount Factor, per-side share)
- adverse_selection_sim.py -- simulates hypothetical inventory + P&L from
  real trade flow against a hypothetical resting quote
- logger.py -- SQLite persistence (snapshot_scores, simulated_fills,
  inventory_marks, orderbook_snapshots, incentive_program_snapshots,
  simulated_stop_losses, news_events, quote_snapshots, market_switches
  tables)
- risk_engine.py -- per-market RiskEngine: rolling mid-price volatility +
  inventory fraction -> composite LOW/ELEVATED/HIGH risk level
- quoting_policy.py -- active quoting policy: rests at the live Reference
  Price (not just the touch) before applying inventory skew, widen/pull
  on risk escalation, a take-profit floor on the exit side (relaxed as
  a market's resolution nears), and proactive exit triggers -- HIGH-risk
  hard stop (refined threshold for small allocations) and a
  time-based forced exit near resolution -- that actively flatten
  (cross the spread via adverse_selection_sim.InventoryState, still no
  real orders)
- capital_allocator.py -- splits config.STARTING_CAPITAL_USD across the
  currently-watched markets, weighted by profit/risk (reusing
  incentive_screener's adjusted_score), bounded to [MIN_ALLOCATION_FRACTION,
  MAX_ALLOCATION_FRACTION] per market via a proper bounded-proportional
  ("waterfilling") split. Sets each MarketRuntimeState.max_inventory --
  the existing hard position-size cap -- instead of the single flat
  config.MAX_SIMULATED_INVENTORY constant it replaces.
- market_switcher.py -- event-triggered market switcher: pure building
  blocks (crowding ratio, sustain+cooldown trigger logic, candidate
  search) -- NOT a continuous re-optimizer, opt-in via
  config.MARKET_SWITCHER_ENABLED (now True -- see 2026-09-02 entry
  below). Orchestration loop lives in main.py (market_switch_loop) to
  avoid a circular import with MarketRuntimeState.
- news_feed.py -- zero-cost news-awareness signal: polls Google News'
  public RSS search (no key required, replaced GDELT 2026-09-02 --
  see milestones below), rule-based keyword-overlap relevance scoring
  gated on a distinctive (capitalized) keyword (no LLM), feeds a
  decaying news_signal into RiskEngine
- event_calendar.py -- scheduled-event calendar awareness: fetches each
  watched market's REAL close/resolution time (rest_client.get_market)
  and feeds hours_until_close into RiskEngine as a proximity flag (nudges
  toward ELEVATED within config.EVENT_CALENDAR_WARNING_HOURS). Unlike
  news_signal, needs no decay window -- self-correcting from close_time
  each poll. Opt-in via config.EVENT_CALENDAR_ENABLED (default True).
- main.py -- orchestrates all of the above; NO live orders are ever
  placed anywhere in this codebase (verified via grep, 2026-08-31)
- server.py -- read-only FastAPI dashboard; streams data/paper_trading.sqlite
  over /ws once per second, never talks to Kalshi
- templates/dashboard.html -- single-page dashboard UI (markets table w/
  risk badge + qualifying-snapshot counts + net position, order book
  depth bars with the hypothetical quote overlaid, recent fills,
  performance chart, flash highlight on new fills/stop-losses, capital
  allocation bars, portfolio balance chart -- see 2026-09-02 below)
- analysis.py -- read-only report: ESTIMATED dollar reward per ticker
  (snapshot_scores joined against incentive_program_snapshots) printed
  alongside real adverse-selection P&L from inventory_marks. No simulated
  cash balance in THIS report specifically -- P&L is already tracked
  correctly in cents (the dashboard's balance chart, added 2026-09-02,
  is a separate, purely additive visualization of the same P&L data,
  not a new ledger).
- singleton_lock.py -- structural protection so only one instance of
  main.py or server.py can ever run at once (see Decisions log 2026-09-01)
- manage.sh -- start/stop/restart/status for main.py + server.py via
  PID files -- use this instead of hand-typed nohup/pkill commands
- test_logic.py -- offline unit tests, no network required, 50/50 passing

## Completed milestones
- 2026-08-31: Initial Phase 0 build: all modules above, 7 unit tests passing
- 2026-08-31: Real target_size/discount_factor wired from incentive_screener
  into main.py's MarketRuntimeState (replaced placeholder 1000.0/0.9)
- 2026-08-31: Real trade-time "my share" calculation using live book depth
  (replaced placeholder 0.15 in make_trade_handler)
- 2026-08-31: .env loading fixed via python-dotenv
- 2026-08-31: Added explicit GET-only guard (ALLOWED_METHODS assertion) in
  rest_client.py + config.is_read_only_mode() startup log, ahead of moving
  Phase 0 data collection to production
- 2026-08-31: Built server.py (FastAPI) + templates/dashboard.html -- a
  read-only live dashboard streaming data/paper_trading.sqlite over /ws
  every 1s (markets table, order book depth, recent fills, performance
  chart). Extended logger.py schema with orderbook_snapshots and
  incentive_program_snapshots tables (main.py now logs both) so the depth
  panel and "Daily Pool ($)" column reflect real persisted data rather
  than being fabricated in the UI layer.

## Active milestone
- 2026-08-31: Upgraded Phase 0 from a naive static quoting policy to an
  active strategy simulation, still fully read-only/paper-trading. New
  risk_engine.py tracks rolling mid-price volatility + inventory fraction
  into a LOW/ELEVATED/HIGH risk level per market. New quoting_policy.py
  skews the hypothetical quote toward reducing inventory, widens it at
  ELEVATED risk, pulls it entirely at HIGH risk, and simulates a hard-stop
  flatten (crossing the spread via adverse_selection_sim.InventoryState)
  once inventory crosses a configurable threshold at HIGH risk -- logged
  to a new simulated_stop_losses table, distinct from passive fills.
  Wired into main.py's scoring_loop; reward_simulator.py's scoring math
  and adverse_selection_sim.py's fill/inventory math were not touched.
  3 new unit tests added (12/12 passing); re-verified via grep that no
  order-placing code exists anywhere in the codebase.
- 2026-08-31 (completed): Added a zero-cost, purely advisory news-awareness
  signal. New news_feed.py polls GDELT's free Doc 2.0 API (no key, no
  cost) for recent articles matching keywords derived from each watched
  market's title/subtitle, and flags "relevant" articles via a coarse
  rule-based keyword-overlap score (no LLM, no paid service anywhere).
  RiskEngine gained a news_signal field + register_news_event(), which
  decays after a configurable window and nudges risk level up (toward
  ELEVATED) alongside volatility and inventory -- direction/confidence-
  free by design, given the heuristic's limited precision. New
  news_events SQLite table for after-the-fact review. NEWS_FEED_ENABLED
  config flag allows disabling this signal to A/B test against the
  volatility-only baseline. 2 new unit tests added (14/14 passing);
  re-verified via grep that no order-placing code exists anywhere.
  News integration is zero-cost (GDELT + keyword heuristic, no LLM
  classification, no paid APIs). Quality is a known limitation vs. a
  true NLP/LLM classifier -- flagged as a future upgrade path if
  paper-trading data suggests the heuristic is too noisy or misses real
  signal. CAVEAT: this sandbox's network doesn't reach api.gdeltproject.org
  (same restriction as Kalshi's own API), so the GDELT/Kalshi
  request-response parsing in news_feed.py has not been exercised
  against a live call -- verify field names on your own machine before
  trusting this signal.
- 2026-09-01 (completed): Added analysis.py -- prints an ESTIMATED dollar
  reward per watched ticker (avg combined_score x period_reward x
  fraction of "clean" snapshots where both sides qualified) alongside the
  real, already-tracked-in-cents realized/unrealized P&L from
  inventory_marks, so both sides of "does reward beat adverse selection"
  are visible together. Deliberately did NOT add a simulated cash
  balance -- P&L is already correct in cents; a balance would be
  redundant, not new information. The reward figure is an explicit
  optimistic-upper-bound ESTIMATE (see "Known simplifications" below),
  not a guaranteed number -- does not change any simulation logic, purely
  reads existing SQLite tables.
- 2026-09-01 (completed): Dashboard now visualizes the hypothetical quote
  directly against the real order book, plus live score/inventory/
  risk-level readouts, so continuous reward-eligibility is visible even
  when simulated_fills stays at 0. New quote_snapshots SQLite table
  (main.py logs risk level + current quote, or NULLs when pulled, every
  tick -- pure instrumentation, no simulation-logic change) since
  server.py and main.py are separate processes with no shared memory.
  Watched Markets table gained Risk badge, Qualifying/Total snapshot
  count, Net Position, and Unrealized P&L columns. Order Book Depth panel
  overlays our hypothetical yes/no quote as a dashed purple "OUR QUOTE"
  marker on the real depth (inserted as a zero-size row if our price
  doesn't coincide with existing depth); grayed out at ELEVATED risk,
  absent entirely at HIGH (quote pulled). New simulated_fill/
  simulated_stop_loss events flash-highlight that market's table row for
  ~2.5s. Verified against live data end-to-end, plus a synthetic-data
  Node harness (no browser available in this environment) covering the
  HIGH/ELEVATED/skewed-quote/flash paths real data hadn't naturally
  exercised yet. Purely additive/visualization -- no simulation logic or
  order-placing code touched, re-verified via grep.
- 2026-09-01 (completed): Fixed analysis.py's estimated-reward figure,
  which was projecting ~1 hour of observed data straight across an
  ENTIRE 14-day, $50,000 program period with zero time-scaling (e.g. one
  ticker showed a ~$6,900 "estimate" off ~1 hour of data). Added
  start_date to ScreenedProgram (incentive_screener.py) and a
  total_period_days column to incentive_program_snapshots (logger.py, w/
  in-place ALTER TABLE migration since the table already existed on
  disk), and split the output into `EstSoFar` (properly prorated by
  hours_observed / total_period_hours -- the honest "what has this earned
  so far" answer) vs. the old figure, relabeled `IfFullPeriod` (a
  forward-looking projection only, explicitly not something already
  earned). Same two tickers now show $6.91 and $25.33 "so far" instead of
  $1,884 and $6,905 -- in line with what a ~1hr sample should plausibly
  support. New regression test for ScreenedProgram.total_period_days;
  16/16 tests passing.
- 2026-09-02 CORRECTION (no code change): re-examined reward_simulator.py's
  score_hypothetical_order after describing it in conversation as having a
  "my_share double-counting bug" -- that description does NOT match the
  actual code. The code comment there is stale/misleading (claims it
  "attributes ALL size at my_price...to me"), but the real line
  (`my_raw += my_size * mult`) only ever credits our own contribution;
  the denominator (total_raw) correctly includes everyone's size,
  including our own addition. This is NOT a double-counting bug --
  reward_simulator.py was NOT modified. If EstSoFar still looks high, the
  more likely explanation is that these are genuinely thin, under-served
  markets (exactly what the reward-density heuristic favors), where a
  modest 100-contract entrant can legitimately represent an outsized
  share -- not a numerator defect.
- 2026-09-02 (completed): Reference-Price-aware quoting. quoting_policy.py
  now rests at the live Reference Price (reward_simulator.compute_reference_price
  on the real book) instead of always joining the touch, before applying
  inventory skew/risk widening. Since price >= Reference Price always
  gets the full (undiscounted) multiplier, this gives whatever safety
  margin is genuinely free on a given market at a given moment (e.g. 4c
  observed live on KXNFLSTADIUM on 2026-09-01) at zero reward cost --
  markets where the touch alone already clears the threshold (e.g.
  KXCITYCHAMPS, Reference Price == touch) correctly get no free margin,
  rather than a fixed offset that would be free on one market and
  needlessly expensive (via discount_factor) on another. New regression
  test; 17/17 passing.
- 2026-09-02 (completed): Event-triggered market switcher (market_switcher.py),
  opt-in via config.MARKET_SWITCHER_ENABLED (default False, NOT yet
  enabled on the live run). Stays in currently-watched markets by
  default; only searches for a replacement when a CURRENTLY watched
  market sustains (config.SWITCH_TRIGGER_SUSTAIN_SEC, default 5 min)
  either HIGH risk or a crowding ratio (real resting depth vs.
  target_size) above config.CROWDING_RATIO_THRESHOLD, respecting a
  global config.MARKET_SWITCH_COOLDOWN_SEC (default 30 min) between
  switches -- deliberately not a continuous re-optimizer. Candidate
  replacements are pre-screened via a single live get_orderbook call
  (CONFIRMED shape 2026-09-02: {"orderbook_fp": {"yes_dollars": [...],
  "no_dollars": [...]}}) -- crowding can be checked this way without
  ever having watched a candidate; volatility can't (needs a time
  series), which is why crowding is the sole criterion for evaluating
  replacements. A switch flattens any open position first (reusing
  quoting_policy.flatten_position, extracted from the existing
  maybe_execute_stop_loss so both share identical crossing logic), then
  uses ws_client's new add_ticker/remove_ticker methods
  (CONFIRMED live 2026-09-02 against a standalone throwaway connection:
  Kalshi's update_subscription supports action="add_markets"/
  "delete_markets"). Runtime-only -- does not rewrite
  config.WATCHED_MARKET_TICKERS; a restart reverts to the configured
  list. New market_switches SQLite table for audit. 2 new tests (crowding
  ratio computation, sustain+cooldown trigger gating); 19/19 passing.
  NOT YET EXERCISED against a real trigger in production (still disabled
  by default) -- the sustain/cooldown logic is unit-tested with synthetic
  timestamps, not observed live.
- 2026-09-02 (completed): config.MARKET_SWITCHER_ENABLED flipped to True
  on the live run -- the switcher is now armed against real triggers for
  the first time (previously unit-tested only, per the entry above).
- 2026-09-02 (completed): Scheduled-event calendar awareness
  (event_calendar.py). Fetches each watched market's REAL close/
  resolution time via rest_client.get_market, computes hours_until_close,
  and feeds it to that ticker's RiskEngine (new set_event_proximity
  method) as a proximity flag -- nudges risk toward ELEVATED within
  config.EVENT_CALENDAR_WARNING_HOURS (default 48h), same one-notch-only
  severity as the existing news_signal. Deliberately does NOT flag a
  market already past its close_time (negative hours) -- that's settled/
  near-settled, which is safe, not risky, matching the existing KXEOWEEK
  thesis. Unlike news_signal's point-in-time decay window, this needs no
  artificial decay: hours_until_close is already a live, self-correcting
  quantity recomputed from Kalshi's own close_time every poll
  (config.EVENT_CALENDAR_POLL_INTERVAL_SEC, default hourly -- close_time
  essentially never changes once listed). New event_calendar_snapshots
  SQLite table for dashboard/audit visibility (pure instrumentation, same
  pattern as quote_snapshots). Wired into main.py's asyncio.gather as
  event_refresh_loop. CONFIRMED live 2026-09-02 on the first production
  restart after deploy: close_time's field name/ISO8601 format matched a
  real get_market() response (KXNFLSTADIUM -> 2028-09-01T03:59:00+00:00,
  KXCITYCHAMPS -> 2030-07-01T03:59:00+00:00; both correctly unflagged,
  far outside the 48h warning window).
- 2026-09-02 (completed): Crowding-aware market selection
  (incentive_screener.py). screen_incentive_programs gained an opt-in
  crowding_aware parameter (default False -- existing callers, i.e.
  main.py's incentive_refresh_loop, build_market_states at startup, and
  market_switcher.find_replacement, are completely unaffected and issue
  no extra REST calls): when True, the top
  config.SCREENER_LIVE_CHECK_POOL_SIZE (default 15) reward-density-ranked
  candidates each get one live get_orderbook call (real crowding ratio)
  and one live get_trades call (stdev of recent trade prices in cents, a
  coarse one-shot volatility proxy), combined into a new adjusted_score
  that dampens -- never disqualifies -- reward density for real crowding
  and choppiness. Missing/failed data is treated as neutral, not
  penalizing. Enabled explicitly in incentive_screener.py's __main__
  block (the manual "python3 incentive_screener.py" market-picking
  workflow -- README Step 1), NOT the always-running pipeline, so the
  live run's REST call volume/cadence is unchanged. print_report gained
  Crowd/VolC/Adj columns when crowding-aware results are passed in. 4 new
  tests (hours_until_close, RiskEngine event-proximity escalation,
  crowding/volatility estimators against a fake REST client,
  adjusted-score dampening); 23/23 passing. Re-verified via grep that no
  order-placing code exists anywhere.

## Known simplifications / open items
- analysis.py's `EstSoFar`/`IfFullPeriod` dollar rewards are OPTIMISTIC
  UPPER BOUNDS, not guaranteed figures: they assume the Time Period Score
  aggregates as a simple average of snapshot scores over however much of
  the window we've collected so far -- unverified against Kalshi's real
  methodology (see 2026-09-02 correction above -- my_share itself is
  NOT double-counting, this is a separate, still-open uncertainty about
  period-level aggregation). `EstSoFar` IS now properly prorated by
  elapsed time vs. the full program period (fixed 2026-09-01), but still
  assumes our observed average score is representative of the rest of the
  period, just time-scaled -- not adjusted for anything else (volatility
  regime changes, other LPs arriving/leaving, etc.).
- News-awareness signal is zero-cost (Google News RSS + keyword
  heuristic, no LLM classification, no paid APIs -- see the 2026-09-02
  GDELT swap below). Quality is a known limitation vs. a true NLP/LLM
  classifier -- expect false positives/negatives even after the
  distinctive-keyword gate fix; flagged as a future upgrade path if
  paper-trading data suggests it's still too noisy or misses real signal.
- "My share" of reward score is computed against the REAL live book
  (our contribution alone in the numerator, everyone's total including
  ours in the denominator -- verified 2026-09-02, not the double-counting
  bug this was previously mis-described as). What it can't model is
  competitive RESPONSE: other real participants might change their own
  behavior once we're resting there too, which no snapshot of the public
  book can tell us. True validation requires Phase 1 real fills, not
  fixable in pure simulation.
- No hosting/deployment yet -- main.py and server.py both currently run
  locally only.
- Stop-loss events (simulated_stop_losses) now flash-highlight their
  market's row in real time, but still don't appear as rows in the
  "Recent Simulated Fills" table itself -- that panel only reads
  simulated_fills. P&L chart is unaffected since it reads inventory_marks,
  which reflects stop-loss impact automatically.
- Both previously-planned workstreams (scheduled-event calendar
  awareness, crowding-aware market selection) are now built -- see the
  2026-09-02 milestone entries above. Their live run surfaced a real,
  pre-existing bug: rest_client.get_trades was hitting a nonexistent
  /markets/{ticker}/trades path (404 on every one of ~15 tickers
  checked, confirmed via curl against the real endpoint before fixing)
  instead of the real /markets/trades?ticker=... list endpoint. Fixed
  same day. This method predates this session and was apparently never
  exercised live before incentive_screener.py's volatility estimator
  called it -- main.py's own trade handling uses the WebSocket trade
  channel, not this REST method, so nothing else was affected.
- Dashboard's "Cumulative Reward Score" chart series is a unitless proxy
  (running sum of combined_score), not a dollar figure -- Kalshi's real
  USD payout pools every participant's score over the full period, which
  isn't reconstructable from public data. "Daily Pool ($)" reuses the
  existing reward_per_day heuristic (period_reward / days_remaining) from
  incentive_screener.py, not a literal daily-reset figure.

## Decisions log
- 2026-08-31: DigitalOcean's GitHub Student $200 credit was retired Aug 1 2026;
  using Azure for Students ($100 credit) for hosting instead once GitHub
  Education activates. Cost is trivial either way (~$6/mo on any provider).
- 2026-08-31: Demo environment has no Liquidity Incentive Program data (mock
  funds, not reflective of real markets). Phase 0 now reads production
  market data (read-only, unfunded account) instead. Added explicit
  GET-only guard in rest_client.py as a safety measure ahead of Phase 1.
- 2026-09-01 INCIDENT: main.py was "restarted" repeatedly across a work
  session using `nohup ... &` plus a hand-typed `pkill -f "python3 main.py"`
  that never actually matched the real process command line (the
  interpreter shows up as `Python`, not `python3`, once launched). Every
  restart silently left the old process running and started a new one
  alongside it -- by the time this was caught, 9 duplicate main.py
  processes had been running simultaneously for hours, all independently
  connected to Kalshi and all writing to the same SQLite file. This
  corrupted inventory_marks/simulated_fills/simulated_stop_losses for that
  entire window (multiple unaware simulations' rows interleaved, producing
  impossible-looking "flapping" position/P&L numbers on the dashboard).
  Response: backed up the contaminated DB (data/paper_trading_contaminated_
  backup_20260901_201057.sqlite), purged and vacuumed the live DB, killed
  all 9 duplicates, and added structural prevention: singleton_lock.py
  (main.py/server.py now refuse to start a second instance, checked
  before any network call -- verified by running `python3 main.py`
  directly while another instance was live; it exited immediately with a
  clear error) and manage.sh (start/stop/restart/status via PID files
  instead of guessed pkill patterns). Data collected before ~20:04 on
  2026-09-01 should be treated as unreliable for anything inventory/P&L
  related.

## 2026-09-02 INCIDENT (caught before the 2-week run, fixed same day)
Within minutes of enabling config.MARKET_SWITCHER_ENABLED=True on the live
run, both watched markets (KXCITYCHAMPS, KXNFLSTADIUM) sustained a
"crowded" trigger (real resting depth 12x/23x target_size -- far above
CROWDING_RATIO_THRESHOLD=3.0) with no qualifying replacement candidate
available (find_replacement kept returning None). Because only a
SUCCESSFUL switch reset the cooldown, the switcher re-ran its full search
(a paginated screen_incentive_programs call + up to 10 live get_orderbook
calls) every ~30-40s, indefinitely, instead of respecting
MARKET_SWITCH_COOLDOWN_SEC (30 min) -- observed live via repeated "No
replacement candidate found" log lines a few seconds apart. Left running
unattended for 2 weeks this would have hammered Kalshi's REST API
continuously and risked rate-limiting/IP throttling.
Response: added MarketSwitcher.record_search_attempt() (called after
ANY search attempt, successful or not) so a failed search now respects
the same cooldown as a successful switch. New regression test
(test_market_switcher_backs_off_after_failed_search_not_just_successful_switch);
24/24 tests passing. Restarted live; awaiting the next natural trigger
(requires the condition to sustain 5 more minutes post-restart) to
confirm live, though the unit test directly covers the fixed code path.
SEPARATE, STILL-OPEN finding from this same incident: both currently
watched markets are far too crowded by the Liquidity Incentive Program's
own criteria for a new entrant to earn a meaningful share right now --
worth reconsidering the watch list (via incentive_screener.py's new
crowding_aware mode) before or during the 2-week run, not just relying
on the switcher to find something better on its own (no candidate under
SWITCH_CANDIDATE_MAX_CROWDING_RATIO=1.0 was found among the top 10
reward-density candidates checked).

- 2026-09-02 (completed): Fully autonomous watch-list selection.
  incentive_screener.select_watchlist applies the crowding-aware ranking
  live at pipeline startup (not just the manual screening workflow),
  hard-filtering to candidates whose LIVE crowding ratio is at/below
  config.SWITCH_CANDIDATE_MAX_CROWDING_RATIO -- the SAME bar
  market_switcher.find_replacement requires of a later replacement, so
  day-0 selection and later mid-run switches are held to one consistent
  standard (missing/unmeasured crowding is excluded here, unlike
  screen_incentive_programs' general ranking, where it's neutral).
  main.py's new _select_initial_watchlist wires this in via
  config.AUTO_SELECT_WATCHLIST (default True, config.NUM_MARKETS_TO_WATCH
  markets), falling back to the manual WATCHED_MARKET_TICKERS list if
  auto-selection finds nothing eligible or is turned off. Runtime-only,
  same as the market switcher -- a restart re-runs selection from
  scratch, nothing is persisted back to config.py.
  2026-09-02 REVISION (same day, before this was ever run for real):
  the hard crowding cutoff described above was dropped entirely after
  live-checking it against real data -- EVERY currently active incentive
  program was already well-crowded (least-crowded observed ~8.9x
  target_size, most far higher), so the cutoff always returned []/None
  and neither autonomous selection nor market_switcher.find_replacement
  could ever actually pick anything. Both now always return the
  least-bad available candidate(s) ranked by adjusted_score (dampened,
  never disqualified, by crowding/volatility) instead of hard-excluding
  anything above an absolute threshold -- config.SWITCH_CANDIDATE_MAX_CROWDING_RATIO
  was removed as now-unused. select_watchlist only falls back to
  WATCHED_MARKET_TICKERS if there are zero active incentive programs at
  all, not merely because everything's crowded. 3 tests updated to match
  (fewer-than-requested and zero-programs cases, not a crowding-cutoff
  case); 27/27 passing.
  incentive_refresh_loop's incentive_program_snapshots logging now checks
  against the live `states` dict instead of the static
  WATCHED_MARKET_TICKERS list -- a real gap that would otherwise have
  silently stopped that table (and the dashboard's "Daily Pool ($)"
  column, and analysis.py's EstSoFar) from updating for any
  auto-selected or switched-into ticker. 3 new tests (crowding-filter +
  count-cap, empty-result case, fallback-on-nothing-eligible); 27/27
  passing. Re-verified via grep that no order-placing code exists
  anywhere. Together with the 2026-09-02 market-switcher fix above, the
  pipeline now both picks its own markets at startup and can switch away
  mid-run without any manual intervention.

- 2026-09-02 (completed): Profit/risk-weighted capital allocation
  (capital_allocator.py). Per explicit request: when this leaves Phase 0
  it will get a fixed starting balance to allocate across its selected
  markets, so that allocation needed to be real now, in simulation, not
  deferred. config.STARTING_CAPITAL_USD (default $1000) is split across
  whichever markets main.py is currently watching, weighted by each
  one's profit/risk ratio -- reusing incentive_screener's adjusted_score
  (reward density dampened by real crowding and recent volatility)
  rather than inventing a second metric, since that ratio already IS
  "potential profit / risk". Bounded to [MIN_ALLOCATION_FRACTION,
  MAX_ALLOCATION_FRACTION] (0.15/0.60 default) per market via a proper
  bounded-proportional ("waterfilling") split -- caught and fixed a real
  bug in an earlier single-pass clip-then-rescale draft during testing:
  rescaling to sum to 1.0 after clipping could push a value back OUTSIDE
  the bound it was just clipped to (e.g. a market capped at 60% ending
  up at 66.7% after the rescale). The waterfilling version locks
  violators one at a time and only re-splits the REMAINING budget across
  still-unlocked markets, so both bounds now actually hold.
  MarketRuntimeState gained a per-market max_inventory field (replacing
  the single flat config.MAX_SIMULATED_INVENTORY used everywhere before,
  which is now only a fallback default); all 5 call sites in main.py
  that read it now read state.max_inventory instead. A market switched
  into mid-run inherits the vacated market's allocation directly rather
  than triggering a full portfolio rebalance -- a switch is scoped to
  the one market that triggered it. 6 new tests (profit/risk weighting,
  missing-adjusted_score fallback, both bounds binding together, the
  infeasible-bounds fallback, empty input, MarketRuntimeState's default/
  override); 33/33 passing. Re-verified via grep that no order-placing
  code exists anywhere. Quote SIZE (config.DEFAULT_SIMULATED_QUOTE_SIZE)
  is still a flat constant, unaffected by this -- only how big a
  position is ALLOWED to grow before quotes pull/flatten is now
  allocation-aware, not the resting order size itself; worth revisiting
  if a market's allocation ever ends up smaller than the quote size.
- 2026-09-02 (completed): rest_client.get_trades was hitting a
  nonexistent /markets/{ticker}/trades path (404 confirmed via curl on
  every ticker checked) instead of the real /markets/trades?ticker=...
  list endpoint -- caught by the capital-allocation work above, since
  that's what first exercised it live. Pre-existing bug, predates this
  session; main.py's own trade handling uses the WebSocket channel, not
  this REST method, so nothing else was affected. Fixed.
- 2026-09-02 (completed): Replaced GDELT with Google News RSS as the
  news-awareness source. GDELT was found to be 100% unreachable from
  this network -- CONFIRMED via curl (connect timeout, read timeout, and
  SSL handshake timeout all observed across repeated attempts, while
  Kalshi's own API and other HTTPS hosts worked fine from the same
  machine, ruling out a general network issue) -- so the signal had
  never actually worked, contrary to the earlier assumption that this
  was just a sandbox-only restriction. Verified Google News RSS live,
  including through Python's `requests` (not just curl), before
  shipping. fetch_recent_articles keeps its exact same contract
  (returns [{"headline", "source_url", "theme_tags"}]) so nothing
  downstream needed to change. theme_tags is now always [] (Google News
  RSS has no GKG-style tag concept) -- a non-regression, since
  score_relevance already fell back to title-only matching in the
  common case where GDELT's theme_tags was empty too.
- 2026-09-02 (completed): Fixed a real precision bug this surfaced
  immediately once the feed actually started working: with a short
  derived-keyword list (e.g. 4 keywords for a short market title), a
  SINGLE generic-word match (e.g. "sign", "orders") already cleared
  NEWS_RELEVANCE_THRESHOLD=0.2 on completely unrelated articles
  ("Patriots Sign TE...", "Justice department orders..."), flooding
  news_events almost every poll (18 hits for one ticker in one cycle,
  12 of 29 total logged rows confirmed false positives and deleted from
  the live DB after the fix). Root cause: derive_keywords lowercased the
  title BEFORE ranking by length, so for "Will Mamdani sign above 0
  executive orders...", "executive" (9 chars, generic) outranked
  "mamdani" (7 chars, the market's actual distinctive subject) --
  length alone isn't a reliable distinctiveness signal. Fixed by ranking
  CAPITALIZED words (a crude but effective proper-noun signal) first,
  and added a new is_relevant() gate requiring the single most
  distinctive keyword specifically be present, not just the aggregate
  overlap ratio. 4 new regression tests using the real observed
  keywords/headlines; 39/39 passing overall. Re-verified via grep that
  no order-placing code exists anywhere.
- 2026-09-02 (completed): Dashboard now shows a Portfolio Balance chart
  (config.STARTING_CAPITAL_USD + cumulative simulated P&L over time) and
  a Capital Allocation panel (per-market $ and % of the current
  allocation, from capital_allocator.py). The balance series correctly
  freezes a market's contribution at its last known value if
  market_switcher.py rotates away from it, rather than dropping it --
  reuses server.py's existing latest-per-ticker accumulation pattern, no
  new "banked P&L" bookkeeping needed in main.py. Also fixed, while
  building this: the existing performance chart's history was capped at
  a flat "last 2000 rows" (server.py's old HISTORY_ROW_CAP), which
  across 3 actively-ticking markets only covered roughly the last ~11
  minutes -- not remotely enough for a 1-2 week run. Replaced with a
  proper time-bounded (config.PERFORMANCE_HISTORY_LOOKBACK_HOURS,
  default 14 days) + downsampled (config.PERFORMANCE_HISTORY_BUCKET_SECONDS,
  default 5 min -- last sample per bucket, computed via SQL window
  functions) query, so the chart stays meaningful across the whole run
  instead of only the last few minutes. New inventory_marks.max_inventory
  column (in-place migration) records each market's current
  capital_allocator allocation on the SAME continuous per-tick timeline
  already used for P&L -- no second table needed, and it self-corrects
  when a market is switched away (simply stops getting new rows, same
  as every other per-market dashboard field). Verified end-to-end
  against real live payload data via a Node harness (no browser
  available in this environment) executing the actual render functions,
  not just a syntax check. Purely additive/visualization -- no
  simulation logic touched, re-verified via grep.
- 2026-09-02 (completed): Dashboard now explains WHY a market's risk
  level is what it is, shows its real name, and lists what Kalshi's
  incentive program actually tells us about it. risk_engine.RiskEngine
  gained public volatility_level/inventory_level attributes (alongside
  the existing news_signal/event_signal) so main.py can log the full
  breakdown, not just the composite badge; server.py's new
  _risk_reason() attributes the composite to whichever factor(s) are
  independently AT that same severity (e.g. an ELEVATED inventory_level
  is correctly NOT cited as the cause of a HIGH reading driven by
  volatility alone), and never cites news/event at HIGH, since
  risk_engine.py only ever lets them nudge to ELEVATED. New
  market_metadata table (one row per ticker, upserted, not a time
  series) holds each market's real title/subtitle -- fetched once via
  rest_client.get_market when a ticker starts being watched (main.py's
  new fetch_market_metadata(), called from run() and again from
  market_switch_loop for a switched-into replacement), not on a
  recurring poll, since titles are effectively static. quote_snapshots
  gained a crowding_ratio column (our "competition level" proxy --
  Kalshi doesn't publish one directly; reuses market_switcher.
  compute_crowding_ratio, now also computed every scoring_loop tick, not
  just inside market_switch_loop's own periodic check).
  incentive_program_snapshots gained start_date/end_date columns (the
  program's real window, not derivable from days_remaining/
  total_period_days alone at a single point in time). Watched Markets
  table gained Market (title, ticker as subtext), Competition, Total
  Reward ($), and Program Window columns, plus a plain-English reason
  line under the Risk badge. Also fixed, while adding this: server.py
  was acquiring the process singleton lock at IMPORT time (inconsistent
  with main.py's own pattern of acquiring it inside run()), which meant
  server.py's pure helper functions couldn't be safely unit-tested (or
  even imported) while the real dashboard was running -- moved the lock
  acquisition into `if __name__ == "__main__":`. 2 new tests
  (RiskEngine's exposed breakdown, _risk_reason's attribution logic
  including the "don't blame the wrong factor" and "never blame news/
  event for HIGH" cases); 41/41 passing. Verified end-to-end against a
  real live payload (real market titles, real program dates, and a
  live ELEVATED reading correctly attributed to "recent news" matching
  an actual concurrent news_events hit) via a Node harness executing
  the actual render functions. Re-verified via grep that no
  order-placing code exists anywhere.

## 2026-09-02 INCIDENT (caught live from the dashboard itself, fixed same day)
User noticed the Reward Score chart's line stopped abruptly partway
across the chart, and the Capital Allocation panel showed 4 dollar
figures (562, 562, 288, 150 -- summing to $1562) for only 3 watched
markets. Root causes, both in server.py, both introduced this session:

1. **Chart stopping early**: pnl_dollars/balance_dollars logged one
   point per (ticker, bucket) row, while reward_score logged one point
   per bucket TOTAL -- with 3 tickers, pnl had ~3x as many points
   (CONFIRMED: 169 vs 363 live). renderChart() plotted both series
   against a SHARED x-axis of plain sequential point index sized to the
   longer series, so the shorter (reward_score) line ran out of data
   and visually stopped well before the chart's right edge -- even
   though its underlying data was fully current (0.66s stale, not
   actually behind at all). Fixed two ways: _performance_history now
   merges all tickers' rows within a bucket into exactly ONE combined
   point per bucket (matching reward_score's density), AND both charts
   now use a real timestamp-based linear x-axis with {x, y} data points
   instead of a shared index -- so even if point counts ever differ
   again, each series lands at its own correct time position rather
   than being silently misaligned.
2. **Duplicate/stale allocation entries**: "latest per ticker" (the
   pattern every other per-market field already uses) means "latest
   EVER", not "currently watched" -- a ticker market_switcher.py had
   switched away from just minutes earlier was still showing up with
   its old allocation (345s stale vs. ~0.2s for genuinely active
   tickers), duplicating the dollar figure of whichever ticker inherited
   or shared that allocation. New RECENCY_CUTOFF_SEC (20s) + _is_recent()
   helper, applied to _capital_allocation AND the main Watched Markets
   table's ticker list (which had the identical, just less visually
   obvious, problem -- 6 rows shown for 3 actually-active markets).
   Falls back to the static config.WATCHED_MARKET_TICKERS list only if
   NOTHING is currently active, preserving the original "helpful
   pre-data hint" behavior for a genuinely fresh start.

3 new tests (bucket-merge point count, stale-ticker exclusion, the
recency cutoff itself); 44/44 passing. Verified live: allocation now
shows exactly 3 tickers summing to exactly $1000, and reward_score/
pnl_dollars/balance_dollars all report identically 170 points with
matching end timestamps (~0.001s apart). Re-verified via grep that no
order-placing code exists anywhere.

## 2026-09-02 (completed): Proactive exit mechanics
Per explicit request: previously, once a passive fill built inventory,
the only exit mechanism was the HIGH-risk hard stop at 80% of
max_inventory -- a position could otherwise sit and drift indefinitely,
including into a market's own resolution. Three additions, all in
quoting_policy.py, wired into main.py's scoring_loop/make_trade_handler:

- **Take-profit floor**: once we hold inventory, compute_quote caps the
  EXIT side only (NO if long yes, YES if short yes) so it's never rested
  more aggressively than the price implying config.TAKE_PROFIT_MARGIN_CENTS
  (4c) of real profit vs. avg_entry_price -- still a passive limit price
  (maker), never crosses the spread on its own. This also fixes a real
  gap: the existing inventory skew had zero connection to actual entry
  price, so the unwind side could rest at a price that would be a LOSS
  if filled.
- **Time-based pre-resolution exit**: event_calendar.event_refresh_loop
  now also caches each market's real close_time on MarketRuntimeState
  (not just the boolean event_signal), so scoring_loop computes
  minutes_until_close fresh every ~1s tick with no extra API call
  (close_time is static). Starting config.EXIT_BEFORE_RESOLUTION_MINUTES
  (60) before close, the take-profit margin above is linearly relaxed
  toward 0 (quoting_policy.effective_take_profit_margin) -- we stop
  holding out for a profit margin we're unlikely to get once resolution
  is imminent. Below config.CRITICAL_EXIT_MINUTES (15),
  maybe_execute_time_based_exit force-flattens any remaining inventory
  (reusing flatten_position) REGARDLESS of risk_level, unlike the
  existing hard stop.
- **Refined hard-stop threshold**: maybe_execute_stop_loss now fires at
  max(HARD_STOP_INVENTORY_FRACTION * max_inventory,
  DEFAULT_SIMULATED_QUOTE_SIZE + config.HARD_STOP_MIN_BUFFER_CONTRACTS)
  contracts, not a pure fraction -- otherwise a market with a small
  allocation (e.g. the real $150 cap seen live this session) hits "80%
  of cap" after a single ~100-contract fill, forcing an immediate,
  unwanted stop-loss before the position ever had a chance to work.
  Large allocations are unaffected (the fraction term still dominates).

Each fill is now also classified for logging
(quoting_policy.classify_fill_exit_reason, called from
make_trade_handler with the pre-fill net/avg_entry captured before
mutation): None (entry/accumulating), "take_profit_exit" (reduced
inventory at a price meeting the full margin), or "exit" (reduced
inventory below the full margin, e.g. during the time-decay window).
New nullable simulated_fills.exit_reason column (in-place migration).
The new forced flatten reuses the existing simulated_stop_losses table
with reason="pre_resolution_flatten" -- no new table needed. Both new
active-flatten paths (hard stop, pre-resolution) now also log a
logger.warning() line, not just a DB row, matching the visibility
other significant events (market switches) already get.

6 new tests (take-profit floor actually constraining vs. not, the
margin's time-decay curve including the zero-at-critical boundary, the
forced time-based exit firing/not-firing, the refined hard-stop
threshold protecting a small allocation while leaving large ones
unaffected, and the fill classifier's take_profit_exit/exit/None
cases); 50/50 passing. Re-verified via grep that no order-placing code
exists anywhere. Restarted live and confirmed no errors, with all three
watched markets' quotes unaffected while flat (net_yes_contracts=0),
correctly exercising the "no take-profit floor on flat inventory"
no-op path in production.

## 2026-09-02 INCIDENT: main.py crashed outright on a transient network error
main.py went down (found via ./manage.sh status while investigating an
unrelated background-task notification) with an unhandled
requests.exceptions.ConnectionError ("Connection reset by peer") raised
inside market_switch_loop's find_replacement -> screen_incentive_programs
-> get_all_incentive_programs REST call. asyncio.gather cancels every
other task the moment ONE of them raises, so a single transient network
blip during a switch-search took down the entire process -- server.py
(a separate process) kept running throughout, so this wasn't caught by
casual observation of the dashboard.

Root cause: market_switch_loop's per-ticker body had NO try/except
anywhere, unlike every other loop in main.py (incentive_refresh_loop,
news_refresh_loop, event_refresh_loop), which all already guard their
bodies with try/except Exception + logger.exception(...). Auditing the
rest of main.py for the same gap found TWO more: scoring_loop (the most
critical loop -- ~1s cadence, all scoring/quoting/inventory logic) had
zero exception protection either, and make_trade_handler's on_trade
(invoked directly from ws_client's message loop) wasn't guarded, so an
error there would be caught by ws_client.run_forever's outer try/except
but force a full WebSocket reconnect + resync of every watched book
over a single bad trade message, rather than just skipping it.

Fixed all three: market_switch_loop and scoring_loop each wrap their
per-ticker body in try/except Exception, logging and continuing to the
next ticker (or next interval) instead of taking down the process;
on_trade wraps its body the same way, logging and returning instead of
forcing a reconnect. No behavior change on the success path -- purely
defensive. Restarted main.py (server.py was unaffected and stayed up
throughout); confirmed stable afterward. This is a reliability gap that
matters a lot for an unattended multi-day/multi-week run -- a single
transient REST hiccup should never be able to take the whole pipeline
down, and now it can't.

## 2026-10-01 06:33:29 UTC -- OFFICIAL START of the 1-2 week unattended run
Deployed to a DigitalOcean Droplet (<droplet-ip>, ubuntu-s-1vcpu-1gb-nyc1)
as a systemd service (kalshi-lab.service, Restart=on-failure, enabled at
boot), with a second localhost-only dashboard service
(kalshi-dashboard.service, view via SSH tunnel), a 5-minute healthcheck
cron alerting to a private ntfy.sh topic on failure, and a two-layer
daily backup (on-box via sqlite3's online .backup() API, plus an
opportunistic off-box pull to the local machine via a dedicated,
command-restricted SSH key with no shell access).

Restarted one final time at this timestamp specifically to mark a clean
start -- everything before this point on the Droplet was deployment
testing (including the news-relevance flood and the 24.5-hour
continuous-re-optimizer incident, both found and fixed the same day).
Watch list is 100% algorithm-selected (config.AUTO_SELECT_WATCHLIST=True,
no manual override) and the market switcher is armed
(MARKET_SWITCHER_ENABLED=True, using the relative-improvement fix).
Starting watch list at this restart: KXNFLLADDERREC-26OCT01PITCLE-CLEQJUDKINS10
($359), KXNFLLADDERREC-26OCT01PITCLE-CLEHFANNIN44 ($334),
KXNFLLADDERRSHYDS-26OCT01PITCLE-PITJWARREN30 ($307) -- expected to
change on its own via the switcher over the run; this is just the day-0
snapshot. Confirmed clean startup, zero errors.

When analyzing results after the run: data/paper_trading.sqlite (and
its backups) contains BOTH this official window AND all of today's
pre-start testing -- filter on ts >= 1790836411 (CONFIRMED against the
live DB, not computed by hand: the first real inventory_marks row
logged by this restart's process) to exclude the testing noise, most
visibly the ~10,778-row pre-fix news_events flood and the 50 pre-fix
market_switches from the continuous-re-optimizer incident, neither of
which reflects real running conditions going forward.

## Next steps (in order)
1. Let the live run actually accumulate data -- the pipeline now picks
   its own watch list at startup and can switch away mid-run on its own
   (risk engine, reference-price quoting, market switcher armed and
   throttle-fixed, news + event-calendar signals, autonomous selection),
   but has very little real runtime behind it yet.
2. Deploy main.py to an always-on host (VPS, systemd service) -- not yet
   started; right now data collection (and the ability to actually pick
   new markets/switch) stops whenever this laptop is off/asleep.
3. Run continuously for 1-2 weeks minimum, spanning quiet periods and at
   least one volatility event.
4. Analyze data/paper_trading.sqlite: compare avg snapshot score x period
   reward against worst-case adverse-selection swings per market.
5. Decision gate: only markets where reward plausibly exceeds worst-case
   risk proceed to Phase 1 (small live pilot, real orders, new code not
   yet built).
