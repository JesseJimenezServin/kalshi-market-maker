# Blueprint — what has to change for this to work

Written 2026-10-13, directly from the measurements in `AUTOPSY.md`. Read
that first; every claim here rests on a number in it.

---

## 1. The thesis: this is not a market-making business

The run was built as a market maker that happened to earn rebates. The data
says it must be the other way round.

```
P&L from passive quoting          −0.21c per contract  (over 359,637 contracts)
measured share of reward pool      1.819%  →  up to $3,275/day across 3 markets
measured adverse selection         ~$50-70/day
```

**Kalshi's Liquidity Incentive Program pays for resting size at good
prices. It pays nothing whatsoever for trading.** A fill does three bad
things at once: it consumes the resting size that was earning the rebate,
it lowers the snapshot score, and it hands us inventory risk. There is no
offsetting revenue.

So the correct objective is not "capture the spread". It is:

> **Maximise qualifying resting size at the best achievable price
> multiplier, for the longest possible time, while being filled as little
> as possible.**

Under that objective a fill is a *loss event*. The previous design
maximised fills. That single inversion explains the shape of the run better
than any individual bug.

Two things follow immediately, and both are already proven in the data:
- Qualification is **solved** — 99.7% both-sides across 2.8M snapshots.
- Capture is **real but small** — a steady 1.819% share.

The job is to keep the first, grow the second, and stop paying for fills.

---

## 2. The one experiment that decides everything

**How does score trade off against fill probability as we rest further from
the Reference Price?**

The reward formula gives an order at `d` ticks below the reference price a
multiplier of `discount_factor ** d`. With a typical `discount_factor` near
0.9, one tick back costs ~10% of score. But resting one tick back puts us
behind more queue, and fill probability should fall much faster than 10%.

If that asymmetry exists, the strategy becomes profitable by *backing off*.
If it does not, passive rebate harvesting on these markets is not viable at
any offset and the project should stop.

**This is answerable from data already collected** — 2.8M order book
snapshots plus the trade flow that drove fills. No new live running
required. Build it as an offline sweep:

```
for offset in 0..5 ticks behind the reference price:
    replay every snapshot:
      - score   = reward_simulator.score_hypothetical_order(... offset ...)
      - fills   = process_trade_for_inventory(... offset ...)
    report: mean score, fills per hour, inventory profile, P&L, score/fill ratio
```

Deliverable: a table of offset → (reward share, fill rate, adverse
selection). Pick the offset that maximises net, or conclude none does.

**This is the first thing to build. Everything else is downstream of it.**

---

## 3. Design changes required

### 3.1 Never cross the spread on a schedule
Already fixed (`MAX_FLATTEN_SLIPPAGE_CENTS = 10.0`), but the principle
needs to be explicit: **$1,249 of the $747 loss was slippage on
`hard_stop_flatten`.** The exit path cost more than the thing it protected
against.

- Exit **passively** by default: quote the closing side at a price we would
  be happy to get, and wait.
- Cross only when the book genuinely offers a sane price, which the
  slippage bound now enforces.
- Prefer **holding to settlement** over crossing at a bad price. Settlement
  accounting (added 10-04) now books this correctly at 0/100, so a held
  position is bounded and properly valued. This is a real option now and
  was not before.

### 3.2 Make inventory asymmetric, not symmetric
With 100 contracts per side against a 333 cap, **3.3 one-sided fills
exhaust the risk budget**, and mean fill imbalance was **47%**.

- Size the two sides **independently**, by remaining room on each (done
  10-04) *and* by realised fill imbalance on that market.
- When one side has filled repeatedly, stop quoting it entirely rather than
  skewing 5 ticks. A 5-tick skew was measurably too weak.
- Treat sustained one-sided fills as a **market-quality signal**: a market
  that only ever fills one side is a market to leave, regardless of its
  reward density.

### 3.3 Select markets by reward per unit of fill risk
Current selection ranks by reward density alone. It should rank by
reward density **divided by** expected fill cost, where fill cost is
estimated from that market's own recent fill imbalance and displacement
history. A $60k/day pool that repriced 11× is worth less than a $16k/day
pool that never moves.

### 3.4 Delete, don't fix, the advisory signals
`news_signal` was measured **anti-predictive** and `event_signal` was a
constant. Both are already gated out. Do not reintroduce either without
first demonstrating, on data, that it beats the baseline it is supposed to
warn about. The bar is explicit: P(≥8c move in 30 min | signal) must exceed
P(≥8c move in 30 min) by a margin, per market.

### 3.5 Keep risk measurement, shrink what it triggers
The displacement term and its 30-minute latch are sound and
well-calibrated (0% false positives on calm markets). The problem was never
the measurement — it was that HIGH armed a ruinous action. Keep the signal;
make its only consequence "stop quoting that side", never "cross the
spread".

---

## 4. What to keep, unchanged

- The reward simulator. It models Kalshi's published mechanics and achieves
  99.7% qualification.
- The adverse-selection fill model. Verified honest: `my_share` 1.4–2.7%,
  our fills at 2.7% of real market volume against a 2.2% modelled share.
- Settlement accounting, dead-market detection, the slippage bound, the
  displacement risk term.
- The whole operational layer: WAL, two-layer verified backups, the
  self-reporting healthcheck, the droplet-only deployment.

None of that needs rethinking. It is the strategy layer that does.

---

## 5. The unknown Phase 0 cannot resolve

`my_share` models our order **added on top of the existing book with nobody
reacting**. Resting real size in a $60,000/day program will draw a
competitive response that grows the denominator and shrinks our share.
Every reward figure in this project — including the $30,392 headline — is
an **upper bound** for that reason.

No amount of further simulation fixes this. It requires Phase 1: a small
number of *real* resting orders, where our fills arrive from Kalshi's own
`fills` stream instead of being inferred, and our share is observed rather
than modelled.

**Phase 1 should be sized to answer that question and nothing else** —
minimum qualifying size, one market, long enough to observe whether the
share holds.

---

## 6. Phased plan with explicit gates

**Phase 0.5 — offline, no new running (days)**
1. Build the offset sweep (§2) against the existing 2.8M snapshots.
2. Rebuild P&L attribution so spread capture, fill cost, slippage and
   settlement are separable *by construction*, not reconstructed afterwards.
3. **Gate:** does any offset show reward share materially exceeding fill
   cost? If no → stop, write it up, do not run Phase 1.

**Phase 0.6 — re-run the simulation, one variable at a time (1–2 weeks)**
4. Implement the chosen offset, asymmetric sizing, passive exits.
5. Freeze the code. **One** behaviour change per measurement window,
   recorded in `PROJECT_STATUS.md` — the autopsy's clearest process
   failure was six deploys in 14 days.
6. **Gate:** 14 consecutive days, no behaviour changes, net simulated P&L
   plus modelled reward positive with the competitive-response caveat
   stated.

**Phase 1 — minimum real capital (weeks)**
7. One market, minimum qualifying size, real orders.
8. Measure the **observed** share against the modelled 1.819%. That ratio
   is the single most valuable number this project can produce.
9. **Gate:** observed share within a stated tolerance of modelled → scale.
   Materially below → the rebate thesis is wrong and the project ends with
   a real answer.

---

## 7. What I will build first

In order, smallest useful increments:

1. **`offset_sweep.py`** — the §2 experiment. Offline, reads the existing
   DB, writes a table of offset → (score, fills/hr, inventory, P&L).
2. **Fix the P&L decomposition** so fill cost, slippage, spread capture and
   settlement are tracked as separate columns at write time rather than
   inferred later. The autopsy needed four ad-hoc scripts to separate them;
   that should be a schema property.
3. Only then touch quoting behaviour, and only one change per window.

Nothing in Phase 0.6 or beyond should be built until the §2 gate is
answered, because a negative answer there makes all of it worthless.

---

## 8. Reset, finally

The measurement window never got declared. Do it as part of Phase 0.6
step 5, not before — there is no point starting a 14-day clock while the
strategy is still being redesigned. Simulated capital should also be reset
to $1,000 at that point; the current $253.27 balance carries 14 days of
defect-driven losses that say nothing about the new design.
