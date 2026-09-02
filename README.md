# Kalshi Liquidity Lab — Phase 0 (Paper Trading, Zero Capital Risk)

This project connects to Kalshi's market data (demo environment recommended
to start) and **simulates** what would happen if you ran a basic
two-sided liquidity-providing quote — without ever placing a real order.
It logs two things over time:

1. **Reward simulation** — your hypothetical share of each Liquidity
   Incentive Program snapshot, computed using Kalshi's actual published
   scoring formula (Reference Price, Discount Factor, per-side
   normalization).
2. **Adverse-selection simulation** — the hypothetical inventory and P&L
   you'd have accumulated from real trade flow crossing your simulated
   resting quotes.

**No orders are ever placed by this code.** It is entirely read-only.

## ⚠️ Important: this sandbox cannot reach Kalshi's servers

I built and unit-tested all the logic here, but this development
environment's network is allow-listed to a specific set of domains and
**does not include Kalshi's API** (`api.elections.kalshi.com` /
`demo-api.kalshi.co`). A live connection attempt from here returns an
explicit `host_not_allowed` denial. This is expected — **run this on your
own machine**, where it will reach Kalshi normally. I validated:
- All modules import and compile cleanly.
- The order-book reconstruction (snapshot + delta + gap detection) against
  synthetic messages shaped like Kalshi's documented WebSocket schema.
- The reward-scoring formula against hand-computed examples.
- The inventory/P&L simulator against hand-computed fills.

See `test_logic.py` — run `python3 test_logic.py` any time to re-verify
the logic on your machine too.

## Setup (on your own machine)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

1. Create a Kalshi account and switch to the **demo** environment first
   (no real money, same API).
2. In the Kalshi dashboard, generate an API Key ID and download the RSA
   private key (`.pem` file).
3. Set environment variables:

```bash
export KALSHI_ENV=demo
export KALSHI_API_KEY_ID="your-key-id"
export KALSHI_PRIVATE_KEY_PATH="/path/to/your/private_key.pem"
```

## Step 1 — (optional) preview what the pipeline would pick

```bash
python3 incentive_screener.py
```

This lists currently-active Liquidity Incentive Program markets, ranked by
a crowding-aware adjusted score (reward density, dampened for real
resting depth and recent trade-price volatility — see
`incentive_screener.py`). You don't need to act on this manually: by
default (`config.AUTO_SELECT_WATCHLIST = True`), `main.py` runs this same
ranking itself at startup and picks its own watch list — see Step 2.
Running it yourself first is just a way to preview what it'll likely
choose, or to sanity-check the ranking.

## Step 2 — watch list: autonomous by default, manual as a fallback

With `config.AUTO_SELECT_WATCHLIST = True` (the default), `main.py`
autonomously selects `config.NUM_MARKETS_TO_WATCH` markets at startup via
`incentive_screener.select_watchlist` — each candidate's REAL crowding
ratio must be at/below `config.SWITCH_CANDIDATE_MAX_CROWDING_RATIO`, the
same bar `market_switcher.py` requires of a replacement later, so day-0
selection and later mid-run switches use one consistent standard. No
manual curation needed. This is runtime-only, like the market switcher
below — a restart re-runs selection from scratch.

If auto-selection finds nothing eligible (or `AUTO_SELECT_WATCHLIST` is
set to `False`), it falls back to the manual `config.py`
`WATCHED_MARKET_TICKERS` list — edit that list yourself if you want a
fixed, hand-picked watch list instead.

## Step 3 — run the paper-trading pipeline

```bash
./manage.sh start main
```

Use `./manage.sh` (start/stop/restart/status) instead of running
`python3 main.py` directly and managing it by hand -- it uses a PID file
so restarts always target the exact right process. (main.py also refuses
to start a second instance of itself no matter how it's launched -- see
`singleton_lock.py` and the 2026-09-01 incident in `PROJECT_STATUS.md`:
running multiple copies by accident corrupted a data-collection window by
writing to the same SQLite file from several unaware processes at once.)

This will:
- Stream the live order book and trade tape for your watched markets.
- Every ~1 second, compute your hypothetical Liquidity Incentive Program
  snapshot score (assuming you're resting `DEFAULT_SIMULATED_QUOTE_SIZE`
  contracts at the best bid on each side — see `config.py`).
- On every real trade print, update a simulated inventory position and
  P&L, respecting `MAX_SIMULATED_INVENTORY` as a hard cap (mimicking a
  policy of pulling quotes once you hit a position limit).
- Log everything to `data/paper_trading.sqlite`.

Let it run for a while — ideally the full 1–2 week window we discussed —
to gather enough data across different market regimes (quiet periods and
at least one volatility event) to be meaningful.

## Step 4 — watch it live in the dashboard

```bash
./manage.sh start server
```

(Or `./manage.sh start both` to start main.py + server.py together, and
`./manage.sh status` any time to check what's currently running.)

Then open http://127.0.0.1:8000/ in a browser. This is a read-only FastAPI
server that streams live updates from `data/paper_trading.sqlite` over a
WebSocket once per second -- it never talks to Kalshi and places no orders.
It shows the watched markets table (target size, discount factor, daily
pool, net P&L), order book depth bars, a recent-fills table, and a
performance chart. Safe to run alongside `main.py` at any time (or on its
own, before you've collected any data).

## Step 5 — analyze results

Query `data/paper_trading.sqlite` directly, e.g.:

```bash
sqlite3 data/paper_trading.sqlite "
SELECT ticker,
       AVG(combined_score) AS avg_snapshot_score,
       COUNT(*) AS snapshots
FROM snapshot_scores
GROUP BY ticker;
"

sqlite3 data/paper_trading.sqlite "
SELECT ticker, MIN(unrealized_pnl_cents), MAX(unrealized_pnl_cents), MAX(ABS(net_yes_contracts))
FROM inventory_marks
GROUP BY ticker;
"
```

The question Phase 0 exists to answer: **does the average snapshot score
(→ converted to expected reward dollars using the program's period_reward)
plausibly exceed the worst-case swings in `unrealized_pnl_cents`?** If
reward income is consistently dwarfed by adverse-selection swings on a
given market, drop it from the watch list. If it looks favorable across
several quiet weeks, that market is a candidate for the small live pilot
(Phase 1) — not before.

## Known simplifications in this Phase 0 model (read before trusting numbers)

1. **"My share" estimation.** The public order book only shows aggregate
   size per price level, not individual participants. `reward_simulator.py`
   estimates your share by adding your hypothetical size on top of the
   existing level, crediting only your own contribution in the numerator
   (verified 2026-09-02 — an earlier comment here claiming it double-counted
   others' resting size was itself wrong and has been corrected). What it
   genuinely can't model is competitive *response*: other real liquidity
   providers might change their own behavior once you're resting there
   too, which no snapshot of the public book can reveal. Treat Phase 0
   reward numbers as an optimistic estimate, not a guarantee — validating
   that gap requires Phase 1 real fills.

2. **Fill-probability heuristic in adverse-selection sim.** `main.py` now
   computes `my_share_estimate` from the live resting size at the exact
   touched price level at the moment of the trade (see
   `compute_my_share_at_touched_price` in `main.py`), rather than a fixed
   placeholder. It shares the same competitive-response caveat as #1
   above (assumes other participants' behavior at that price doesn't
   change in response to us).

3. **Quoting policy.** `quoting_policy.compute_quote` rests at the live
   Reference Price (not just the touch) before applying inventory skew
   and risk-based widening/pulling — see `risk_engine.py` for the
   LOW/ELEVATED/HIGH escalation (now also fed by `event_calendar.py`'s
   close-time proximity flag, alongside volatility, inventory, and news)
   and `market_switcher.py` (enabled as of 2026-09-02) for switching away
   from a market that sustains HIGH risk or high crowding. Still
   intentionally simple relative to a fully optimized policy — the point
   of Phase 0 is a measurable baseline to improve on, not a finished
   strategy.

4. **Target Size / Discount Factor.** `main.py` now seeds each watched
   market's `MarketRuntimeState` with the real `target_size` and
   `discount_factor` pulled live from `screen_incentive_programs` at
   startup (see `build_market_states` in `main.py`). Watched tickers with
   no active incentive program are skipped, not defaulted.

None of these are hard to fix — they're flagged so Phase 0's numbers are
read correctly (as a promising/discouraging *signal*, not a precise
forecast) while we tighten the model with real observed data.

## File map

| File | Purpose |
|---|---|
| `config.py` | Environment, watch list, thresholds |
| `auth.py` | RSA-PSS request signing |
| `rest_client.py` | REST wrapper (markets, incentive programs, account) |
| `orderbook.py` | Local order-book state from snapshot + deltas |
| `ws_client.py` | Authenticated WebSocket client, reconnect + resync |
| `incentive_screener.py` | Ranks active liquidity-incentive markets |
| `reward_simulator.py` | Implements the actual scoring formula |
| `adverse_selection_sim.py` | Hypothetical inventory + P&L from trade flow |
| `logger.py` | SQLite persistence |
| `event_calendar.py` | Feeds real close/resolution-time proximity into `risk_engine.py` |
| `main.py` | Orchestrates everything |
| `server.py` | Read-only FastAPI dashboard, streams `data/paper_trading.sqlite` over `/ws` |
| `templates/dashboard.html` | Dashboard single-page UI (markets, depth, fills, chart) |
| `test_logic.py` | Offline unit tests (no network needed) |
