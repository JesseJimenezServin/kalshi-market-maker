# What you need to know about this dataset

`data/paper_trading.sqlite` — 3.22 GB, ~2.82M quote snapshots, covering
2026-09-30 to 2026-10-13. Written by `main.py` via `logger.py`.

**Recommendation up front: do not delete it.** Reasons in §5.

---

## 1. Trust boundaries — read before querying

The run spanned eight behaviour-changing deploys. Data is only comparable
within a window, and some of it is simply wrong. Four filters matter:

| Filter | Why |
|---|---|
| `ts >= 1790836411` | Excludes pre-official-start testing noise, including a ~10,778-row news flood and 50 switches from a continuous-re-optimizer bug. |
| `ts >= 1791070000` (10-02 22:35) | **Hard trust floor.** Before this, order books were up to 31.6% phantom levels. Any P&L, score or mid before this is fiction. |
| `ticker NOT IN config.CONTAMINATED_TICKERS` | Six markets with 39–60% phantom books that produced $161.06 of fictitious profit. |
| `ts >= config.PNL_EPOCH_TS` (10-13 00:00) | The Phase 0 reset. P&L before this reflects defects since fixed — see `AUTOPSY.md`. |

Reward/score data is trustworthy from the 10-02 floor onward. **P&L data is
only meaningful within a single deploy window**, and the longest such window
in Phase 0 was ~28 hours.

---

## 2. Tables, and what each is good for

| Table | Rows | Use it for |
|---|---|---|
| `orderbook_snapshots` | ~2.8M | **Required input to `offset_sweep.py`.** Full book state per tick. The bulk of the file. |
| `snapshot_scores` | ~2.8M | Reward capture: `combined_score`, `qualifies_yes/no`, `my_share_*`. The most trustworthy series in the DB. |
| `quote_snapshots` | ~2.8M | What we quoted and why: risk/volatility/inventory levels, `displacement_cents`, `crowding_ratio`. |
| `inventory_marks` | ~2.9M | Position and P&L over time, now with the `realized_passive/taker/settlement` decomposition. |
| `simulated_fills` | ~4.5k | Our inferred passive fills. **Records OUR quote price, not the trade price.** |
| `simulated_stop_losses` | 68 | Every forced exit, with reason. The $1,267 slippage finding came from here. |
| `trades` | from 10-13 | **Raw public tape.** New. The missing input for the fill half of the gate. |
| `incentive_program_snapshots` | ~900 | Pool size, target size, discount factor, program window. |
| `news_events` | ~15.7k | Articles. Kept as research material only — the signal was measured anti-predictive. |

---

## 3. The gap that mattered most

**The raw trade tape was never persisted until 2026-10-13.**

`simulated_fills` records the price *we* quoted, not the price the trade
printed at. So from Phase 0 data alone it is impossible to ask "would a
quote one tick deeper also have been filled?" — which is exactly the
question that decides whether this strategy can work.

`BLUEPRINT.md` originally claimed the gating experiment was answerable from
existing data. **That was wrong.** The score half is; the fill half needs
the tape, which now exists. Any future instrumentation decision should
default to persisting raw inputs, not just derived outputs.

---

## 4. Results already extracted (don't redo these)

- Reward capture: mean `combined_score` 0.03639 = **1.819% of pool**, both
  sides qualifying **99.7%** of 2.82M snapshots.
- **Score vs offset** (`offset_sweep.py`, 13,341 sampled books):
  offset 0 → 1 costs **46.5% of score**, far worse than the ~10%/tick that
  `discount_factor` alone implies, because backing off also lands us behind
  much more resting size. Qualification holds at 99.9% at every offset.
- **Fill rate vs offset, the gate** (2026-10-22, 24,259 tape trades): one
  tick back cuts fills 83% but reward dollars outweigh fill cost ~1000x, so
  offset 0 wins unless real reward is under ~1% of modelled. See the
  2026-10-22 entry in `PROJECT_STATUS.md`.
- Fill realism: our fills were **2.7% of real market volume** against a
  2.2% modelled share — the fill model was never the optimistic part.
- Fill imbalance: **47% mean**, markets up to 100% one-sided.
- Forced exits: **$1,267.01** of slippage, $1,249.31 of it `hard_stop_flatten`.
- Inventory: 66.7% of time under 25 contracts, peak 319, mean 26.

---

## 5. Retention — keep it

You asked whether to delete the raw data. **No, and specifically not
`orderbook_snapshots`**, for three reasons:

1. **It is the input to the gating experiment.** `offset_sweep.py` replays
   book state to compute score at each offset. Deleting it destroys the
   ability to answer the question the whole redesign hinges on.
2. **It holds the only record of the 2026-10-06 repricing** (7.5c → 84.5c),
   the single most informative event the project has produced and the basis
   for the displacement risk term.
3. **Disk is not under pressure** — 34% used, and the file is already
   backed up off-box to Backblaze B2 with a verified restore.

If space does become tight, prune in this order, and only then:
- `news_events` — research material, no analysis depends on it.
- `orderbook_snapshots` **older than the 10-02 trust floor** — already
  unusable for anything.
- `orderbook_snapshots` between the trust floor and the tape era, but
  **only after the offset sweep has been run and its output saved**.

Never prune `snapshot_scores`, `simulated_fills`, `simulated_stop_losses`,
`inventory_marks`, `trades`, or `incentive_program_snapshots` — they are
small and carry all the economics.

---

## 6. Reproducing the autopsy figures

Everything in `AUTOPSY.md` came from this DB with no external inputs. The
one thing that needed the Kalshi API was settlement results (`result`
field), used for the settlement backfill — `market_metadata` does not store
`close_time` or `result`, which is itself worth fixing if settlement
analysis matters later.
