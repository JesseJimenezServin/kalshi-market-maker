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
- deploy/ -- Droplet-side artifacts (off-box backup push + systemd
  units + install steps). Written 2026-10-02, NOT yet deployed.
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
- Deployed as of 2026-10-01: main.py runs on the Droplet
  (kalshi-lab.service), with a localhost-only kalshi-dashboard.service
  beside it. A laptop dashboard and a laptop-side backup pull also still
  exist; both are being retired -- see "Target architecture: nothing runs
  on the laptop" below.
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

## 2026-10-02 INCIDENT: off-box backup silently dead, dashboard silently stale

Two independent silent failures, both found while asking why the
dashboard's PORTFOLIO BALANCE did not match the real account. Neither
had surfaced an alert anywhere, and both were invisible from the
dashboard itself -- it kept rendering a plausible-looking balance the
whole time.

(0) Pre-note on the question that started it: PORTFOLIO BALANCE is not,
and never was, a Kalshi account balance. server.py computes it as
config.STARTING_CAPITAL_USD + simulated pnl_dollars -- a hardcoded
$1000.00 base plus paper P&L (the docstring calls it "the pretend
account balance"). rest_client.get_balance() does hit the real
/portfolio/balance endpoint, but no dashboard code path calls it. This
is working as designed; it is only confusing because the tile is
labelled in dollars with no "simulated" marker.

(1) BACKUP DEAD SINCE 09-30 19:40. The repo lived in ~/Downloads, which
macOS protects under TCC. The launchd agent
(com.kalshi.droplet-backup-pull) therefore could not execute
pull_droplet_backup.sh at all: every firing failed instantly with
"/bin/bash: ...: Operation not permitted" and exit code 126, appending
to droplet_backups/launchd.log while pull.log stayed frozen at its last
success. So the off-box copy stopped the same evening the official
unattended run was marked as started, and the 09-30 -> 10-02 window had
no second copy anywhere.
FIX: moved the repo to ~/projects/kalshi_liquidity_lab (out of the
TCC-protected folder) and updated the only two places that hardcoded the
old absolute path -- DEST_DIR here and the three path strings in
~/Library/LaunchAgents/com.kalshi.droplet-backup-pull.plist. config.py
derives everything from BASE_DIR = Path(__file__).parent, so nothing
else needed touching, and the venv survived the move unchanged.
CONFIRMED: launchctl kickstart now exits 0 with an empty launchd.log,
and pulled a 661721088-byte snapshot (integrity_check: ok, data through
10-02 02:00) at the new path.

(2) DASHBOARD SERVING A RUN THAT DIED ON 09-05. main.py runs on the
Droplet; server.py runs on the laptop. But server.py read
config.DB_PATH, which was unconditionally the LOCAL
data/paper_trading.sqlite -- last written 09-05 14:42, when this laptop
was still the host. The laptop dashboard had been showing a ~4-week-old
dead run while the Droplet accumulated the real one, and nothing
indicated the staleness:
    LOCAL   (what the dashboard showed)  snapshot_scores 153280, simulated_fills     3, latest 2026-09-05 14:40
    DROPLET (the actual run)             snapshot_scores 256975, simulated_fills  1237, latest 2026-09-30 19:24
FIX: config.DB_PATH now honours an optional KALSHI_DB_PATH override,
defaulting to the old DATA_DIR path when unset -- so main.py on the
Droplet is unaffected (CONFIRMED by importing config.py with a
Droplet-style .env that omits the var: DB_PATH falls back to
<repo>/data/paper_trading.sqlite). The override is set only in this
laptop's gitignored .env, pointing at
droplet_backups/paper_trading_latest.sqlite, which
pull_droplet_backup.sh now repoints (symlink, not a copy) after every
successful pull, so the dashboard follows new snapshots with no further
edits. server.py also opens the DB mode=ro now: it only ever reads, and
must not drop a -wal/-journal next to a backup snapshot or quietly
create an empty DB if the path is ever wrong.
CONFIRMED over the live WebSocket after the change: 685 balance points
spanning 09-29 17:04 -> 10-02 02:00, $1000.00 -> $1106.87, 10 recent
fills, 3 markets. test_logic.py: ALL TESTS PASSED.

Residual limitation, by design: the laptop dashboard now shows the
Droplet's run as of the last pull, not live -- the launchd agent fires
daily at 10:00, so the view can lag up to ~24h. A live view would mean
running server.py on the Droplet next to main.py; not done, and the
backup SSH key on this laptop deliberately cannot do it (it is
restricted to the Droplet's forced push_latest_backup.sh command, so no
general-access credential is stored here).

## 2026-10-03 INCIDENT (caught before it hit): Droplet disk would have filled mid-run

Found by checking capacity after gaining admin access to the Droplet.
Nothing had failed yet; this was ~9 days from failing.

backup_db.py kept KEEP_LAST = 5 UNCOMPRESSED full snapshots of a
database growing ~390MB/day. Measured from three consecutive on-box
backups plus the live file: 269MB (10-01 09:00) -> 662MB (10-02 09:00)
-> 939MB live (10-03 02:19), i.e. 393MB/day then 385MB/day, so flat
rather than tapering. Five full copies of that grow faster than the
database itself: projected 21.6GB of DB artifacts against ~20GB of
usable disk (24GB total less ~2.8GB OS/venv and the ext4 reserve) at
about day 10 -- roughly 10-12/10-13, i.e. BEFORE the 14-day run that
started 10-01 would have finished.

Consequence had it been missed: the disk fills, main.py's SQLite writes
start failing mid-run, and the run ends ~4 days short with its final
days of data lost. The 5-minute healthcheck would have caught the
resulting stall, but only after the damage.

FIX (applied 10-03, verified on the box):
- backup_db.py now gzips each snapshot, and its retention sweep also
  picks up legacy uncompressed files so the old ones rotate out
  normally. Measured on the real 940MB database: 940032000 -> 71432530
  bytes, a 92.4% reduction, in 16 seconds wall clock (nice -n 10, since
  this shares one vCPU with main.py).
- It also refuses to start unless ~1.3x the DB size is free, so the
  backup can never itself be the process that fills the disk and takes
  main.py's writes down.
- The three existing uncompressed backups were compressed in place:
  1.1GB -> 102MB, and disk use went 4.7GB -> 3.7GB.
- push_latest_backup.sh (the restricted key's forced command) now zcats
  .gz backups, so it still streams RAW sqlite bytes and any existing
  puller keeps working unchanged. CONFIRMED: streamed 940032000 bytes
  with a valid "SQLite format 3" header, and the forced-command binding
  in authorized_keys is intact.
- CONFIRMED end to end: the new gzipped backup decompresses to
  940032000 bytes, PRAGMA integrity_check returns ok, snapshot_scores
  has 716375 rows with max(ts) at the backup instant, simulated_fills
  3205.
Reprojected with compression: ~7.8GB total at day 14, leaving ~12.2GB
headroom. The disk is no longer the binding constraint.

Worth a separate decision later, not fixed here: ~390MB/day of SQLite
for three watched markets is a lot, and it is dominated by
orderbook_snapshots. Reducing that sampling rate would shrink
everything downstream, but changing logging volume mid-run would make
the run's data non-uniform, so it was deliberately left alone.

## 2026-10-03 SECURITY: alert topic was about to be published

While copying the Droplet's untracked infrastructure into deploy/ for
the first time, healthcheck.sh was found to hardcode the private ntfy
topic. Committing it as-is would have published the topic to GitHub,
letting anyone read these alerts and post convincing forged ones --
including a forged "all clear".

FIX: the topic now lives in /etc/kalshi-alerts.env (chmod 600, not in
git), sourced by both healthcheck.sh and the new
deploy/push_offbox_backup.sh so there is one secret to rotate and one
place to watch. The file was written on the Droplet from the value
already there, so the secret was never retyped into anything tracked.
healthcheck.sh now exits 2 with a loud FATAL if the topic is missing
rather than POSTing to an empty URL -- a healthcheck that cannot alert
but looks healthy is the exact 10-02 failure shape.
CONFIRMED all three paths: normal run exits 0 silently; with the
secrets file absent it exits 2 and logs FATAL; and a labelled test POST
to the real topic returned HTTP 200.

Consider rotating the topic anyway: it sat in a file on two machines
and in this session's logs. Changing it is a one-line edit to
/etc/kalshi-alerts.env plus re-subscribing in the ntfy app.

## 2026-10-03: laptop removed from the system; off-box backup live

GOAL MET. Nothing Kalshi-related runs on the laptop any more: no launchd
job, no process, no cron, port 8000 free. The Droplet is the whole
system and the laptop is a viewing terminal reached by SSH tunnel.

Off-box backup is now a Droplet-side PUSH to Backblaze B2
(kalshi-db-backups-prod), on kalshi-offbox-backup.timer daily at
03:30 UTC with up to 600s jitter and Persistent=true so a missed run
fires on next boot. Verified end to end, including via `systemctl start`
rather than only from a shell: 975MB -> 72.7MB in ~1m40s, read-back size
check, staging cleaned, and both forced-failure paths exiting 1 with an
ntfy alert.

MOST IMPORTANT VERIFICATION: the backup was actually RESTORED off-box --
downloaded from B2 to the laptop with the restricted key, gunzipped to
956022784 bytes, PRAGMA integrity_check ok, 726967 snapshot_scores rows.
An unrestored backup is a hypothesis, not a backup.

The laptop-era run (09-01..09-05, 153280 rows) existed nowhere else, so
it was uploaded to B2 as
laptop-era/paper_trading_laptop_20260901-20260905.sqlite.gz, SHA1
confirmed matching. Local data/ and droplet_backups/ are now safe to
delete; nothing unique remains on this machine.

CREDENTIALS -- what was changed and why. The key supplied was a MASTER
application key: scoped to all buckets, with deleteFiles, deleteBuckets,
deleteKeys, writeKeys and bypassGovernance. Putting that on the Droplet
would have defeated the entire point of the off-box copy, because
anything that compromised or malfunctioned on the Droplet could then
erase every backup in the account and bypass object-lock retention. A
restricted key "kalshi-droplet-offbox-push" was minted instead --
writeFiles, listFiles, readFiles, scoped to the single bucket, no delete
-- and only that key is on the box. readFiles is included deliberately
so a restore works with the same key. rclone confirms the scoping
independently: pointing it at another bucket fails with "you must use
bucket kalshi-db-backups-prod with this application key".
The master key should be rotated: it was pasted into a chat transcript.
Retention is NOT yet configured -- it belongs on the bucket as a
lifecycle rule (provider-enforced, so it still works when this host is
gone) and was left for an explicit decision about the window rather
than set unilaterally, since it deletes data on a schedule. Bucket
currently holds 4 test-run backups (~290MB) plus the laptop-era archive.

B2 GOTCHA worth remembering: B2's S3-compatible endpoint returns 403
Forbidden on PUT for a bucket-restricted application key. The same key
authorizes fine via b2_authorize_account with writeFiles, lists fine,
and uploads fine through rclone's NATIVE b2 backend; removing the ACL
header made no difference. Since bucket-scoping is the security property
worth keeping, the native backend is the right side of that trade.

BUG IN MY OWN SCRIPT, found by the forced-failure test not failing:
push_offbox_backup.sh sourced /etc/kalshi-offbox.env with a plain `.`,
which assigns unconditionally and so silently overrode any environment
variable the caller passed. The documented verification step
(`DB_PATH=/nonexistent ./push_offbox_backup.sh`) therefore ran a NORMAL
backup and exited 0 while appearing to prove the alert path -- it did
this twice before being noticed. The config reader now uses `${k:=v}` so
the environment wins, verified both ways. A verification step that
cannot fail is worse than no verification step, which is the same lesson
as the 10-02 silent backup failure in a new costume.

## 2026-10-03: _latest_per_ticker rewritten -- helps, does not fix

Applied the max(ts)/GROUP BY rewrite to server.py's _latest_per_ticker.
Correct and verified, but it did NOT fix the dashboard CPU load, and the
gap between the isolated benchmark and the live result is the useful
part of this entry.

EQUIVALENCE, checked properly: compared old vs new for all five real call
sites with their actual column lists, against a STATIC copy (a
decompressed on-box backup, not the live DB -- an earlier comparison
against the live file produced a phantom mismatch that was just main.py
writing between the two queries). Results byte-identical for every call
site, 28-29 tickers each.

Benchmarked on that static copy, the rewrite looked like a 10x win:
    snapshot_scores              7.84s -> 0.72s
    inventory_marks              6.97s -> 0.69s
    incentive_program_snapshots  0.03s -> 0.00s
    quote_snapshots              6.65s -> 1.00s
    orderbook_snapshots         11.32s -> 8.50s

On the LIVE box it is far less, because the static benchmark had no
competing writer and a warm cache to itself:
    _performance_history                     7.00s -> 6.93s  (untouched)
    _qualifying_counts                       2.67s -> 2.37s  (untouched)
    _latest_per_ticker(snapshot_scores)      2.60s -> 0.70s
    _latest_per_ticker(inventory_marks)      2.52s -> 2.84s  (noise/contention)
    _latest_per_ticker(quote_snapshots)      5.34s -> 4.19s
    _latest_per_ticker(orderbook_snapshots) 12.84s -> 8.59s
End to end through the real WebSocket path: 41.2s -> 25.5s per tick.
Better, and strictly less work, but not a fix. Kept.

WHY IT IS NOT A FIX, and what would be. ws_endpoint does
`build_dashboard_payload()` then `await asyncio.sleep(1.0)`, i.e. it is
designed to tick once a second. A tick costs ~25s. So while any browser
is attached the loop is ~96% busy and the 1s sleep is irrelevant -- the
box is saturated by the build itself, and no plausible per-query speedup
closes a 25x gap. In rough order of leverage:
  1. Raise the tick interval (one line). At 25s/tick the dashboard is
     already effectively refreshing every ~26s; sleeping 15-30s between
     builds would make that explicit and hand the vCPU back to main.py
     in between. Highest leverage, lowest risk, no query changes.
  2. _performance_history is now the single largest component at 6.9s and
     the rewrite does not touch it. PERFORMANCE_HISTORY_LOOKBACK_HOURS is
     336h against ~75h of data, so it buckets a window 4.5x larger than
     the data; matching the lookback to the run, or widening
     PERFORMANCE_HISTORY_BUCKET_SECONDS, cuts it directly.
  3. orderbook_snapshots is 8.6s because of its yes_levels/no_levels
     blobs, not the row scan -- GROUP BY still walks the whole index.
     Fetching only the 3 CURRENTLY-WATCHED tickers with
     `ORDER BY ts DESC LIMIT 1` per ticker would be ~3 index seeks
     instead of a 780k-entry walk. The dashboard renders 3 markets but
     this query returns 29.
  4. Cache the payload server-side and let all clients share one build.
None of these were done: the ask was specifically the GROUP BY rewrite,
and 1-3 each change behaviour (refresh rate, chart window, which tickers
appear) rather than being pure optimisations.

Note on expectations: the 5-7x figures I reported before applying this
came from the static benchmark. I should have said they were measured
without a concurrent writer, because that turned out to be most of the
difference.

## 2026-10-03: dashboard CPU load fixed (scoped orderbook query + 15s tick)

Applied the two remaining options. Together with the earlier
max(ts)/GROUP BY rewrite this takes the tick from 41.2s to ~9.3s and the
duty cycle from ~96% to ~38%, so the Droplet's single vCPU is no longer
saturated whenever a browser is open.

OPTION 3 -- scope the orderbook query. _latest_per_ticker now takes an
optional ticker list, and the orderbook_snapshots call is MOVED to after
`tickers` is computed so it can be restricted to exactly the markets
being rendered. That table carries the yes_levels/no_levels blobs and
was the single most expensive read in the tick while returning 29
tickers for a table that renders 3. `book` is only ever read as
books.get(ticker) inside the render loop, so narrowing it cannot change
the payload.
Measured on a static copy: 9.67s unscoped (29 tickers) -> 0.287s scoped
(3 tickers), a 34x improvement, and EXPLAIN QUERY PLAN changes from a
full index walk to `SEARCH orderbook_snapshots USING INDEX
idx_orderbook_ticker_ts (ticker=?)`. Results verified identical for the
scoped tickers, and the empty-list case returns {} rather than emitting
invalid `IN ()` SQL.

OPTION 1 -- WS_TICK_SECONDS = 15.0, replacing a hardcoded
asyncio.sleep(1.0). The 1s target was aspirational rather than real: a
build cost ~25s, so nobody ever saw a 1s refresh, and the loop simply
ran flat out. Making the interval explicit hands the core back between
builds.

RESULT on the live box:
    payload build, cold (just after restart)   24.73s
    payload build, warm                         9.86s / 9.28s
    end to end via the real WebSocket           9.8s to first payload
    observed cadence between payloads           24.0s / 24.1s
                                                (9.3s build + 15s sleep)
    duty cycle                                  ~38%  (was ~96%)
With a client attached: dashboard process ~27% CPU, main.py 1.9%, load
average 0.62-0.79 -- indistinguishable from the no-client baseline of
0.74, where before the fix an attached client pushed it to 1.03. Load
average is noisy at this scale, so the duty-cycle figure is the firmer
number of the two.

Full history of this tick, for reference:
    41.2s  original
    25.5s  after max(ts)/GROUP BY rewrite
     9.3s  after scoping the orderbook query
and the ts-leading indexes from earlier contributed nothing measurable,
for the reasons in the entry above.

NOW THE LARGEST REMAINING COST: _performance_history at 6.95s, roughly
70% of the 9.3s. Untouched by any of this. It buckets a 336h window
(PERFORMANCE_HISTORY_LOOKBACK_HOURS) over ~75h of data, so it processes a
window 4.5x larger than the data that exists. Matching the lookback to
the run, or widening PERFORMANCE_HISTORY_BUCKET_SECONDS from 300s, would
cut it directly -- but both change what the chart shows, so neither was
done. If the build ever drops well under 15s, lower WS_TICK_SECONDS to
match rather than leaving the dashboard needlessly stale.

Rollback copies on the Droplet: /root/server.py.orig-20261003 (before
any of today's query work) and /root/server.py.prev-groupby (after the
GROUP BY rewrite, before the orderbook scoping).

## 2026-10-03: monitoring gaps closed before the unattended run

The run was otherwise ready; what was missing was visibility into the
things that protect the run. Closed two of three gaps here; the third was
handled outside the repo.

GAP 1 -- nothing reported the off-box backup silently ceasing. The push
script alerts when it FAILS, but a dead timer, a masked unit or a
removed cron produces no failure to alert on, so silence was being read
as success. That is exactly the 10-02 incident one level up.
FIX, two parts:
  - push_offbox_backup.sh now writes /var/lib/kalshi-offbox/last-success
    ONLY after the upload and read-back size check both pass, so the
    file's mtime answers "when did a verified off-box copy last exist".
  - healthcheck.sh alerts if that marker is missing, or older than 36h
    (OFFBOX_STALE_SECONDS: a daily timer, so this tolerates one missed
    run plus jitter). It also checks the timer unit is still armed, which
    catches the failure earlier than the marker would.

GAP 3 -- the healthcheck only watched kalshi-lab. It now also checks
kalshi-dashboard (flagged non-critical in the alert text itself, since
losing the dashboard costs no data) and kalshi-offbox-backup.timer.
The script no longer exits at the first problem: it checks everything and
reports each independently, so a down dashboard cannot mask a dead
backup timer.

GAP 2 -- "the whole Droplet is gone" cannot be detected by anything
running on the Droplet. DigitalOcean CPU and memory alert policies were
saved 2026-10-03, which covers it from outside.

ALERT THROTTLING, added unprompted because it would otherwise have made
the above counterproductive: a 5-minute cron turns one real problem into
288 notifications a day for as long as it lasts, which trains you to
ignore them. notify() is now keyed per issue and re-alerts at most
hourly (NOTIFY_THROTTLE_SECONDS), with the FIRST alert still immediate,
and state kept in /var/lib/kalshi-healthcheck. A key's state is cleared
when its condition recovers, so a recurrence alerts immediately instead
of being suppressed by a stale timestamp. The throttle stamp is written
only when curl actually succeeds -- a failed POST must not count as
"already alerted".

Also fixed, same class of bug as the push script's: healthcheck.sh
sourced /etc/kalshi-alerts.env with a plain `.`, which assigns
unconditionally and so silently overrode any caller-supplied NTFY_TOPIC.
Both scripts now let the environment win, and both are therefore
testable without editing /etc.

VERIFIED, nine cases, using a throwaway ntfy topic so the real one was
not spammed:
    1. all healthy                                exit 0, silent
    2. marker missing                             exit 1, alert
    3. marker 40h old (limit 36h)                 exit 1, alert
    4. marker 30h old (inside window)             exit 0, silent
    5. same issue twice                           first alerts, second suppressed
    6. no NTFY_TOPIC anywhere                     exit 2, loud FATAL
    7. kalshi-dashboard stopped                   exit 1, alert
    8. kalshi-offbox-backup.timer stopped         exit 1, alert
    9. healthy again after restoring both         exit 0, silent
Plus one real alert delivered to the live topic through the script's own
notify(), confirmed by the throttle stamp being written (it is only
written when curl succeeds). Both services were restored and the timer
re-armed; verified active afterwards.

READINESS at the end of this: disk projected to peak at 12.65GB of ~20GB
usable on the last day of the run, growth 394MB/day recent, both backup
layers verified including an actual off-box restore, all three units
`enabled` so they survive reboot, collector and backup failures now
self-reporting, and box-level failure covered by DigitalOcean. Remaining
non-blockers: the B2 lifecycle rule is still unset (bucket grows ~3.5GB
over the run, pennies) and the B2 master key still wants rotating since
it was pasted into a chat transcript.

## 2026-10-03: B2 retention set; local copies deleted after verification

RETENTION is now a bucket lifecycle rule on kalshi-db-backups-prod,
enforced by Backblaze rather than by a script on the host it protects:
    prefix "paper_trading_"  -> hide at 30 days, delete 1 day later
Scoped to that prefix ON PURPOSE. A blanket rule (prefix "") was the
obvious implementation and would have been a trap: it would also have
expired laptop-era/, which by then was the ONLY copy of the 09-01..09-05
run, 31 days after upload and with no warning. Anything under
laptop-era/ is covered by no rule and is retained indefinitely.

LOCAL DELETIONS, 1.1GB -> 56MB. Each was verified redundant first rather
than assumed:
  - data/paper_trading.sqlite -- the laptop-era run. The local file had
    grown 121368576 -> 126562304 bytes since being archived (a read-write
    open rolled back the stale -journal that was sitting next to it), so
    a size or hash comparison would have been misleading. Compared row
    counts and max(ts) across all 11 tables instead: identical in every
    one. Deleted.
  - data/paper_trading_backup_20260901_234239.sqlite -- 24246 rows
    spanning 09-01 20:11:04..23:42:38, entirely inside the archive's
    range with fewer rows. Redundant. Deleted.
  - droplet_backups/ (864MB) -- the 09-30 and 10-02 pulls. Both are
    strict subsets of the 10-03 snapshots already in B2; the live DB is
    append-only so later snapshots contain them. Deleted.
    (Their min(ts) appeared 7h earlier than the live DB's at first
    glance; that was PDT-vs-UTC display in two different scripts, not
    missing data. Same epoch value.)

AND ONE THAT WAS NOT REDUNDANT. data/paper_trading_contaminated_backup_
20260901_201057.sqlite looked like obvious junk -- it is literally named
"contaminated", it is the 09-01 multi-process incident, and the request
was to delete local laptop-era data. But its 91825 rows span
09-01 17:21:19..20:10:56, which ends 8 SECONDS BEFORE the archive's
range begins at 20:11:04. It was the only copy of that window anywhere,
including the only surviving record of the 9-duplicate-process incident
itself. Uploaded to laptop-era/ (80.7MB -> 8.0MB, SHA1 confirmed) before
deleting it locally.
The general lesson, twice in one task: "contaminated" and "backup" in a
filename describe someone's intent at the time, not whether the bytes
are reproducible from somewhere else. Check the time ranges.

Bucket now holds 386MB: 5 run snapshots under the 30-day rule, plus
22MB of laptop-era archives kept indefinitely. Nothing unique remains on
the laptop.

## 2026-10-03 INCIDENT: phantom orderbook levels manufactured the entire reported profit

The dashboard was showing $1162.05 against $1000 starting capital -- +16%
in three days of passive market making, which is not a plausible return
and was not a real one. 99.6% of it was an artefact of a float bug.

CAUSE. orderbook.py's apply_delta removed a price level only on
`new_size <= 0`. Sizes are floats because Kalshi's "_fp" fields are
genuinely fractional, so a level cancelled down to nothing lands on
binary residue -- 0.1+0.1+0.1-0.3 is 5.55e-17, not 0.0 -- which is > 0,
so the level was never popped. It stayed in the book forever at an
effectively-zero size.

WHY THAT WAS EXPENSIVE. best_yes_bid()/best_no_bid() return max(price)
over whatever keys exist, with no size test. A phantom at a better price
therefore became the best bid, and from there it fed everything at once:
the mid price, the quote placement, the fill test in
adverse_selection_sim.process_trade_for_inventory
(`trade_yes_price <= quote.yes_price`), and the mark the P&L was measured
against. Measured on the live DB:
    31.6%   of ALL logged price levels were phantoms
    51208   sampled snapshots where the BEST level was a phantom
and one concrete snapshot of the biggest winner:
    best YES bid  seen 12c @ size 2.84e-14   real 9c @ size 634
    implied mid   seen 11.0c                 real 9.5c   -> 1.5c off
A 1.5c error on a 10c market is 15%. The reported edge was 0.52c per
contract ($132.32 over 25459 contracts), so a persistent 1.5c mid
distortion is more than enough to manufacture all of it.

THE EVIDENCE IS UNAMBIGUOUS. Per-ticker, P&L tracks book corruption and
nothing else:
    KXTRUMPAPPROVE-26OCT02-E38.5   46.9% phantom   $132.32
    KXTRUMPAPPROVE-26OCT02-E38.6   39.2% phantom   $ 18.90
    KXTRUMPAPPROVE-26OCT02-E38.8   60.3% phantom   $  9.60
    KXTRUMPAPPROVE-26OCT01-E38.5    0.3% phantom   $  0.63
    every NFL / MLB market          0.0% phantom   $  0.00
$161.06 of the $162.04 came from the six markets with >10% phantom
levels. Every clean book made approximately nothing. It also contaminated
the REWARD scores, which are the actual point of the project:
reward_simulator derives our share as our size / level size, and a
near-zero level inflates that toward 1.0.

FIX.
- SIZE_EPSILON = 1e-6 in orderbook.py; apply_delta pops on
  `<= SIZE_EPSILON`. Deliberately a tolerance and NOT integer rounding:
  11% of real sizes are legitimately fractional (e.g. 5787.01), so
  coercing to int would corrupt live data to fix a float bug.
- apply_snapshot filters incoming levels too, so the invariant holds from
  the first message rather than only after the first delta.
- All four read paths (best_yes_bid, best_no_bid, yes_side_sorted_desc,
  no_side_sorted_desc) filter through _live_levels() as well. Redundant
  given the write sites, but this bug was invisible for days and cost the
  whole run's headline number; no future write path should be able to
  reintroduce a phantom best bid silently.
- Size type annotations corrected from int to float. The wrong annotation
  is part of why `<= 0` looked safe.
- FOUR REGRESSION TESTS in test_logic.py, which had none covering level
  removal: delta-to-exactly-zero, delta-to-float-residue (the real bug,
  and it FAILS on the old code with "phantom level survived at size
  5.551115123125783e-17"), snapshot-carrying-a-zero-level, and
  legitimately-fractional-sizes-preserved. The last one exists
  specifically to stop someone "simplifying" the fix into rounding.
CONFIRMED after restarting kalshi-lab so books rebuilt from fresh
snapshots: 0 phantom levels and 0 phantom best-levels in 3452 levels
across 518 new snapshots, against 31.6% before.

SECOND, SEPARATE PROBLEM: fixing the bug did NOT clear the fake profit.
_performance_history freezes a market's last mark and keeps counting it
forever once it stops logging (deliberate, so a market switch cannot
cause a phantom balance jump). The six contaminated markets had already
stopped logging, so their $161 would have sat in the balance chart as a
floor for the remaining 12 days of a now-correct run.
Handled with config.CONTAMINATED_TICKERS, excluded at report time in
_performance_history (both series), the rendered markets table, and
recent_fills. The rows are NOT deleted -- they are the evidence for this
entry, and this codebase already prefers documented filters over
destructive cleanup. Threshold >10% phantom separated cleanly: the six
excluded are 13.4-60.3%, the next worst is 5.3%.
Dashboard now reports $1000.99, down from $1162.05.

RESIDUAL, stated rather than hidden: the ~$0.99 that remains was measured
against books with 0.1-5.3% phantom levels, so it is not clean either --
treat any small pre-10-03 P&L as noise, not signal. The honest
measurement window for P&L and reward capture starts 2026-10-03.
Separately, turnover on the biggest contaminated market was 71x the
position cap (25459 contracts through a 359-contract limit in ~22h);
that was never validated against real market volume and still should be,
independently of this bug.

## 2026-10-04 INCIDENT: bot sat on three SETTLED markets for 4.4 hours

Found during a status check. kalshi-lab was "active", logging news and
refreshing the incentive screen, with the Kalshi WebSocket connected --
and producing no usable data at all since 16:32:04 UTC.

The three watched markets were KXTRUMPAPPROVE-26OCT04-{E38.0, E38.1,
U37.9}, all `status=finalized`, closed 2026-10-04T16:59:00Z (CONFIRMED
against the API, not inferred). Their books were empty on both sides.

WHY NOTHING CAUGHT IT. scoring_loop logged the orderbook snapshot BEFORE
testing for a usable mid, then hit `if best_yes is None or best_no is
None: continue` -- so it wrote an empty row every tick and skipped
snapshot_scores, inventory_marks and quote_snapshots entirely. 41856 of
46649 orderbook rows written in those 4.4h were empty (90%, ~229k/day of
pure bloat).

And the switcher would not leave, for a reason worth remembering:
EVERY QUALITY SIGNAL IMPROVES AS A MARKET DIES. compute_crowding_ratio is
competing-size-based, so an empty book reports crowding 0.0 against 50.0
for a busy one (measured). Live, the settled incumbent scored 14.79
against the best replacement's 120.63, so is_worthwhile_crowding_switch
correctly refused -- nothing looks less crowded than a dead market. Risk
also falls (no price movement) and reward density rises. check_trigger
therefore never fires, and when something else does fire, the crowding
gate blocks the switch. Liveness cannot be derived from any of these; it
has to be its own check.

FIX.
- config.DEAD_MARKET_EMPTY_BOOK_SECONDS = 120.0. Long enough that a
  transient empty book during real trading (refills in seconds) cannot
  evict a healthy market, short enough to lose minutes not hours.
- MarketRuntimeState.empty_book_since + is_dead(now). scoring_loop sets
  it when BOTH sides are empty, clears it the moment any level returns.
- scoring_loop no longer logs a snapshot when both sides are empty, which
  removes the ~229k/day of empty rows.
- market_switch_loop checks is_dead() BEFORE check_trigger, raises a
  "dead" trigger, and "dead" skips the crowding gate alongside "volatile"
  -- there is no point requiring a replacement be less crowded than a
  market we must leave regardless.
- Two tests. One pins the duration requirement (a single empty tick must
  not evict). The other pins the trap itself: it asserts a dead book
  reads as LESS crowded than a busy one, so if someone later tries to
  infer liveness from crowding, that test documents why it cannot work.
CONFIRMED: restarted, all four tables writing within 1s, watch list
re-selected to live OCT05 markets.

OPERATIONAL NOTE: the restart was done twice -- once immediately to stop
losing data, once after deploying the fix. Collection resumed instantly
both times.

MONITORING WORKED. The healthcheck added on 10-03 caught this on its own:
exit=1 with "[collector-stuck] no new DB row in 15546s", throttled to
hourly re-alerts. This is the first incident in this project that was
detected rather than stumbled upon.

MEASUREMENT CORRECTIONS from the same session, both mine:
- I reported 84109 "phantom" orderbook levels as a regression of the
  10-03 fix. That was a measurement error: my detector counted
  `0 < size < 0.5`, which flags LEGITIMATE small fractional sizes. Against
  the actual threshold, true residue (0 < size <= 1e-6) is 0 out of
  1611710 levels since the fix. The fix is holding exactly as intended.
- I reported "database is locked" errors as an ongoing fault. There were
  3 in 12 hours: one at 09:00:06 from backup_db.py, and TWO THAT I CAUSED
  with my own diagnostic queries. In journal_mode=delete a long reader
  blocks the writer, and logger.py connects with Python's default 5s
  busy_timeout, so a heavy analytical scan of this 1.3GB DB can stall the
  collector. Worth knowing before running big queries against it live.

STILL OPEN, flagged not fixed: there is NO SETTLEMENT ACCOUNTING. When we
switch out of a settled market, flatten_position returns None (the book
has no side to cross), so the position's P&L stays frozen at its last
mark. A settled binary market actually resolves to 0 or 100, so the true
realised P&L is knowable and is not what gets recorded. Today that left
145 contracts of KXTRUMPAPPROVE-26OCT04-E38.1 marked at -$11.88 unrealised
rather than resolved. This biases P&L by an unknown amount every time a
market settles under us, which is daily for these contracts.

CLEAN-RUN P&L as of this entry: balance $989.53, i.e. -$10.47 -- realised
$0.19, unrealised -$10.67, driven by a 145-contract long that moved
against us. A small loss from adverse selection is a far more credible
result than the +16% the phantom levels were manufacturing, and it is the
first genuine signal this project has produced.

## 2026-10-04 (later): settlement accounting, WAL, turnover validated

Closing out the three items left open by the dead-market fix.

### 1. SETTLEMENT ACCOUNTING -- was biasing P&L every single day
A binary market does not expire at its last mid, it resolves to 100 or 0.
There was no settlement path at all, so leaving a settled market froze
the position at whatever mid was last observed: on 10-04 that recorded
145 contracts of KXTRUMPAPPROVE-26OCT04-E38.1 as -$11.88 UNREALIZED when
the true outcome was already determined. These contracts settle daily, so
the bias applied daily, always leaving P&L as an estimate where a fact
was available.

InventoryState.settle(result_yes) books the position at 100/0 as REALIZED,
flattens it, and is idempotent. It uses the existing sign convention:
closing a long yes books net * (P - entry), a short yes (-net) *
(entry - P); settlement is that with P pinned to 100 or 0.
main.py calls _settle_if_resolved() on the "dead" trigger BEFORE
flatten_position -- flatten needs a side of the book to cross and a dead
market has none, so it would return None and silently leave the position
frozen, which is the exact bias being fixed. It also writes a final
inventory_marks row at the resolved price, so the dashboard's
freeze-last-value behaviour freezes the TRUE number.
It deliberately does NOT guess: if the market is closed but `result` is
not yet "yes"/"no", it logs and leaves the position alone. A wrong
settlement is worse than a late one.
Three tests: long-yes resolving both ways, short-yes plus idempotency,
and one reproducing the 10-04 position to assert nothing is left
unrealized in either direction.

### 2. DB LOCK CONTENTION -- journal_mode is now WAL
The 09:00 backup produced a real "database is locked" that lost a scoring
tick, and an ad-hoc analytical query stalled the collector the same way.
Cause was structural, not bad luck: in rollback-journal mode a READER
blocks the writer, and every reader here is slow -- backup_db.py's online
.backup(), the off-box VACUUM INTO, and the dashboard tick all scan a
>1.3GB file, while logger.py connected with Python's default 5s timeout.
logger.py now sets journal_mode=WAL (readers and the writer no longer
exclude each other) with synchronous=NORMAL, and a 30s busy_timeout as a
backstop. WAL removes the conflict class rather than waiting longer for
it.
VERIFIED BEFORE DEPLOYING, because mode=ro readers under WAL can fail if
they cannot create the shared-memory file: a mode=ro read works with the
writer connected, a held read transaction does NOT block the writer, and
a mode=ro read still works after a clean close with -wal/-shm absent.
Then verified all four real consumers on the box: dashboard ($989.52,
3 markets, 1090 points), backup_db.py (1.3GB -> 125MB, rotation working),
the off-box push (1.3GB -> 108MB uploaded, exit 0, marker written), and
healthcheck.sh (exit 0).

### 3. TURNOVER -- validated, no problem found
The open question was whether the fill model is too generous, since the
biggest contaminated market turned over 71x its position cap. Checked
against real traded volume using volume_fp:
    KXTRUMPAPPROVE-26OCT05-E38.2   real volume 3838   our fills 104 = 2.7%
    (my_share_yes on that market averages 0.022, i.e. 2.2%)
Our simulated fills land at approximately our share of the resting size,
which is what the model intends. High turnover was never the problem --
it is a small position round-tripping many times while taking only ~3% of
the flow. The 71x figure was misleading on its own.

### Correction to my own claim in this session
I reported a "fourth bug" -- that the code reads a non-existent `volume`
field. That was wrong, and it was MY diagnostic script using
m.get("volume"). The production code is correct: the Kalshi API exposes
volume_fp / volume_24h_fp / open_interest_fp, and incentive_screener.py
and market_switcher.py both go through fp_to_size()/dollars_to_cents()
on the right names. A grep for the wrong names across the codebase
returns nothing.

## 2026-10-05: settlement backfill applied; WAL and empty-row fixes confirmed

### Backfill
The settlement path added on 10-04 only covers markets that go dead while
being watched. Six positions had been abandoned BEFORE it existed and were
still frozen at whatever mid was last observed, all six since finalized
with known results:
    KXTRUMPAPPROVE-26OCT01-E38.5   -45 @ 20.91  no   $  0.63 -> $  9.41
    KXTRUMPAPPROVE-26OCT01-E38.6     6 @ 13.00  no   $  0.03 -> $ -0.78
    KXTRUMPAPPROVE-26OCT01-E38.8     1 @ 19.00  yes  $ -0.07 -> $  0.81
    KXTRUMPAPPROVE-26OCT04-E38.0    44 @  1.00  no   $  0.22 -> $ -0.44
    KXTRUMPAPPROVE-26OCT04-E38.1   145 @  9.69  no   $-11.60 -> $-13.77
    KXTRUMPAPPROVE-26SEP30-E38.7   -20 @ 22.00  no   $  0.40 -> $  4.40
Reported P&L went -$10.48 -> -$0.46, i.e. the dashboard had been
OVERSTATING the loss by $10.02 on $1000 of capital. Three short positions
that resolved in our favour were marked at their last mid instead of at 0.

backfill_settlements.py is committed rather than run as a throwaway, with
two deliberate properties:
  - It REUSES InventoryState.settle() instead of reimplementing the
    arithmetic. A backfill that computed settlement even slightly
    differently from the live path would leave a permanent inconsistency in
    the same column -- worse than the bug it fixes.
  - It only ADDS rows. server.py takes the latest mark per ticker, so a new
    row supersedes the stale one while the old rows remain as the record of
    what was believed at the time. Nothing is deleted.
Idempotent, verified: the settled rows carry net_yes_contracts = 0, so a
second run reports 37 already-flat and a $0.00 correction. It skips the six
CONTAMINATED_TICKERS on purpose -- their entry prices came from phantom
liquidity, so settling them would promote an artefact to a realised fact.
Ran against the live DB with the collector writing; no lock errors, latest
row 1s old, healthcheck exit 0 throughout.

### WAL confirmed (24h of evidence)
Every "database is locked" in the preceding 24h, and none after:
    03:33:20, :25, :30, :36, :41   the OFF-BOX PUSH's VACUUM INTO
    09:00:06                       the on-box backup
    20:51-20:54 (x4)               my own diagnostic queries
    since 21:37:40 (WAL deploy)    0, and 0 scoring-loop failures
Worth recording that BOTH backup jobs were causing this daily, not just the
09:00 one -- the 03:33 cluster from the off-box push had not been noticed
before. WAL removed a recurring daily data-loss source, not a one-off.

### Empty-row fix confirmed
0 empty orderbook rows out of 25869 written since the fix, against 90%
before. The ~229k/day of bloat is gone. The -wal file is holding at 0.1MB,
so checkpointing is keeping up with a once-per-second writer.

### Where the run actually stands
Balance $999.54. Three days of clean-book market making has produced
approximately nothing, which is the honest and expected result for passive
quoting on these markets -- and is the first time this project's headline
number has been trustworthy. Settlement's first live exercise is ~16:59 UTC
on 10-05, when the current OCT05 markets resolve; the log should show a
SETTLED line for each.

## 2026-10-05: reward income finally measured in dollars

Until now the dashboard measured COSTS in dollars (pnl_dollars) and
REVENUE in a unitless score, which made the one question this project
exists to answer -- does incentive income exceed adverse selection? --
impossible to read off the chart. Three changes fix that.

### 1. daily_pool was overstated by 74% and growing
It was period_reward / days_remaining: the WHOLE pool divided by the time
LEFT. A $60,000 24-hour program with 0.575 days remaining reported
$104,408/day, and the error grows without bound as a program nears its
end. The pool is fixed; only the time left shrinks.
_program_reward_per_second() now derives the rate from the program's own
start_date/end_date. Same case now reports $60,000/day. It returns None
rather than raising on missing, malformed or zero-length windows.

### 2. reward_dollars series added
Kalshi pools 2.0 of snapshot score per snapshot across all participants,
so our fraction of a pool is combined_score / 2.0. Multiplied by the
program's dollars-per-second and the time actually spent quoting, that
gives dollars, accumulated per bucket per ticker. It is plotted on the
SAME right-hand axis as pnl_dollars, deliberately: revenue and cost
belong in the same units, and the chart now shows directly whether the
green line outruns the red.

CURRENT READING: est. reward income $6,098 against simulated P&L $3.12
over the run so far. Verified by independent recomputation at $7,106 --
higher as expected, because that check uses max(ts)-min(ts) per ticker
(which counts gaps) while the series sums actual buckets.

THE NUMBER IS AN UPPER BOUND AND THE GAP IS THE WHOLE RESEARCH QUESTION.
$6,098 on $1000 of capital in five days is not a forecast and should not
be read as one. my_share models our order added on top of the existing
book with NOBODY REACTING; resting real size in a $60k/day program would
draw a competitive response that grows the normalisation denominator and
shrinks our share. reward_simulator.py's docstring has always said this
is the thing it cannot model. The distance between $6,098 and whatever
Phase 1 actually pays IS the competitive-response effect, and measuring
it is the point of the exercise. The caveat is stated in the dashboard UI
itself, not just here, so the number cannot be read naively.

### 3. quote sizing vs the inventory cap -- a three-way inconsistency
apply_yes_fill/apply_no_fill clamp a fill at max_inventory and justify
discarding the remainder as "rest assumed unfilled because we'd have
pulled the quote". But compute_quote never pulled anything -- it returned
the full DEFAULT_SIMULATED_QUOTE_SIZE regardless of inventory -- and
reward_simulator scores whatever size is resting. So at the cap the
simulation simultaneously assumed the quote was GONE (for fills) and
STILL THERE (for reward score), collecting incentive credit for liquidity
it had already decided it was not providing.
compute_quote now sizes each side by the room actually left on it
(yes_room = max_inventory - net, no_room = max_inventory + net), pulling
a side at 0 and returning None when both are capped. Fills, scoring and
quoting now agree, and the approach to the cap is gradual rather than a
cliff, which is also what a real maker would do.

Note this makes reward estimates slightly CONSERVATIVE relative to before
-- correctly so, since we no longer score size we are not providing.

Five tests added across the three changes, including the old daily_pool
formula pinned as a negative assertion so it cannot come back.
CONFIRMED live: dashboard HTTP 200, collector writing, healthcheck exit 0,
balance $1003.12.

## 2026-10-05: risk-adjusted reward optimisation (two changes, both corrections)

Asked to optimise for max risk-adjusted profit. What that turned out to
mean in practice was fixing two places where the system was destroying
revenue, not adding new optimisation. Both are measured, not speculative.

### 1. reward_density inflated as a program approached expiry
_reward_density computed reward_per_day as period_reward / days_remaining
-- the WHOLE pool divided by the time LEFT -- so a market's apparent
attractiveness grew as it neared expiry while the pool stayed fixed. This
is the SAME bug fixed in server.py's daily_pool on 10-05, in the place
that actually drives market SELECTION, and it was missed the first time.
Measured live: KXBIGSKYOPEN (7-day, $40,000 program, 0.97 days left)
scored 41.13 against a true 5.71 -- a 7.2x overstatement. The distortion
is worst exactly where it does the most damage: a nearly-expired market
has little pool left to win AND is about to go dead and force a switch.
This is how the selector came to pick NFL game markets after the 06:45
restart.
program_reward_per_day() now derives the rate from the program's own
start/end dates and is the SINGLE source of truth -- server.py imports it
rather than keeping its own copy, so the dashboard and the selector cannot
disagree about what a program pays.

### 2. nothing guarded revenue when switching
The "crowded" trigger asked only "is the replacement less contested?".
Nothing asked "does it still pay?", so an uncontested but nearly worthless
market always won. Measured cost on 10-05: the bot sat in NFL ladder
markets worth an estimated $13.11/day/market while Trump approval markets
worth $863.74/day/market were available -- a 66x gap -- for 30.3
market-hours, foregoing an estimated $1,074. That single decision dwarfs
the entire trading P&L the run has produced (+$3.12).
is_reward_preserving_switch() + MIN_REPLACEMENT_REWARD_DENSITY_FRACTION
(0.5) now require a replacement to pay at least half the incumbent's
density. Mirrors CROWDING_RELATIVE_IMPROVEMENT_FACTOR: we will give up
some revenue to escape genuine crowding, but not an order of magnitude.
"dead" is exempt -- we have to leave regardless, and anything beats a
settled book. Unknown density on either side is neutral (returns True), so
a transient screener failure cannot freeze switching, consistent with how
_adjusted_score already treats missing data.

### What was deliberately NOT done
No tuning of risk thresholds, inventory caps, skew ticks or quote sizing.
Those knobs would be fitted against reward_dollars, which is an UPPER
BOUND built on a share estimate that assumes no competitive response.
Optimising hard against a number whose dominant error term is unmodelled
would produce confident-looking overfitting, not profit. The existing risk
layer was left intact: _adjusted_score already damps reward density by
both crowding and volatility, the risk engine still pulls quotes at HIGH,
and quote sides are still capped by remaining inventory room. Tuning those
is a Phase 1 question, once real fills replace inferred ones.

CONFIRMED after deploy: selector now ranks the $60,000/day approval markets
top at a true density of 60.00; watch list re-selected to
OCT06-E38.4/.5/.6; collector writing; healthcheck exit 0; no warnings.
Four tests added, including the old density formula pinned as a negative
assertion and the exact 66x case as a blocked switch.

## 2026-10-06: dashboard build 34s -> 0.31s; and the first real adverse-selection event

### Build cost: 34s -> 0.31s steady state (110x)
Profiled rather than guessed, after being wrong twice doing the latter.

    component                          before    after
    _performance_history               13.29s    0.02s
    _qualifying_counts                  4.59s    0.02s
    _latest_per_ticker(quote_snapshots) 5.67s    ~0
    _latest_per_ticker(inventory_marks) 5.00s    ~0
    _latest_per_ticker(snapshot_scores) 1.24s    ~0
    FULL BUILD                        ~34s       0.31s

Three distinct causes:

(a) _performance_history ran two window functions across the whole 336h
lookback every tick (11.49s and 11.79s measured individually). All three
series are CUMULATIVE over immutable history, so the SQL now covers only
the newest bucket onward and the Python walk that turns per-bucket values
into running totals is redone each tick -- 1.6k buckets, free. The window
functions are gone too: plain GROUP BY with MAX(ts) gives identical answers
and was ~35% faster even uncached. Verified against the old implementation:
pnl_dollars and balance_dollars EXACT to the cent, reward_dollars within
$0.02, identical point counts (1655).

(b) _qualifying_counts grouped the ENTIRE snapshot_scores table, no time
bound, every ticker ever watched, to produce numbers for the 3 on screen.
Now scoped to the rendered tickers. Counts remain lifetime, which is the
intended meaning; only the rows scanned changed.

(c) THE SUBTLE ONE. Bounding the per-ticker queries with `WHERE ts >= ?`
barely helped -- 5.00s -> 4.26s -- because SQLite satisfies GROUP BY ticker
by walking the whole (ticker, ts) COVERING INDEX and will not use a
composite index's SECOND column to restrict a range. EXPLAIN QUERY PLAN
confirmed `SCAN ... USING COVERING INDEX idx_marks_ticker_ts`: 1.3M index
entries to return 3 rows. Split into two steps instead --
_active_tickers_since() forces the ts-leading index with INDEXED BY
(0.000s), then every detail query is scoped by ticker where equality seeks
are fast (0.042s). Added idx_quote_snapshots_ts, which that table lacked,
and the probe degrades gracefully to an unhinted query if an index is
missing on an older DB.

WS_TICK_SECONDS 15.0 -> 3.0, following the note left in the code: the right
value is a function of the build cost, and at 0.31s a 15s tick left the
dashboard needlessly stale. Measured through the real WebSocket: 3.3/3.5/3.2s
cadence, load 0.71 with a client attached, ~10% duty cycle.

### The first real adverse-selection event -- and a risk blind spot
Balance went $1003.12 -> $857.44. Essentially all of it is one market,
KXTRUMPAPPROVE-26OCT06-E38.5, realised -$132.54 between 12:00 and 16:00 UTC.

What happened: the mid went 7.50c -> 54.50c -> 84.50c, an 11x repricing on
a news event (Trump approval crossing 38.5). We had accumulated a large
NO position while YES was cheap -- fills include 1500 contracts of NO at
88c, 655 at 89c, 519 at 90c, 467 at 92c, i.e. selling YES at 8-12c -- and
YES then settled toward 85c. The flatten machinery closed it out near flat
(net -9), converting unrealised into realised. This is exactly the risk
adverse_selection_sim.py exists to measure, and it is the first time it has
been measured on clean data: ONE repricing cost 13% of capital in ~3 hours.

THE BLIND SPOT, which is the actionable part. Through that entire move:
    volatility_level: LOW in 12280 snapshots, ELEVATED 105, HIGH 30
    risk_level:       ELEVATED 12382, HIGH 33
Volatility read LOW through 99.1% of an 11x repricing, so the quote was
never pulled -- at 15:36, with the mid at 84.50, we were still showing
YES 100@81c / NO 100@13c. RiskEngine measures volatility as the stdev of
TICK-TO-TICK mid changes in cents. A large move delivered smoothly, or as a
gap between samples, produces small per-tick changes: the proxy measures
CHOPPINESS, not DISPLACEMENT. It cannot see a trend by construction.
Note the news signal DID fire at 12:13, ~40 minutes before the move, and
fed risk as one input without ever escalating to HIGH.

NOT FIXED, deliberately: adding a displacement term (|mid now - mid N
minutes ago|, relative to price) alongside the stdev term is the obvious
candidate, and the news signal firing 40 minutes early suggests it is
already predictive enough to act on. But that is a change to the risk
model's definition, it would alter every risk_level in the run, and it
wants deciding rather than slipping in during a performance pass.

## 2026-10-06: displacement risk term, calibrated on the event that exposed the gap

The 10-06 incident (-$132.54 on an 11x repricing, volatility_level LOW in
99.1% of it) came down to a structural blind spot: RiskEngine measured
volatility as the stdev of TICK-TO-TICK mid changes, which is CHOPPINESS.
A move delivered smoothly, or as a gap between samples, has small per-tick
changes and reads calm no matter how far the price travels.

### The fix
A second, independent term: peak-to-trough RANGE of the mid over a trailing
window. volatility_level is now the worse of (stdev, displacement), so
quoting_policy and the dashboard react to either with no change on their
side, and both terms are exposed separately -- the incident was hard to
diagnose precisely because one "volatility: LOW" badge hid which quantity
was being measured. displacement_cents is now logged per quote snapshot
(new column, migrated in place) so the next event is readable from data.

### Calibration -- measured, not chosen
Measured in CENTS, not percent, because P&L is linear in cents
(net_contracts x price_move). A first attempt normalised the range by
distance-to-nearer-bound and was DISCARDED during calibration: it made
penny markets look explosive, and a normal market scored higher (3.33) than
the event market (2.14). Absolute cents separates cleanly.

Trailing 300s range, event market vs six normal markets (~260k snapshots):
        threshold   event    normal
        >= 8c        4.9%    0.0% - 1.2%
        >= 15c       1.4%    0.0%  (all six)

Then replayed the REAL mid series through the actual new RiskEngine rather
than trusting the offline metric:
    EVENT  OCT06-E38.5   LOW 95.2%  ELEV 3.4%  HIGH 1.4%   first HIGH 12:48
    norm   OCT07-E38.2   LOW 100.0% ELEV 0.0%  HIGH 0.0%
    norm   OCT06-E38.4   LOW  98.8% ELEV 1.2%  HIGH 0.0%
    norm   OCT06-E38.6   LOW  99.6% ELEV 0.4%  HIGH 0.0%
    norm   OCT05-E38.2   LOW 100.0% ELEV 0.0%  HIGH 0.0%
Zero false HIGH on every normal market. That matters more than catching
more of the event: HIGH pulls the quote, a pulled quote earns no incentive
reward, and reward is the revenue.

### Honest limitation: it is REACTIVE
On the 10-06 event the term first crossed 15c at 12:48:07, when P&L was
already -$78.35 of the eventual -$132.54. It would have prevented roughly
$54 of $132 (41%), not the loss. Useful, not a solution. A displacement
measure cannot be predictive by construction -- it needs the move to have
happened.

### The news signal CANNOT carry the predictive half -- measured
The obvious next idea was that news fired at 12:13, ~30 minutes before the
move, so news + any displacement should go straight to HIGH. That idea is
dead, and the measurement kills it:
    news_signal is active 61.2% of the time across 1.63M quote snapshots
    and 95-100% of the time on many individual markets.
It fired 30 minutes early because it fires essentially ALWAYS. Routing it
to HIGH would pull quotes ~61% of the time and destroy the reward income
the strategy exists to earn. NOT DONE.

Corollary worth recording, because it reframes the whole risk picture: an
always-on news signal pins risk_level at ELEVATED, which is why the event
showed ELEVATED in 12382 of 12415 snapshots. ELEVATED therefore carries
almost no information, and only HIGH actually changes behaviour. The real
fix for the predictive half is to make news relevance SELECTIVE (tighten
the keyword heuristic, or cut NEWS_SIGNAL_DECAY_MINUTES from 45), not to
escalate it. That is its own piece of work and was not bundled in here.

CONFIRMED live after deploy: displacement_cents logging (0.0 on calm
markets), collector writing, no warnings, healthcheck exit 0.
Three tests, including a PERFECTLY smooth ramp whose tick-to-tick stdev is
exactly zero -- the pathological case the old measure cannot see by
construction. It fails on the pre-fix engine with AttributeError.

## 2026-10-06: risk signals calibrated against reality; run ready to reset

Both advisory risk inputs were measured against actual price behaviour.
One was wider than the markets' entire lifetime; the other had NEGATIVE
predictive value. Between them they caused 79.9% of all ELEVATED risk
while volatility and inventory were both LOW.

This mattered because ELEVATED is not cosmetic: it widens the quote by
WIDEN_TICKS_ELEVATED, and quoting away from the Reference Price lowers the
distance multiplier in the reward formula. So two uninformative signals
were quietly suppressing the revenue the project exists to measure, 81.5%
of the time.

### event_signal: window was wider than the market's life
EVENT_CALENDAR_WARNING_HOURS was 48, but the Trump approval contracts run
on a 24-HOUR cycle -- every snapshot was inside the window, so the signal
could never turn off (60.0% active; the rest is markets with no calendar
data).
Calibrated on 300s mid displacement bucketed by hours-until-close, six
markets:
        0-1h     0.0% of snapshots >= 8c   (book already emptying)
        1-2h    11.3%
        2-4h    10.7%
        4-8h     5.1%
        8-24h    0.4%
        24-48h   0.0%
Risk is concentrated in 1-4h before close. Set to 4.0h: keeps both
high-risk buckets with margin, excludes the 8-48h dead zone that caused
the always-on behaviour.

### news_signal: measured to be ANTI-predictive
P(mid displacement >= 8c within 30 min) after a news hit vs at a random
moment, per market:
        OCT06-E38.5   11.7%  vs  14.5%
        OCT06-E38.4    5.2%  vs   7.5%
        OCT06-E38.6    3.1%  vs   4.1%
        OCT05-E38.2    0.0%  vs   0.0%
        OCT05-E38.4    0.0%  vs   0.0%
        OCT04-E38.1   18.4%  vs  20.8%
A news hit made a large move LESS likely than a coin-flip moment, in EVERY
market. Combined with firing 61.2% of the time, it was not a warning --
it was a constant.
NEWS_SIGNAL_AFFECTS_RISK = False gates it out of the composite.
news_events collection, the news_signal field and the dashboard flag are
all DELIBERATELY KEPT: 15711 logged articles are research material for a
better relevance heuristic, and re-enabling is a one-line flag once
relevance can beat the baseline above. Only the effect on risk_level is
removed.

This also retires my own earlier hypothesis, recorded here because the
reasoning was wrong in an instructive way: the 10-06 incident log showed
news firing ~30 minutes BEFORE the repricing, which looked predictive. It
fired 30 minutes early because it fires essentially always. A signal that
is on 61% of the time will always appear to precede any event.

### Projected effect, replayed through the new engine
Simulating a news hit on EVERY tick (worst case), five markets, 66788
samples:
        before:  LOW 15.9%  ELEVATED 81.5%  HIGH  2.6%
        after:   LOW 88.3%  ELEVATED 11.3%  HIGH  0.5%
ELEVATED at 11.3% now tracks the measured displacement risk (10-11% in the
1-4h pre-close window) instead of being a constant. CONFIRMED live after
deploy: risk=LOW on calm markets with news=0 event=0, collector writing,
no warnings, healthcheck exit 0.

### ON RESETTING THE RUN
This was the last KNOWN behaviour-affecting defect, so the run is now
ready to reset. For the record, why it should not have been reset before:
the data spans SIX behaviour-changing deploys since the 10-01 "official
start" (phantom orderbook 10-02, dead-market detection and settlement
10-04, quote sizing 10-04, reward density 10-05, displacement risk and
these signal fixes 10-06). The longest behaviourally-consistent stretch
was ~28 hours. Resetting before fixing the news/event defect would have
started a 14-day clock on code that was widening every quote for no
reason.

Reset means declaring a measurement-start timestamp, NOT wiping the
database -- same convention as the existing ts >= 1790836411 filter and
CONTAMINATED_TICKERS. The 10-06 adverse-selection event is the single most
valuable observation this project has produced and must survive.
Continuity is otherwise good: exactly ONE gap over 5 minutes in the whole
run (the 4.5h settled-market stall on 10-04).

Accepted caveat: a bug has been found on every status check so far, so
"wait for stability" could run forever. The bound is to reset now and
record future changes in this file as they happen, rather than restarting
the clock each time.

## 2026-10-07: reset DEFERRED ~2 days by decision; what to watch meanwhile

Decision (user, 2026-10-07): let the run continue roughly two more days
before declaring the measurement-start timestamp.

Rationale, and it is better than resetting immediately: the 10-06 risk
calibration (displacement term, event window 48h -> 4h, news gated out of
the composite) has only a couple of hours of live data behind it. Waiting
means the window starts on code that has been OBSERVED to behave, not just
tested and projected. It also finally produces the first
behaviourally-consistent multi-day stretch this project has had -- the
previous best was ~28 hours.

### Falsifiable predictions from the 10-06 changes
Stated now so the wait produces an ANSWER rather than just elapsed time.
If these do not hold, the calibration is wrong and the reset should wait
further:
  1. risk_level ELEVATED settles near ~11% of snapshots, not ~81%.
     Projection was LOW 88.3% / ELEVATED 11.3% / HIGH 0.5%.
  2. displacement HIGH fires only on genuine repricings. It fired on 1.4%
     of the event market's snapshots and 0.0% on four calm markets; a
     materially higher rate in live trading means the 15c threshold is too
     tight.
  3. combined_score per snapshot RISES relative to the pre-10-06 baseline.
     This is the one that matters commercially: ELEVATED was widening every
     quote by WIDEN_TICKS_ELEVATED 81.5% of the time, and quoting away from
     the Reference Price lowers the reward distance multiplier. Removing an
     uninformative permanent widening should show up as more reward capture.
     Pre-fix baseline for comparison: mean combined_score 0.02767 (10-05
     post-density-fix window) and 0.0428 on the best market.

### Disk: fine for the wait, TIGHT for the full window after it
Measured 10-07: 23% used, live DB 1.86GB, on-box backups 606MB, growth
~363-390MB/day (gz deltas 27.6MB/24h).
    day +2   10-08 (reset)   steady  2.98GB   peak-during-backup  5.62GB
    day +9   10-15           steady  6.48GB   peak-during-backup 11.86GB
    day +16  10-22 (+14d)    steady  9.99GB   peak-during-backup 18.11GB of 20GB
The two-day wait is comfortable. A FULL 14-day window after the reset is
not: ~1.9GB of headroom at peak, inside the error bars of a linear growth
estimate.

The peak is driven by shape, not volume: both backup_db.py and
push_offbox_backup.sh write a FULL UNCOMPRESSED copy (live-DB-sized) before
gzipping it, so peak ~= 2x live + retained backups. Options, in rough order
of leverage, NONE yet done:
  - stream the snapshot through gzip instead of writing raw-then-compress,
    which removes a whole live-DB-sized temp from the peak;
  - KEEP_LAST 5 -> 3 (~1.1GB at that size, modest);
  - reduce orderbook_snapshots sampling rate (changes the data, so a
    measurement decision rather than an ops one);
  - resize the Droplet volume (costs money, user's call).
Not urgent today and deliberately not bundled into the reset, but it must
be settled BEFORE committing to a 14-day window ending ~10-22.

## 2026-10-08: predictions confirmed; HIGH now latches; P&L still negative

### The three 10-06 predictions all held
  1. ELEVATED settles near ~11%  -> ACTUAL LOW 96.6% / ELEV 2.7% / HIGH 0.8%
     (better than the 88.3/11.3/0.5 projection). Gating verified: of 8409
     elevated snapshots, volatility (2482) and event proximity (5925)
     account for essentially all; news contributes nothing.
  2. displacement HIGH stays rare -> 0.8%, firing on genuine repricings.
  3. combined_score RISES -> 0.02454 -> 0.06938, +182.7%.
CAVEAT on (3): causation NOT established. The watched markets also changed
between the two windows, and a query joining quotes to scores on exact
timestamps returned nothing, so the "quote now sits at the Reference
Price" mechanism is unverified. Direction and magnitude are consistent
with removing a permanent 2-tick widening, but it is association.

### But the losses continued, and the reason was structural
Balance $755.39, P&L -$244.61, down ~$105 in 24h on two markets
(-$69.34 and -$63.29). The displacement term fired ON TIME in both cases
-- P&L was +$0.78 and $0.00 at the first HIGH -- and the whole loss landed
afterwards anyway.

Cause: HIGH lasted EXACTLY one window and reset. On
KXTRUMPAPPROVE-26OCT07-E38.5: 10990 snapshots, 2 HIGH episodes, both
exactly 303s. The mid fell 59c -> 6c; HIGH fired near the start, expired
303s later while the price was still collapsing, the quote came back on,
and the position was filled down to -$75.73. A rolling range cannot see a
sustained trend -- once the move scrolls out of the window, each later
slice holds only the incremental step. Same blind spot as the original
stdev measure, one level up.

### Fix: latch HIGH for 30 minutes
Calibrated on how far the mid travels AFTER a trigger:
       market          +5m   +10m  +15m  +30m  +60m
       OCT06-E38.5    27.0  28.0  28.2  28.2  28.2
       OCT07-E38.5     4.5   4.5   5.0  39.5  39.5   <- the $69 loss
       NCAAFDELAY      6.2   6.5   6.8   7.0  10.5
       OCT07-E38.4    53.0  53.0  53.0  53.0  53.0
Most danger lands in 5-15 min, but OCT07-E38.5 did most of its damage at
the 30 MINUTE mark, so a 15-min hold would have missed the market this fix
exists for. Cost, as % of snapshots with the quote pulled: 0.0% on all four
calm markets at EVERY duration; 5.6-15.3% at 30m on markets that moved.
60m roughly doubles that for almost no extra coverage. Re-triggering
extends the hold rather than resetting it. Only HIGH latches -- ELEVATED
merely widens, so it stays instantaneous.

### Replayed against the real losses -- honest, mixed result
       market          HIGH%  episodes  longest   loss covered while HIGH
       OCT07-E38.5      6.4%     1       3524s         74.9%
       OCT06-E38.5      5.6%     2       2290s         30.9%
       NCAAFDELAY      15.2%     2       2091s          0.0%
       3 calm markets   0.0%     0          0s           n/a
Strong on the extended move it was built for, partial on the original
incident, and USELESS on NCAAFDELAY -- which collapsed 62c -> 27.5c in
under 29 minutes, booking $49.52 in a single window. That is the
irreducible limit stated when the term was added: no backward-looking
measure can precede a gap. Worth recording as a measured fact rather than
a caveat.

Zero cost on calm markets at every tested duration, which is what makes
the hold affordable: a pulled quote earns no reward, and reward is the
revenue.

### Where this leaves the run
Trading P&L is -$244.61 against $14,648 of estimated reward income. The
reward figure remains the UPPER BOUND (no competitive response modelled),
so the honest read is unchanged: a real and growing measured cost against
an unreliable revenue estimate. The adverse-selection cost is now the
best-measured quantity in the project, which is itself the point of
Phase 0.

## 2026-10-09 INCIDENT: my latch tripled exposure to a ruinous exit path

Balance $755.39 -> $292.85, P&L -$707.15, a $462 loss in 28.6h. The
proximate cause was a change I made.

### What happened
The 10-08 latch worked exactly as designed -- HIGH episodes went from a
hard 303s cap to 2098s/3951s. But HIGH does TWO things and I only reasoned
about one: it pulls the quote AND it arms hard_stop_flatten, which crossed
the spread UNCONDITIONALLY, at whatever price happened to be resting.
Holding HIGH ~6x longer exposed that path ~3x more often:
        forced-flatten rate   0.15/h -> 0.45/h
        average exit price    54.7c  -> 86.1c
        exits at >= 95c       838    -> 1586 contracts
Those are 322-404 contract positions sold into a 1-cent bid. Three markets
ended flat with large REALISED losses (-$174.47, -$139.74, -$114.48).

I got the diagnosis right and the consequence wrong. The latch was never
the defect -- it is correct behaviour sitting on top of a broken exit path
that predates it (99c exits appear in the data before 10-08 too). What I
failed to do was ask what ELSE the HIGH state triggers.

### Step 1: stopped the bleeding immediately
DISPLACEMENT_HIGH_HOLD_SECONDS -> 0, which makes the hold expire the
instant it is set and reproduces pre-latch behaviour exactly, without
reverting any file. Verified by unit check before deploying.

### Step 2: fixed the exit path, two defects
(a) NO PRICE BOUND. flatten_position crossed at any price.
MAX_FLATTEN_SLIPPAGE_CENTS = 10.0 now refuses beyond that. Calibrated on
44 historical flattens: slippage p50 2.5c, p75 6.0c, p90 26.5c, max 51.5c,
with a clean break -- everything is <= 7.5c or >= 15c. 10c keeps every
normal exit and blocks exactly the tail.
Replayed over all 44: 37 kept, 7 REFUSED, and those 7 carried $722.20 of
slippage -- essentially the entire $707 loss. (That is the cost realised BY
crossing. Holding is not free either; it caps the position at its own
settlement value, which is bounded and now correctly accounted.)

(b) NO DEPTH CHECK. flatten_position took the WHOLE position at the best
price regardless of resting size -- a 404-contract exit assumed the touch
absorbed all 404. It now walks the levels via _walk_book() for a true
average and a true fillable size, with the remainder left on the book. A
test had encoded the fantasy directly: 200 contracts resting, a
420-contract exit asserted at the top price.

Refusing is safe because of two properties that did not exist earlier in
the run: the hard stop only fires at HIGH, where compute_quote already
returns None so a held position cannot grow; and settlement accounting
(10-04) resolves a held position at its true 0/100 outcome instead of
freezing a stale mark.

### What I did NOT do, and why
The plan said "don't hard-stop while the quote is already pulled". Taken
literally that deletes the hard stop: it only fires at HIGH, and HIGH
always pulls the quote, so the condition is always true. That would remove
tail protection entirely. The slippage bound achieves the actual intent --
stop ruinous forced exits -- while keeping the stop available when the book
can absorb the position at a sane price.

### Latch re-enabled
Back to 1800.0 now the exit path is bounded. Two changes at once is exactly
the mistake that caused this incident, so this one wants watching: the
specific thing to check is whether HIGH episodes now coincide with REFUSED
flattens in the log rather than 90c+ crossings.

## Target architecture: nothing runs on the laptop

GOAL (stated 2026-10-02): this laptop is a viewing terminal only. No
process, timer, or stored run data on it that the system depends on. The
Droplet is the whole system; the laptop can be closed, wiped, or lost
without affecting the run or the ability to recover it.

Rationale beyond convenience: both 2026-10-02 failures were caused by
the laptop being load-bearing. A laptop is the worst possible host for
anything unattended -- it sleeps, it moves networks, and macOS TCC can
revoke a path's executability (which is exactly what killed the backup
agent) without anything failing loudly.

### What still runs here (full footprint, audited 2026-10-02)
1. server.py dashboard, started via ./manage.sh (pid varies), bound
   127.0.0.1:8000. REDUNDANT -- the Droplet already runs
   kalshi-dashboard.service.
2. launchd agent com.kalshi.droplet-backup-pull, daily 10:00, pulls the
   whole DB over SSH. This is the only piece doing a job nothing else
   does: it is the off-box copy.
3. State: droplet_backups/ (848MB), data/ (226MB, the laptop-era
   09-03..09-05 run), and ~/.ssh/kalshi_droplet_backup.
Nothing else -- no crontab, no other Kalshi launchd agent, no login
items. The 5-minute healthcheck and its ntfy.sh alerting already live on
the Droplet and have no laptop dependency.

### Step 0 (BLOCKER): real SSH access from this laptop
The only key here is ~/.ssh/kalshi_droplet_backup, which is
command-restricted to the Droplet's forced push_latest_backup.sh and has
no shell. It cannot open a tunnel and cannot administer anything. Every
step below needs ordinary root/sudo access, so first add a normal keypair
to the Droplet's authorized_keys (via the DigitalOcean web console, or
from whichever machine the 10-01 deployment was performed from).
Deliberately NOT done by widening the backup key's permissions: that key
is restricted precisely so a laptop compromise cannot reach the Droplet,
and that property is worth keeping.

PREPARED 2026-10-02: an ordinary admin keypair now exists at
~/.ssh/kalshi_droplet_admin (ed25519). Its public half needs adding to
the Droplet's root authorized_keys, after which every step below can
proceed. It was generated WITHOUT a passphrase so the migration can run
unattended -- add one with `ssh-keygen -p -f ~/.ssh/kalshi_droplet_admin`,
and/or remove it from the Droplet once the migration is finished, since
after that the laptop only needs tunnel access to view the dashboard.

### Step 1: retire the laptop dashboard -- DONE 2026-10-03
The laptop dashboard is stopped and port 8000 is free. The tunnel was
verified end to end first: `ssh -L 8001:127.0.0.1:8000` then an HTTP 200
and a live WebSocket payload from the Droplet's own
kalshi-dashboard.service showing $1162.05 at 10-02 19:28 local, 894
balance points, 2 markets, 10 fills -- i.e. the real run, not the 09-05
corpse the laptop had been serving. KALSHI_DB_PATH is still set in the
laptop .env; harmless with the local dashboard stopped, and it stays
until the laptop is fully retired so a local view remains possible if
needed.

OPEN, found while verifying: the Droplet dashboard takes ~41 SECONDS to
produce one WebSocket tick, against a design that ticks once per second,
so it is permanently backlogged and saturates the single vCPU for as
long as a browser is attached (load average went 0.05 -> 1.03 with one
client). main.py was still fine at the time, but do not leave the
dashboard open for long stretches mid-run; it competes with the process
that is actually collecting the data.

UPDATE 2026-10-03: the two ts-leading indexes were applied, and they did
NOT fix it. Recording the real numbers because the original diagnosis
above was only half right.

Applied idx_scores_ts and idx_marks_ts to the live DB (services stopped
first: journal_mode is `delete`, not WAL, so CREATE INDEX takes an
exclusive lock, and logger.py connects with Python's default 5s
busy_timeout -- long enough to throw "database is locked" at main.py if
the build overran it). Build took 1.39s and 1.94s, DB grew 943MB ->
968MB, collection gap ~1 minute, main.py resumed writing within 1s with
no warnings. Added to logger.py's schema block so new databases get them.

EXPLAIN QUERY PLAN confirms they are used (`SCAN snapshot_scores` ->
`SEARCH snapshot_scores USING INDEX idx_scores_ts (ts>?)`), but the
dashboard payload went 39.79s -> 39.51s, i.e. unchanged. Two reasons:
  1. PERFORMANCE_HISTORY_LOOKBACK_HOURS is 336h and the run had 74.7h of
     data, so `ts > cut` matches EVERY row. An index range scan over all
     rows is no cheaper than a table scan. These indexes only start
     paying off once a run exceeds the lookback window -- kept for that
     reason, at ~25MB on a ~1GB DB.
  2. _performance_history was never the bulk of the cost anyway.

Measured breakdown of the 39.79s, which is where the real fix lies:
    _latest_per_ticker(orderbook_snapshots)   12.84s
    _performance_history                       7.00s
    _latest_per_ticker(quote_snapshots)        5.34s
    _qualifying_counts                         2.67s
    _latest_per_ticker(snapshot_scores)        2.60s
    _latest_per_ticker(inventory_marks)        2.52s
_latest_per_ticker accounts for ~23s of it and no ts index can help it:
it has no WHERE clause at all. It computes
`ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY ts DESC)` across the
ENTIRE table and then keeps rn=1, so it reads every row of every table
on every tick just to find the newest row per ticker.

NOT YET APPLIED, but measured read-only on the live DB -- rewriting it as
`SELECT ..., max(ts) FROM {table} GROUP BY ticker`, which can use the
existing (ticker, ts) indexes:
    quote_snapshots       7.15s -> 1.00s   (7x)
    inventory_marks       4.61s -> 0.90s   (5x)
    snapshot_scores       4.33s -> 0.80s   (5x)
    orderbook_snapshots  11.89s -> 8.29s   (1.4x only -- wide table, and
                                            the query does SELECT *)
Equivalence was checked rather than assumed: the aggregates agree on all
31 tickers. An initial comparison appeared to disagree on 3 rows, but
those were the 3 currently-watched tickers and the gap was ~3.2s of
main.py writes landing between the two queries, plus `SELECT *, max(ts)
AS ts` yielding two columns named ts. Worth ~16s of the 39s; the
orderbook case wants narrower column selection too, not just the
rewrite. This is a server.py code change rather than a schema change, so
it carries no write-lock risk to the run.

### Original plan for Step 1 (kept for context)
kalshi-dashboard.service already runs on the Droplet next to the live DB,
bound to localhost. So this is purely subtractive:
  - view it with:  ssh -L 8000:127.0.0.1:8000 root@<droplet-ip>
    then open http://127.0.0.1:8000/ as before
  - ./manage.sh stop server   on the laptop
  - drop the KALSHI_DB_PATH line from the laptop .env (the override added
    on 10-02 exists only to make the *laptop* dashboard non-stale; once
    the dashboard is viewed on the Droplet it is obsolete)
This also removes a real cost: pointed at the 662MB Droplet snapshot, the
laptop dashboard sustained 56-99% CPU, because each tick re-scans a
336-hour window over a DB that is now ~6x the size it was when those
queries were written. On the Droplet it reads the live DB locally, but
note the 1vcpu sizing makes that query cost worth watching there too.

### Step 2: move the off-box backup to a Droplet-side push (the real work)
The laptop pull must be replaced, not just deleted -- it is the only
thing protecting against the Droplet's own disk failing. Replace
"laptop pulls from Droplet" with "Droplet pushes to object storage":
  - on-box: keep the existing daily sqlite3 .backup(), then
    `VACUUM INTO` + gzip before upload. SQLite full of numeric rows
    compresses hard, which matters at the growth rate below.
  - upload: rclone/aws-cli to an S3-compatible bucket on a cron, with a
    write-only credential stored on the Droplet.
  - provider choice: DigitalOcean Spaces is the least work (same
    console/billing) but shares a blast radius with the Droplet it is
    insuring. Backblaze B2 or any non-DO S3 target is a genuinely
    independent failure domain, which is the entire point of an off-box
    copy. Recommend the independent provider.
  - retention: enforce with a bucket lifecycle rule, not a shell `rm`.
  - MUST alert on failure to the existing ntfy.sh topic. The 10-02
    incident was not "the backup broke", it was "the backup broke and
    said nothing for two days." A push with no failure alert reproduces
    that bug in a new location.

WRITTEN 2026-10-02, NOT YET DEPLOYED: deploy/push_offbox_backup.sh plus
kalshi-offbox-backup.{service,timer}, a config example, and install steps
in deploy/README.md. It implements all of the above: free-space precheck
before VACUUM INTO (so the backup cannot be what fills the disk),
integrity_check on the snapshot before upload, read-back size
verification rather than trusting rclone's exit code, ntfy alert on every
failure path via an ERR trap, and a separate alert if the source DB has
gone stale (a backup faithfully archiving a dead main.py is still bad
news). Retention is left to a bucket lifecycle rule on purpose.
Syntax-checked only -- it targets Linux and cannot run on macOS, so the
first run must be done by hand on the Droplet and watched.

Also noted while writing it: backup_db.py, push_latest_backup.sh, the
healthcheck, both systemd units, root's crontab and the authorized_keys
forced-command entry exist ONLY on the Droplet and are in no repository.
If that disk failed, the recovery procedure would be lost with the thing
it recovers. Copy them into deploy/ and commit -- listed at the bottom of
deploy/README.md.

### Step 3: remove the laptop timer
After Step 2 is confirmed uploading: `launchctl unload` then delete
~/Library/LaunchAgents/com.kalshi.droplet-backup-pull.plist, and mark
pull_droplet_backup.sh as a manual-only recovery tool (or delete it).
Order matters -- do not remove the pull before the push is verified, or
there is a window with exactly one copy of the data.

### Step 4: get the laptop-era data off the laptop
data/paper_trading.sqlite is the ONLY copy of the 09-03..09-05 run (the
Droplet never had it). It is superseded for analysis purposes but it is
still unique data: upload it to the same bucket before deleting anything
locally. droplet_backups/ can be deleted freely once Step 2 is verified,
since those are copies of data the Droplet and the bucket both hold.

### Capacity risk to check while doing this (NOT yet verified)
Measured from two consecutive pulls: 216805376 bytes at 09-30 19:26 ->
661721088 bytes at 10-02 18:00, i.e. ~229MB/day and not obviously
linear. A 14-day run from 10-01 therefore lands around 3GB live, and the
on-box daily backups multiply that by the retention count on a
1vcpu-1gb Droplet (nyc1, default 25GB disk). Seven retained on-box
backups of a multi-GB DB will not fit. Check `df -h` and the on-box
retention policy on the Droplet -- if nothing else, this is a second
silent-failure candidate of exactly the 10-02 kind, since main.py will
simply start failing writes when the disk fills.

## Next steps (in order)
1. Let the live run actually accumulate data -- the pipeline now picks
   its own watch list at startup and can switch away mid-run on its own
   (risk engine, reference-price quoting, market switcher armed and
   throttle-fixed, news + event-calendar signals, autonomous selection),
   but has very little real runtime behind it yet.
2. DONE 2026-10-01: main.py deployed to a DigitalOcean Droplet as a
   systemd service, so data collection no longer stops when this laptop
   sleeps. Remaining follow-on: finish removing the laptop from the
   system entirely (dashboard + off-box backup) -- see "Target
   architecture: nothing runs on the laptop".
3. Run continuously for 1-2 weeks minimum, spanning quiet periods and at
   least one volatility event.
4. Analyze data/paper_trading.sqlite: compare avg snapshot score x period
   reward against worst-case adverse-selection swings per market.
5. Decision gate: only markets where reward plausibly exceeds worst-case
   risk proceed to Phase 1 (small live pilot, real orders, new code not
   yet built).
