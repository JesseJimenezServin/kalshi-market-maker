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
  inventory_marks, orderbook_snapshots, incentive_program_snapshots tables)
- main.py -- orchestrates all of the above; NO live orders are ever
  placed anywhere in this codebase (verified via grep, 2026-08-31)
- server.py -- read-only FastAPI dashboard; streams data/paper_trading.sqlite
  over /ws once per second, never talks to Kalshi
- templates/dashboard.html -- single-page dashboard UI (markets table,
  order book depth bars, recent fills, performance chart)
- test_logic.py -- offline unit tests, no network required, 9/9 passing

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

## Known simplifications / open items
- "My share" of reward score still assumes we're the ONLY provider at our
  exact price level -- overestimates share if others already quote there.
  True fix requires Phase 1 real fills, not fixable in pure simulation.
- Quoting policy is naive (joins best bid, no inventory skew, no
  news-window awareness) -- intentional baseline for Phase 0.
- No hosting/deployment yet -- main.py and server.py both currently run
  locally only.
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
