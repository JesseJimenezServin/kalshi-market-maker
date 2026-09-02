# Kalshi Liquidity Lab -- Project Status

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
  on risk escalation, simulated hard-stop flatten (crosses the spread via
  adverse_selection_sim.InventoryState, still no real orders)
- market_switcher.py -- event-triggered market switcher: pure building
  blocks (crowding ratio, sustain+cooldown trigger logic, candidate
  search) -- NOT a continuous re-optimizer, opt-in via
  config.MARKET_SWITCHER_ENABLED (default False). Orchestration loop
  lives in main.py (market_switch_loop) to avoid a circular import with
  MarketRuntimeState.
- news_feed.py -- zero-cost news-awareness signal: polls GDELT's free
  Doc 2.0 API (no key required), rule-based keyword-overlap relevance
  scoring (no LLM), feeds a decaying news_signal into RiskEngine
- main.py -- orchestrates all of the above; NO live orders are ever
  placed anywhere in this codebase (verified via grep, 2026-08-31)
- server.py -- read-only FastAPI dashboard; streams data/paper_trading.sqlite
  over /ws once per second, never talks to Kalshi
- templates/dashboard.html -- single-page dashboard UI (markets table w/
  risk badge + qualifying-snapshot counts + net position, order book
  depth bars with the hypothetical quote overlaid, recent fills,
  performance chart, flash highlight on new fills/stop-losses)
- analysis.py -- read-only report: ESTIMATED dollar reward per ticker
  (snapshot_scores joined against incentive_program_snapshots) printed
  alongside real adverse-selection P&L from inventory_marks. No simulated
  cash balance -- P&L is already tracked correctly in cents.
- singleton_lock.py -- structural protection so only one instance of
  main.py or server.py can ever run at once (see Decisions log 2026-09-01)
- manage.sh -- start/stop/restart/status for main.py + server.py via
  PID files -- use this instead of hand-typed nohup/pkill commands
- test_logic.py -- offline unit tests, no network required, 14/14 passing

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
- News-awareness signal is zero-cost (GDELT + keyword heuristic, no LLM
  classification, no paid APIs). Quality is a known limitation vs. a
  true NLP/LLM classifier -- expect false positives/negatives; flagged as
  a future upgrade path if paper-trading data suggests it's too noisy or
  misses real signal.
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
- Two planned workstreams, not yet started: scheduled-event calendar
  awareness (feeding known event/resolution timing into the quoting
  policy or market screener as a risk flag) and crowding-aware market
  selection (accounting for how crowded a price level already is /
  observed volatility when ranking markets in incentive_screener.py,
  beyond today's simple reward-density heuristic).
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

## Next steps (in order)
1. Pick 2-4 active, low-volatility incentivized markets via
   incentive_screener.py, add tickers to config.py WATCHED_MARKET_TICKERS
2. Deploy main.py to an always-on host (VPS, systemd service) --
   not yet started
3. Run continuously for 1-2 weeks minimum, spanning quiet periods and at
   least one volatility event
4. Analyze data/paper_trading.sqlite: compare avg snapshot score x period
   reward against worst-case adverse-selection swings per market
5. Decision gate: only markets where reward plausibly exceeds worst-case
   risk proceed to Phase 1 (small live pilot, real orders, new code not
   yet built)
