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

## Step 1 — find markets worth watching

```bash
python3 incentive_screener.py
```

This lists currently-active Liquidity Incentive Program markets, ranked by
a rough reward-density heuristic (reward per day ÷ required Target Size).
Pick a handful that look interesting — ideally lower-volatility markets
without scheduled news in the near term (see the strategy notes we
discussed: weather, slow-moving economic ranges, non-marquee sports
pre-game, etc.).

## Step 2 — configure the watch list

Edit `config.py` and add the tickers you picked to `WATCHED_MARKET_TICKERS`.

## Step 3 — run the paper-trading pipeline

```bash
python3 main.py
```

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

## Step 4 — analyze results

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
   existing level — this **overestimates** your share if other real
   liquidity providers are already resting at your exact price. Treat
   Phase 0 reward numbers as an optimistic upper bound, not a guarantee.

2. **Fill-probability heuristic in adverse-selection sim.** `main.py`
   currently uses a fixed placeholder (`my_share_estimate = 0.15`) for
   how much of each trade print we'd have captured. This should be
   replaced with a book-state-aware calculation (using the resting size
   at the touched price level at the moment of the trade) before trusting
   the P&L numbers quantitatively — directionally useful now, not exact.

3. **Quoting policy is naive.** `MarketRuntimeState.refresh_quote_from_book`
   just joins the best bid on both sides. It doesn't yet implement
   inventory skew or pull quotes ahead of scheduled news. That's
   intentional for Phase 0 — we want a simple baseline to measure, and to
   improve on once we see where the naive policy struggles.

4. **Target Size / Discount Factor defaults.** `main.py` seeds each
   watched market with placeholder values (`target_size=1000,
   discount_factor=0.9`) rather than pulling the real per-market values
   from the incentive screener automatically. Wire `screen_incentive_programs`
   output into `MarketRuntimeState` before trusting absolute numbers —
   right now the screener and the scoring loop run independently.

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
| `main.py` | Orchestrates everything |
| `test_logic.py` | Offline unit tests (no network needed) |
