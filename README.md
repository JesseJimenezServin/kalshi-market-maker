# Kalshi Market Maker

A paper-trading market maker for [Kalshi](https://kalshi.com) prediction
markets. It was built to collect rewards from Kalshi's **Liquidity Incentive
Program**, which pays people who keep orders resting near the best price.

It ran from September to October 2026, first on a laptop and then 24/7 on
a cloud server. It streamed live
order books, simulated a two-sided quote against real trade flow, and logged
about 2.8 million snapshots. **It never placed a real order.** Every trade
described here was simulated against live market data.

**Status: concluded (2026-10-26).** The strategy lost money in simulation,
and the analysis below explains why. Originally called *Kalshi Liquidity Lab*
(`kalshi_liquidity_lab`); older commits use that name.

> **Sister project:** [Kalshi-Bot](https://github.com/JesseJimenezServin/Kalshi-Bot)
> asked a different question (do new markets open mispriced?) and reused the
> lessons learned here.

---

## The result

| | |
|---|---|
| Simulated capital | $1,000 |
| Main run (2026-09-30 → 10-13) | **−$746.73 (−74.7%)** |
| Passive (maker) volume | 359,637 contracts |
| Share of the reward pool captured | 1.819%, steady |
| Both sides qualifying for rewards | 99.7% of 2.8M snapshots |
| Loss caused by forced exits alone | **$1,267.01** |

The surprising part is that the passive quoting roughly broke even. Forced
exits, which crossed the spread into thin books, cost more than the entire
loss. With those exits priced at the mid, the same run would have ended
about **+$520**, though that is an upper bound.

After a reset, the run repeated the pattern from 10-13 to 10-22: passive
fills **+$123**, taker exits **−$823**. Turning off taker exits was the
final change. The project was stopped before that version finished its
planned 14-day measurement window.

Full write-ups:
- [`AUTOPSY.md`](AUTOPSY.md): what happened, measured from the database
- [`BLUEPRINT.md`](BLUEPRINT.md): the redesign that the data called for
- [`DATA.md`](DATA.md): the dataset and which parts of it can be trusted
- [`PROJECT_STATUS.md`](PROJECT_STATUS.md): the day-by-day engineering log,
  incidents included

---

## What I learned

**1. Know exactly what you are paid for.** The reward formula pays for
*resting size at good prices*. It pays nothing for trading. A fill removes
the size that was earning the reward and leaves you holding inventory. I
built a market maker that tried to maximise fills, when the business needed
the opposite: rest as much size as possible and get filled as little as
possible. That one inverted assumption explains the results better than any
single bug.

**2. On binary markets, flow is directional.** Mean fill imbalance was
**47%**, and some markets filled **100% on one side**. When news arrives,
informed traders hit the side that is about to be wrong, so a two-sided
quote fills on the side you least want. A market maker needs both sides to
fill. Here they mostly didn't.

**3. A risk control is only as good as the action it triggers.** On 10-08 I
made the "high risk" state last 30 minutes instead of about 5. The
measurement was right and well calibrated. But "high risk" also armed a
forced exit that crossed the spread with no price limit. Keeping the state
on 6× longer tripled exposure to that exit, and the run lost **$467 in 28
hours**, its worst day. I never asked what else depended on the state I
changed.

**4. Change one thing at a time, or you are not measuring anything.** Six
behaviour-changing deploys in 14 days meant the longest window with a
single consistent strategy was about 28 hours. Each fix was justified on
its own. Together they meant no version of the strategy was ever properly
tested.

**5. Suspect your profits before your losses.** Early on, the simulation
showed a profit. It came from a floating-point bug: order-book levels
cancelled to zero left residues like `5.55e-17` and were never removed.
**31.6% of logged price levels were phantoms**, and $161 of the $162
"profit" came from the six markets with the worst-corrupted books. Every
market with a clean book made roughly nothing. (The sister project uses
`Decimal` throughout for this reason.)

**6. Record raw inputs, not just your conclusions.** The database stored the
price *we* quoted, not the price each trade printed at. So the most
important question, whether a quote one tick deeper would still have been
filled, couldn't be answered until the raw trade tape was added on 10-13.

**7. Test signals against the base rate.** The news signal fired 61% of the
time and was **anti-predictive**: large moves were *less* likely after a
news hit than at a random moment. An event signal used a 48-hour window on
markets that lived 24 hours, so it never switched off. Together these two
signals widened quotes about 80% of the time and suppressed reward income.
Fixing them raised the reward score by 183%.

**8. A simulation can't show how competitors react.** The 1.819% reward
share assumes nobody responds to our resting size. A real order in a
$60,000/day program would attract competition. Every reward figure here is
therefore an upper bound, and only a small live test could have measured
the real number. That test was never run.

**What worked:** a monitor that caught a stalled market without anyone
watching. Two-layer backups, checked by actually restoring one. WAL-mode
SQLite, which stopped a daily data-loss bug. A query rewrite that made the
dashboard **110× faster** (34s → 0.31s). And a fill model that proved honest
when checked: our simulated fills were 2.7% of real volume against a
modelled 2.2%.

---

## How it works

```
Kalshi WebSocket ──► orderbook.py (snapshot + deltas, gap detection → resync)
                        │
                        ├─► reward_simulator.py      Kalshi's published scoring formula
                        ├─► quoting_policy.py        where to rest, with inventory skew
                        ├─► risk_engine.py           volatility / displacement / inventory → LOW…HIGH
                        └─► adverse_selection_sim.py fills inferred from real trade prints
                                   │
                              logger.py ──► SQLite (WAL) ──► server.py dashboard (FastAPI, WebSocket)
```

- **Market selection**: `incentive_screener.py` ranks live incentive
  programs by reward per unit of crowding. `market_switcher.py` rotates out
  of markets that become crowded, volatile or dead.
- **Capital**: `capital_allocator.py` splits the $1,000 across watched
  markets by reward and risk.
- **Read-only by construction**: the REST client refuses anything but GET,
  so no code path can place an order.
- **Operations**: one DigitalOcean droplet running systemd services. A
  healthcheck alerts by push notification. A daily on-box backup plus an
  off-box backup to Backblaze B2 runs an integrity check before upload and
verifies the uploaded size afterwards.
  See [`deploy/`](deploy/).
- **Analysis**: `offset_sweep.py` replays recorded books to trade reward
  score against fill rate at each distance from the reference price.
  `backfill_settlements.py` books resolved markets at 0 or 100.

About 8,500 lines of Python. 75 offline tests in `test_logic.py`, no network
needed.

## Running it

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # Kalshi API key ID + private key path
python3 test_logic.py         # offline tests
./manage.sh start main        # paper-trading pipeline → data/paper_trading.sqlite
./manage.sh start server      # dashboard on http://127.0.0.1:8000
```

You need a Kalshi API key; an unfunded account is enough, since nothing
here trades. The 3.2 GB dataset from the run is archived offline and not in
this repository.

---

*Built with [Claude Code](https://claude.com/claude-code) as a pair
programmer. I set the direction, made the decisions and reviewed the
results.*
