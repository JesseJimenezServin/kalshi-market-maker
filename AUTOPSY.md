# Phase 0 Autopsy — 2026-09-30 to 2026-10-13

Paper-trading run of a two-sided market-making strategy against Kalshi's
Liquidity Incentive Program. $1,000 of simulated capital, no real orders at
any point.

**Outcome: −$746.73 (−74.7%).** Balance $1,000.00 → $253.27.

Every figure below is measured from `data/paper_trading.sqlite`, not
estimated. The six markets in `config.CONTAMINATED_TICKERS` are excluded
throughout (their order books were corrupted — see §4.1).

---

## 1. Headline numbers

| Metric | Value |
|---|---|
| Final P&L | **−$746.73** (realized −$673.66, unrealized −$73.07) |
| Passive (maker) volume | 359,637 contracts |
| Forced (taker) exit volume | 14,497 contracts (3.9% of all volume) |
| **P&L per passive contract** | **−0.2076c** |
| Quoted spread width, mean | 9.04c |
| Mean share of reward pool | **1.819%** of a theoretical 2.0 |
| Both sides qualified | **99.7%** of 2,823,597 snapshots |
| Peak single-market position | 319 contracts (mean 26) |
| Estimated reward income (upper bound) | $30,392 |

A maker capturing half of a 2c spread earns +1.00c per contract. We earned
−0.21c on a 9c quoted width. **The spread was never the problem; we simply
never collected it.**

---

## 2. The single largest cause: execution, not strategy

Forced exits cost **$1,267.01** in slippage against the mid at the moment
of each exit:

| Reason | Events | Contracts | Slippage cost |
|---|---|---|---|
| `hard_stop_flatten` | 39 | 12,949 | **$1,249.31** |
| `market_switch_volatile` | 3 | 493 | $10.77 |
| `market_switch_crowded` | 17 | 789 | $5.61 |
| `pre_resolution_flatten` | 9 | 266 | $1.33 |
| **Total** | **68** | **14,497** | **$1,267.01** |

`flatten_position` crossed the spread at **whatever price happened to be
resting**, with no price bound and no depth check. On a thin book that
meant selling 322–404 contracts into a 1-cent bid. It also assumed the top
of book absorbed the entire position regardless of resting size.

**The counterfactual is the most important number in this document:**

```
actual P&L                           −$746.73
slippage paid on forced exits       +$1,267.01
P&L with exits at the mid            +$520.28
```

Exiting at the mid is impossible — you always pay something — so +$520 is
an upper bound, not an achievable figure. But the direction is unambiguous:
**the passive business was roughly break-even or better, and the exit path
destroyed it.**

The natural experiment agrees:

| Group | Markets | P&L | Per contract |
|---|---|---|---|
| With a `hard_stop_flatten` | 19 | −$698.65 | −34.88c |
| Without one | 57 | −$48.09 | −4.16c |

8.4× worse per contract. Partly selection — the hard stop fires *because*
a market moved against us — but the $1,267 of slippage is a direct,
causal measurement independent of that.

---

## 3. The fundamental strategic flaw: it was never market making

**Mean fill imbalance across 31 markets: 47.0%.** Individual markets:

| Market | YES ct | NO ct | Imbalance |
|---|---|---|---|
| `KXMLBPLAYOFFHOST-26CHC-8` | 3,000 | 0 | **100.0%** |
| `KXTRUMPAPPROVE-26OCT08-E38.4` | 22 | 2,723 | 98.4% |
| `KXTRUMPAPPROVE-26OCT01-U38.5` | 44 | 4,181 | 97.9% |
| `KXTRUMPAPPROVE-26OCT08-E38.3` | 166 | 4,785 | 93.3% |

A market maker's business model requires **both sides to fill**, so the
spread is captured repeatedly while inventory stays near zero. That is not
what happened. Fills arrived overwhelmingly on one side.

This is structural, not bad luck. On a binary prediction market, when
information arrives it is *directional*: informed flow hits the side that
is about to be wrong. A passive two-sided quote is therefore filled
precisely on the side it should not want, and the other side never trades.
Quoting a 9c width is irrelevant if you only ever transact one half of it.

The inventory profile confirms the shape:

| Position | Share of time |
|---|---|
| 0–25 contracts | 66.7% |
| 25–100 | 25.3% |
| 100–200 | 6.1% |
| 200–300 | 1.9% |
| 300+ | 0.003% |

Usually flat, occasionally very exposed — the opposite of a market-maker's
desired profile. With 100 contracts quoted per side against a 333-contract
cap, **roughly 3.3 consecutive one-sided fills exhaust the entire risk
budget.** The 5-tick inventory skew was far too weak to counteract that.

---

## 4. Defects found during the run

Each of these was real, measured, and fixed. They matter because they mean
**no single strategy was under test for most of the run** — six
behaviour-changing deploys in 14 days, longest consistent window ~28 hours.

### 4.1 Phantom order book levels (10-02) — invalidated all prior results
`apply_delta` removed a price level only on `new_size <= 0`. Sizes are
floats (Kalshi's `_fp` fields are fractional), so a level cancelled to
nothing landed on residue like `5.55e-17` and was never removed. **31.6% of
all logged price levels were phantoms**; in 51,208 sampled snapshots the
*best* level was one. One book showed a best YES bid of 12c at size
2.84e-14 where reality was 9c with 634 contracts — a 1.5c mid error on a
10c market.

This manufactured the entire reported profit at the time: **$161.06 of
$162.04 came from the six markets whose books were 39–60% phantoms.** Every
clean-book market made approximately nothing.

### 4.2 Reward-rate arithmetic (10-05, twice)
`period_reward / days_remaining` — the whole pool divided by the time
*left*, so a market's apparent value **inflated as it approached expiry**.
A 7-day $40,000 program with 0.97 days left scored 41.13 against a true
5.71. This drove market *selection*, and caused the bot to hold NFL markets
worth ~$13/day/market while $864/day/market markets sat available — an
estimated **$1,074 of foregone reward in one day.**

### 4.3 Volatility blind to trend (10-06)
Risk used the stdev of *tick-to-tick* mid changes, which measures
choppiness. During an 11× repricing (7.5c → 84.5c) `volatility_level` read
LOW in **12,280 of 12,415 snapshots (99.1%)**, so the quote was never
pulled. Cost: −$132.54.

### 4.4 Risk signals that were constants (10-06)
`event_signal` used a 48-hour window against markets with a **24-hour
life** — it could never turn off (60.0% active). `news_signal` fired 61.2%
of the time and was measured **anti-predictive**: P(≥8c move within 30 min)
was *lower* after a news hit than at a random moment, in all six markets
tested. Together they caused **79.9% of all ELEVATED risk** while
volatility and inventory were both LOW.

This was not cosmetic. ELEVATED widens the quote, and quoting away from the
Reference Price lowers the reward distance multiplier — so two
uninformative signals suppressed revenue 81.5% of the time. Fixing them
raised mean `combined_score` from 0.02454 to 0.06938 (**+182.7%**).

### 4.5 Settlement never booked (10-04)
No settlement path existed, so leaving a settled market froze the position
at its last observed mid. A binary market resolves to 0 or 100 — the P&L
was a known fact recorded as an estimate. Backfilling corrected the
reported figure by **+$10.02**; the dashboard had been *overstating* the
loss.

### 4.6 Dead markets held indefinitely (10-04)
The switcher's only criterion was crowding, and an empty book reads as
**perfectly uncrowded (0.0 vs 50.0 for a busy one)**. The bot sat on three
`finalized` markets for 4.4 hours producing no data, writing ~229,000 empty
rows/day. Every quality signal *improves* as a market dies, so liveness had
to become its own check.

---

## 5. The change that made things worse

**10-08: latching displacement HIGH for 30 minutes. Cost: −$467.18 in 28.6
hours** — the single worst day of the run, and self-inflicted.

The latch itself was correct and well-calibrated: HIGH episodes went from a
hard 303-second cap (one measurement window, expiring mid-trend) to
2098s/3951s, with 0.0% false positives on four calm markets.

What I failed to do was ask **what else the HIGH state triggers.** HIGH
both pulls the quote *and* arms `hard_stop_flatten`. Holding HIGH ~6×
longer exposed the unbounded exit path ~3× more often:

```
forced-flatten rate   0.15/h → 0.45/h
average exit price    54.7c  → 86.1c
exits at >=95c        838    → 1,586 contracts
```

**Lesson: a risk control is only as good as the action it triggers.** The
diagnosis was right, the second-order consequence unexamined. Changing one
variable at a time would have caught it in hours instead of a day.

---

## 6. What worked

- **Monitoring.** The healthcheck added on 10-03 caught the dead-market
  stall autonomously — the only incident detected rather than stumbled
  upon. Alert throttling kept it legible.
- **Backups.** Two layers, verified by an actual **restore** (download from
  B2 → gunzip → `integrity_check` ok → 726,967 rows). An unrestored backup
  is a hypothesis.
- **Qualification.** 99.7% both-sides qualification across 2.8M snapshots.
  The mechanics of *being eligible* are solved.
- **Reward capture.** A steady **1.819% share** of the pool. Small, but
  real and consistent.
- **WAL + query work.** Eliminated a daily data-loss source (both backup
  jobs were causing `database is locked`) and cut the dashboard build from
  34s to 0.31s, a 110× improvement.
- **The adverse-selection simulator.** It did its job: `my_share` averaged
  1.4–2.7% and our fills landed at 2.7% of real market volume against a
  2.2% modelled share. The fill model was never the optimistic part.

---

## 7. What did not work

- **Two-sided passive quoting as a P&L strategy.** −0.21c/contract on a 9c
  width, 47% fill imbalance.
- **The hard stop.** Cost $1,249 to avoid losses that were smaller.
- **Advisory risk signals.** News was anti-predictive; event proximity was
  a constant. Both actively suppressed revenue.
- **Inventory skew at 5 ticks.** Far too weak — 3.3 one-sided fills
  exhausted the risk budget.
- **The measurement discipline.** Six behaviour-changing deploys in 14 days
  meant no strategy was ever properly under test. The reset was deferred
  repeatedly, with justification each time, and never happened.

---

## 8. The economics, stated plainly

This is the finding that should drive everything next.

```
our measured share of a pool            1.819%
$60,000/day pool  →  $1,091.64/day/market  →  3 markets = $3,274.92/day
$16,021/day pool  →  $  291.49/day/market  →  3 markets = $  874.46/day

measured adverse selection cost                    ~$50-70/day on $1,000
```

**The reward program is worth roughly 47× the adverse-selection cost at our
measured capture rate.** The strategy did not fail because the economics
are bad. It failed because:

1. $1,267 was destroyed by an unbounded exit path (fixable, fixed);
2. fills are a pure *cost* with no offsetting reward — and the design
   treated them as the business;
3. the 1.819% share is an **upper bound** that assumes no competitor
   reacts to our resting size, which Phase 0 structurally cannot test.

Point 2 is the conceptual error. **The reward formula pays for resting
size at good prices. It pays nothing for trading.** Being filled reduces
your resting size, lowers your score, and hands you inventory risk. In a
rebate-harvesting business, a fill is a *loss event* — and this strategy
was built to maximise them.

---

## 9. Run metadata, for anyone analysing the data

- DB: `data/paper_trading.sqlite`, 3.22 GB, ~2.82M quote snapshots.
- Excluded tickers: `config.CONTAMINATED_TICKERS` (6 markets, 39–60%
  phantom books, $161.06 of fictitious profit).
- Pre-run testing noise: filter `ts >= 1790836411`.
- One collection gap > 5 min in the whole run: 4.5 h on 10-04 (the
  settled-market stall).
- Known-stale window: all data before 2026-10-02 22:35 UTC predates the
  phantom-orderbook fix and should not be used for P&L or reward analysis.
- Behaviour-changing deploys: 10-02 22:35, 10-04 14:05, 10-04 14:43,
  10-04 23:48, 10-05 12:30, 10-06 23:58, 10-08 00:38, 10-09 18:51.
- Infrastructure: single DigitalOcean droplet (1 vCPU, 1 GB), systemd,
  WAL-mode SQLite, daily on-box + off-box backups to Backblaze B2.
