"""
Read-only FastAPI dashboard for the Kalshi Market Maker paper-trading
pipeline.

This server NEVER talks to Kalshi and places no orders of any kind -- it
only reads data/paper_trading.sqlite, which main.py populates. It's safe
to run this alongside (or independently of) main.py at any time.

The `/ws` WebSocket pushes a fresh snapshot of the dashboard state once
per second, built entirely from SQL queries against the SQLite file:
  - snapshot_scores    -> per-market scoring (qualifies, share, combined score)
  - inventory_marks    -> per-market simulated inventory + P&L
  - orderbook_snapshots -> per-market order book depth (for the depth bars)
  - incentive_program_snapshots -> per-market target size / discount factor / pool
  - simulated_fills    -> the 10 most recent simulated fills
  - quote_snapshots    -> current hypothetical quote + risk level (plus its
                          individual inputs -- volatility/inventory/news/
                          event -- and our crowding-ratio "competition
                          level" proxy) per market (main.py logs this
                          purely for this dashboard -- server.py and
                          main.py are separate processes with no shared
                          memory)
  - market_metadata    -> real market title/subtitle per ticker (one row
                          per ticker, not a time series -- see _market_metadata)

Also derives, from the tables above:
  - a downsampled performance/balance history (reward score proxy, P&L,
    and starting-balance-adjusted balance over time -- see
    _performance_history)
  - a capital-allocation percentage breakdown, from each market's latest
    inventory_marks.max_inventory (see _capital_allocation)

Run with:
    python3 server.py
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import sqlite3
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import config
import incentive_screener
from logger import SimulationLogger
from singleton_lock import acquire_singleton_lock

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("server")

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app = FastAPI(title="Kalshi Market Maker Dashboard")

# Ensures the schema exists even if main.py has never been run yet, so the
# dashboard can start up and show an empty (rather than broken) state.
SimulationLogger()

RECENT_FILLS_LIMIT = 10


# Per-bucket reward accrual, memoised across ticks.
#
# reward_dollars is a CUMULATIVE series over immutable history: once a
# 5-minute bucket is complete its contribution can never change. Recomputing
# the whole lookback window every tick took the dashboard build from 9.3s to
# 40.7s on a 1.36GB database (measured 2026-10-05), pushing the duty cycle
# back to ~73% on a single vCPU shared with the collector -- undoing the tick
# optimisation from earlier the same day.
#
# Only the newest (partial) bucket and anything not yet seen needs
# recomputing. That also makes the query's `ts >=` predicate narrow enough
# for idx_scores_ts to actually help, instead of matching every row.
_reward_bucket_accrual: dict[int, float] = {}
_reward_bucket_end_ts: dict[int, float] = {}
_hist_score_bucket: dict[int, float] = {}
_hist_score_ts: dict[int, float] = {}
_hist_pnl_bucket: dict[int, dict[str, float]] = {}
_hist_pnl_ts: dict[int, float] = {}


def _program_reward_per_second(program) -> float | None:
    """
    Dollars of incentive pool paid per second, from the program's own
    start/end dates.

    NOT period_reward / days_remaining, which is what this used to be. That
    divides the WHOLE pool by the time LEFT, so on 2026-10-05 a $60,000
    24-hour program with 13.8h remaining reported a $104,408/day pool -- a
    74% overstatement that grows without bound as a program nears its end.
    The pool is fixed; only the time left shrinks.
    """
    if program is None:
        return None
    per_day = incentive_screener.program_reward_per_day(
        program["period_reward"], program["start_date"], program["end_date"]
    )
    return None if per_day is None else per_day / 86400.0


def _exclusion_clause(column: str = "ticker") -> tuple[str, list[str]]:
    """
    SQL fragment excluding config.CONTAMINATED_TICKERS, plus its parameters.

    Those markets' books were mostly phantom levels (see
    config.CONTAMINATED_TICKERS), so their P&L is an artefact. They must be
    excluded at REPORT time rather than deleted, and excluding them matters
    beyond tidiness: _performance_history freezes a market's last mark and
    keeps counting it forever once it stops logging, so without this the
    fake $161 would sit in the balance chart as a floor for the rest of the
    run even though the underlying bug is fixed.
    """
    bad = sorted(config.CONTAMINATED_TICKERS)
    if not bad:
        return "", []
    return f" AND {column} NOT IN ({','.join('?' * len(bad))})", bad


def _connect() -> sqlite3.Connection:
    # mode=ro: this process only ever reads (see module docstring), and
    # config.DB_PATH may point at a pulled Droplet backup via
    # KALSHI_DB_PATH. Opening read-write would let SQLite create a -wal/
    # -journal beside a snapshot we want left byte-for-byte intact, and
    # would silently create an empty DB if the path were ever wrong --
    # read-only turns that into a loud error instead.
    conn = sqlite3.connect(f"file:{config.DB_PATH.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _active_tickers_since(conn: sqlite3.Connection, since_ts: float) -> list[str]:
    """
    Tickers with any inventory or quote row since `since_ts`.

    Deliberately a separate, tiny query rather than a bound on the big
    latest-per-ticker queries. Those GROUP BY ticker, and SQLite satisfies
    that by walking the whole (ticker, ts) covering index -- it ignores a
    `ts >=` bound because ts is the index's SECOND column. Measured
    2026-10-06 on a 1.3GB DB: ~4s each to return 3 rows. INDEXED BY forces
    the ts-leading index instead, which is what makes the bound actually
    restrict anything: 0.000s for the same answer. The detail queries are
    then scoped by ticker, where equality seeks on (ticker, ts) are fast
    (0.042s).
    """
    found: set[str] = set()
    for table, hint in (("inventory_marks", "INDEXED BY idx_marks_ts"),
                        ("quote_snapshots", "INDEXED BY idx_quote_snapshots_ts")):
        try:
            rows = conn.execute(
                f"SELECT DISTINCT ticker FROM {table} {hint} WHERE ts >= ?", (since_ts,)
            ).fetchall()
        except sqlite3.OperationalError:
            # Index not present yet (pre-migration DB). Correctness first:
            # fall back to the unhinted query, which is slower but right.
            rows = conn.execute(
                f"SELECT DISTINCT ticker FROM {table} WHERE ts >= ?", (since_ts,)
            ).fetchall()
        found.update(r["ticker"] for r in rows)
    return sorted(found)


def _latest_per_ticker(
    conn: sqlite3.Connection,
    table: str,
    columns: str,
    tickers: list[str] | None = None,
    since_ts: float | None = None,
) -> dict[str, sqlite3.Row]:
    """
    Newest row per ticker.

    Uses SQLite's documented bare-columns-with-max() behaviour: in an
    aggregate query containing exactly one max(), the non-aggregated
    columns are taken from the row that produced that maximum. That lets
    the existing (ticker, ts) indexes do the work.

    The previous formulation was
    `ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY ts DESC)` filtered to
    rn = 1, which has no WHERE clause and so materialised a row number for
    EVERY row of the table on every dashboard tick just to keep ~29 of
    them. On 2026-10-03, with ~780k rows per table, that was ~33s of a
    ~40s tick against a once-per-second design, saturating the Droplet's
    single vCPU while main.py was trying to use it.

    Measured on a static copy of the live DB, old -> new:
        snapshot_scores              7.84s -> 0.72s
        inventory_marks              6.97s -> 0.69s
        incentive_program_snapshots  0.03s -> 0.00s
        quote_snapshots              6.65s -> 1.00s
        orderbook_snapshots         11.32s -> 8.50s
    Results were verified byte-identical for all five call sites with
    their real column lists. orderbook_snapshots gains least because its
    yes_levels/no_levels blobs dominate, not the row scan.

    _max_ts is an artefact of the aggregate; it is never read. Callers
    select named columns, so it does not reach the JSON payload -- do not
    dict() these rows wholesale without dropping it.
    """
    where = "" if since_ts is None else " WHERE ts >= ?"
    bound = [] if since_ts is None else [since_ts]
    if tickers is None:
        rows = conn.execute(
            f"""
            SELECT {columns}, max(ts) AS _max_ts
            FROM {table}{where}
            GROUP BY ticker
            """,
            bound,
        ).fetchall()
    else:
        # Restricting by ticker turns a walk of the whole (ticker, ts) index
        # into one lookup per named ticker. Worth it where the caller already
        # knows it will use only a few of them -- see the orderbook_snapshots
        # call site, which needed 3 of 29.
        wanted = list(tickers)
        if not wanted:
            return {}
        placeholders = ",".join("?" * len(wanted))
        rows = conn.execute(
            f"""
            SELECT {columns}, max(ts) AS _max_ts
            FROM {table}
            WHERE ticker IN ({placeholders})
            {'AND ts >= ?' if since_ts is not None else ''}
            GROUP BY ticker
            """,
            (*wanted, *bound),
        ).fetchall()
    return {row["ticker"]: row for row in rows}


def _recent_fills(conn: sqlite3.Connection, limit: int = RECENT_FILLS_LIMIT) -> list[dict]:
    # Contaminated markets are excluded here too: their fills came from
    # phantom liquidity, so showing them in "Recent Simulated Fills" would
    # present the artefact as activity even though it is out of the P&L.
    excl_sql, excl_params = _exclusion_clause()
    rows = conn.execute(
        f"SELECT ts, ticker, side, price, size FROM simulated_fills "
        f"WHERE 1=1{excl_sql} ORDER BY ts DESC LIMIT ?",
        (*excl_params, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _qualifying_counts(conn: sqlite3.Connection, tickers: list[str] | None = None) -> dict[str, dict]:
    """
    Running count (over everything collected so far) of "clean" snapshots
    where both sides reached Target Size, vs. total -- reuses the same
    qualifies_yes/qualifies_no values main.py already computed and logged,
    no rescoring.
    """
    # Scoped to the tickers actually being rendered. Unscoped this grouped
    # the ENTIRE snapshot_scores table -- every ticker ever watched, with no
    # time bound -- to produce numbers for the 3 on screen: 4.59s of a 34s
    # build on 2026-10-05, growing with the run. Counts stay lifetime
    # ("over everything collected so far"), which is the intended meaning;
    # only the rows scanned change.
    if tickers is None:
        rows = conn.execute(
            """
            SELECT ticker,
                   SUM(CASE WHEN qualifies_yes = 1 AND qualifies_no = 1 THEN 1 ELSE 0 END) AS qualifying,
                   COUNT(*) AS total
            FROM snapshot_scores
            GROUP BY ticker
            """
        ).fetchall()
    else:
        wanted = list(tickers)
        if not wanted:
            return {}
        placeholders = ",".join("?" * len(wanted))
        rows = conn.execute(
            f"""
            SELECT ticker,
                   SUM(CASE WHEN qualifies_yes = 1 AND qualifies_no = 1 THEN 1 ELSE 0 END) AS qualifying,
                   COUNT(*) AS total
            FROM snapshot_scores
            WHERE ticker IN ({placeholders})
            GROUP BY ticker
            """,
            wanted,
        ).fetchall()
    return {r["ticker"]: {"qualifying": r["qualifying"], "total": r["total"]} for r in rows}


def _recent_event_tickers(conn: sqlite3.Connection, since_ts: float) -> set[str]:
    """Tickers with a new simulated_fills or simulated_stop_losses row since since_ts."""
    tickers: set[str] = set()
    for table in ("simulated_fills", "simulated_stop_losses"):
        rows = conn.execute(f"SELECT DISTINCT ticker FROM {table} WHERE ts > ?", (since_ts,)).fetchall()
        tickers.update(r["ticker"] for r in rows)
    return tickers


def _performance_history(conn: sqlite3.Connection) -> dict[str, list]:
    """
    Three proxy series, downsampled to at most one point per
    config.PERFORMANCE_HISTORY_BUCKET_SECONDS within the last
    config.PERFORMANCE_HISTORY_LOOKBACK_HOURS -- bounded this way
    (rather than the previous flat "last N rows" cap) so the chart stays
    meaningful across a multi-day/multi-week run instead of only ever
    showing the last few minutes of a fast-ticking, multi-market log:
      - reward_score: running cumulative sum of combined_score across all
        watched markets. This is a unitless proxy for reward capture, NOT
        dollars -- Kalshi's actual USD payout formula pools every
        participant's snapshot scores over the full period, which this
        codebase doesn't (and can't, from public data alone) reconstruct.
      - pnl_dollars: running total simulated P&L (unrealized + realized)
        across every market ever watched, in dollars, using each
        market's most recently observed mark at every point in time --
        a market's contribution is FROZEN at its last known value once
        it stops logging (e.g. switched away by market_switcher.py),
        not dropped, so a switch never causes a phantom balance jump.
      - balance_dollars: config.STARTING_CAPITAL_USD + pnl_dollars --
        the pretend account balance over time (see capital_allocator.py).
    """
    bucket = float(config.PERFORMANCE_HISTORY_BUCKET_SECONDS)
    cutoff_ts = time.time() - config.PERFORMANCE_HISTORY_LOOKBACK_HOURS * 3600.0
    excl_sql, excl_params = _exclusion_clause()

    # All three series are CUMULATIVE over immutable history: once a bucket
    # is complete its contribution can never change. So the SQL only covers
    # the newest (possibly partial) bucket onward, and the Python walk that
    # turns per-bucket values into running totals is re-done each tick --
    # that walk is over ~1.6k buckets and costs nothing.
    #
    # Before this, each tick ran two window functions across the whole
    # lookback window. Measured 2026-10-05 on a 1.36GB DB: 11.49s and
    # 11.79s respectively, which was most of a 34s dashboard build. The
    # window functions are also gone -- a plain GROUP BY with MAX(ts) gets
    # the same answers and was ~35% faster even uncached.
    # The P&L epoch floors the window: marks before it belong to a run whose
    # losses were dominated by defects that no longer exist (see
    # config.PNL_EPOCH_TS and AUTOPSY.md). reward_score is NOT floored --
    # reward capture was measured honestly throughout and the longer series
    # is still informative.
    pnl_cutoff = max(cutoff_ts, config.PNL_EPOCH_TS)
    oldest_bucket = int(cutoff_ts // bucket)
    oldest_pnl_bucket = int(pnl_cutoff // bucket)
    for cache in (_hist_score_bucket, _hist_score_ts,
                  _reward_bucket_accrual, _reward_bucket_end_ts):
        for b in [b for b in cache if b < oldest_bucket]:
            del cache[b]
    for cache in (_hist_pnl_bucket, _hist_pnl_ts):
        for b in [b for b in cache if b < oldest_pnl_bucket]:
            del cache[b]

    # ---- reward score (unitless, cumulative sum of combined_score) ------
    resume = max(_hist_score_bucket, default=oldest_bucket)
    for r in conn.execute(
        f"""
        SELECT CAST(ts / ? AS INTEGER) AS b, SUM(combined_score) AS s, MAX(ts) AS ts
        FROM snapshot_scores WHERE ts >= ?{excl_sql}
        GROUP BY b ORDER BY b ASC
        """,
        (bucket, float(resume * bucket), *excl_params),
    ).fetchall():
        _hist_score_bucket[r["b"]] = r["s"] or 0.0
        _hist_score_ts[r["b"]] = r["ts"]

    reward_series = []
    running = 0.0
    for b in sorted(_hist_score_bucket):
        running += _hist_score_bucket[b]
        reward_series.append({"ts": _hist_score_ts[b], "value": running})

    # ---- simulated P&L and balance --------------------------------------
    # One COMBINED point per bucket, not one per ticker-row, so this series
    # has the same point density as reward_score above -- otherwise the two
    # lines are different lengths and, on a shared x-axis, one visibly stops
    # short of the other (CONFIRMED live 2026-09-02).
    #
    # A ticker's contribution is carried FORWARD from its last known mark
    # once it stops logging rather than dropped, so switching away from a
    # market cannot cause a phantom balance jump. That carry-forward is why
    # the Python walk below has to start from the oldest bucket every tick,
    # even though the SQL does not.
    resume = max(_hist_pnl_bucket, default=oldest_pnl_bucket)
    for r in conn.execute(
        f"""
        SELECT CAST(ts / ? AS INTEGER) AS b, ticker,
               unrealized_pnl_cents, realized_pnl_cents, MAX(ts) AS ts
        FROM inventory_marks WHERE ts >= ?{excl_sql}
        GROUP BY b, ticker ORDER BY b ASC
        """,
        (bucket, float(max(resume * bucket, pnl_cutoff)), *excl_params),
    ).fetchall():
        marks = _hist_pnl_bucket.setdefault(r["b"], {})
        marks[r["ticker"]] = (r["unrealized_pnl_cents"] or 0.0) + (r["realized_pnl_cents"] or 0.0)
        _hist_pnl_ts[r["b"]] = max(_hist_pnl_ts.get(r["b"], 0.0), r["ts"])

    pnl_series = []
    balance_series = []
    last_by_ticker: dict[str, float] = {}
    for b in sorted(_hist_pnl_bucket):
        last_by_ticker.update(_hist_pnl_bucket[b])
        pnl_dollars = sum(last_by_ticker.values()) / 100.0
        ts = _hist_pnl_ts[b]
        pnl_series.append({"ts": ts, "value": pnl_dollars})
        balance_series.append({"ts": ts, "value": config.STARTING_CAPITAL_USD + pnl_dollars})

    # ---- estimated reward income in dollars ------------------------------
    # Kalshi pools 2.0 of snapshot score per snapshot across ALL
    # participants, so our fraction of a program's pool is
    # combined_score / 2.0, times the program's dollars-per-second, times
    # the time actually spent quoting.
    #
    # UPPER BOUND, not a forecast: my_share models our order added on top of
    # the existing book with nobody reacting. See reward_simulator.py.
    rate_by_ticker: dict[str, float] = {}
    for prog in conn.execute(
        """
        SELECT ticker, period_reward, start_date, end_date FROM (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY ts DESC) AS rn
            FROM incentive_program_snapshots
        ) WHERE rn = 1
        """
    ).fetchall():
        rate = _program_reward_per_second(prog)
        if rate is not None:
            rate_by_ticker[prog["ticker"]] = rate

    resume = max(_reward_bucket_accrual, default=oldest_bucket)
    fresh: dict[int, float] = {}
    for r in conn.execute(
        f"""
        SELECT CAST(ts / ? AS INTEGER) AS b, ticker,
               AVG(combined_score) AS mean_score, MAX(ts) AS ts
        FROM snapshot_scores WHERE ts >= ?{excl_sql}
        GROUP BY b, ticker ORDER BY b ASC
        """,
        (bucket, float(resume * bucket), *excl_params),
    ).fetchall():
        rate = rate_by_ticker.get(r["ticker"])
        fresh[r["b"]] = fresh.get(r["b"], 0.0) + (
            ((r["mean_score"] or 0.0) / 2.0) * rate * bucket if rate else 0.0
        )
        _reward_bucket_end_ts[r["b"]] = max(_reward_bucket_end_ts.get(r["b"], 0.0), r["ts"])
    _reward_bucket_accrual.update(fresh)

    reward_dollars_series = []
    running = 0.0
    for b in sorted(_reward_bucket_accrual):
        running += _reward_bucket_accrual[b]
        ts = _reward_bucket_end_ts.get(b)
        if ts is not None:
            reward_dollars_series.append({"ts": ts, "value": running})

    return {
        "reward_score": reward_series,
        "pnl_dollars": pnl_series,
        "balance_dollars": balance_series,
        "reward_dollars": reward_dollars_series,
    }


def _market_metadata(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """market_metadata is one row per ticker (upserted by main.py when a
    ticker starts being watched), not a time series -- no latest-per-
    ticker windowing needed, just read it directly."""
    rows = conn.execute("SELECT ticker, title, subtitle FROM market_metadata").fetchall()
    return {r["ticker"]: r for r in rows}


def _risk_reason(
    risk_level: str | None,
    volatility_level: str | None,
    inventory_level: str | None,
    news_signal: bool | None,
    event_signal: bool | None,
) -> str | None:
    """
    Human-readable explanation of WHY risk_level is what it is, from its
    individual inputs (see risk_engine.RiskEngine.update -- risk_level is
    the max severity across volatility, inventory, news, and event
    proximity). A factor is listed only if it's independently AT the
    composite's own severity -- e.g. if risk_level is HIGH because of
    volatility alone, an ELEVATED inventory_level is correctly NOT listed
    as a cause, since it isn't what's driving the composite. news/event
    are never listed at HIGH -- risk_engine.py only ever lets them nudge
    toward ELEVATED, never straight to HIGH on their own.
    """
    if risk_level is None:
        return None
    if risk_level == "LOW":
        return "Normal conditions"

    contributors = []
    if volatility_level == risk_level:
        contributors.append("price volatility")
    if inventory_level == risk_level:
        contributors.append("position size")
    if risk_level == "ELEVATED":
        if news_signal:
            contributors.append("recent news")
        if event_signal:
            contributors.append("nearing close/resolution")

    if not contributors:
        return f"{risk_level} (sub-signal data unavailable)"
    return f"{risk_level.capitalize()} due to: {', '.join(contributors)}"


# Comfortably larger than the ~1s scoring cadence across multiple
# tickers, small enough to promptly drop a ticker market_switcher.py
# just switched away from. CONFIRMED live 2026-09-02: a just-vacated
# ticker's latest row was already 345s stale by the time it was queried
# (vs. ~0.2s for genuinely active ones) -- without this cutoff, "latest
# per ticker" means "latest EVER", so a switched-away ticker kept
# showing up (with its last-known allocation) alongside the real
# currently-watched ones indefinitely, e.g. a stale $562 entry
# duplicating the live ticker it had been switched away FROM.
RECENCY_CUTOFF_SEC = 20.0

# Seconds between dashboard payload builds.
#
# History, because the right value is entirely a function of the build cost:
# it was an aspirational 1.0 when a build cost ~25s, which kept the loop
# ~96% busy and starved main.py of the single vCPU while nobody ever saw a
# 1s refresh. Raised to 15.0 on 2026-10-03 when the build was ~9s. The build
# is now ~0.31s steady-state (2026-10-06, after memoising the history series
# and scoping every per-ticker query), so 3.0 gives a responsive dashboard
# at roughly a 10% duty cycle. Keep this comfortably above the real build
# cost, and revisit it in either direction when that cost changes.
WS_TICK_SECONDS = 3.0


def _is_recent(row: sqlite3.Row | None, now: float) -> bool:
    return row is not None and (now - row["ts"]) <= RECENCY_CUTOFF_SEC


def _capital_allocation(conn: sqlite3.Connection, inventory: dict[str, sqlite3.Row], now: float) -> list[dict]:
    """
    "Where is the money currently allocated" breakdown, derived from each
    CURRENTLY-active ticker's latest inventory_marks.max_inventory (see
    logger.py's SCHEMA) -- no separate table needed. Filtered to recent
    rows only (see RECENCY_CUTOFF_SEC) so a ticker market_switcher.py
    switched away from drops out promptly instead of lingering with a
    stale allocation figure.
    """
    allocations = {
        ticker: row["max_inventory"]
        for ticker, row in inventory.items()
        if row["max_inventory"] is not None and _is_recent(row, now)
    }
    total = sum(allocations.values())
    if total <= 0:
        return []
    return [
        {"ticker": t, "dollars": dollars, "pct": 100.0 * dollars / total}
        for t, dollars in sorted(allocations.items(), key=lambda kv: -kv[1])
    ]


def build_dashboard_payload(since_ts: float | None = None) -> dict:
    """
    since_ts: if given, markets with a simulated_fills/simulated_stop_losses
    row newer than this get "flash": true, for a one-shot visual highlight
    on the dashboard. Each WebSocket connection tracks its own since_ts
    (the ts of its previous tick), so every connected client sees each
    event flash exactly once, independent of other clients.
    """
    # Captured BEFORE the queries, not after.
    #
    # This is the reference time for the RECENCY_CUTOFF_SEC staleness test
    # below. It used to be read after every query had run, and a build takes
    # 9-25s on this box against a 20s cutoff -- so on any slow build, rows
    # that were fresh when read were judged stale, active_tickers came out
    # empty, and the markets table silently fell back to
    # config.WATCHED_MARKET_TICKERS: three unrelated markets with no data,
    # rendered as score 0.00000 / pool $0 / risk None. CONFIRMED 2026-10-05
    # on a cold build right after a restart, which is exactly when someone
    # would open the dashboard.
    #
    # Measuring freshness from the START of the build also means a slow
    # build can no longer mask a genuinely stalled collector: if the newest
    # row was already >20s old when we began, that is real staleness.
    now = time.time()
    conn = _connect()
    try:
        # Find who is live with one cheap probe, then fetch detail ONLY for
        # those tickers. The floor is generous relative to
        # RECENCY_CUTOFF_SEC so a slow build cannot narrow the candidate set
        # before the real staleness test below.
        freshness_floor = now - (RECENCY_CUTOFF_SEC * 10.0)
        live = _active_tickers_since(conn, freshness_floor)
        inventory = _latest_per_ticker(
            conn, "inventory_marks",
            "ticker, ts, net_yes_contracts, avg_entry_price, mid_price, "
            "unrealized_pnl_cents, realized_pnl_cents, max_inventory",
            live,
        )
        quotes = _latest_per_ticker(
            conn, "quote_snapshots",
            "ticker, ts, risk_level, quote_yes_price, quote_yes_size, quote_no_price, quote_no_size, "
            "volatility_level, inventory_level, news_signal, event_signal, crowding_ratio",
            live,
        )
        flash_tickers = _recent_event_tickers(conn, since_ts) if since_ts is not None else set()
        metadata = _market_metadata(conn)

        # Only tickers actively ticking right now (see RECENCY_CUTOFF_SEC) --
        # otherwise a ticker market_switcher.py switched away from (or an
        # old manual-config ticker from a previous session) keeps showing
        # up in the table indefinitely, since "latest per ticker" alone
        # means "latest EVER", not "currently watched". Falls back to the
        # static config list only if NOTHING is currently active (e.g.
        # main.py isn't running yet), so the table still shows a helpful
        # pre-data hint instead of a totally empty state.
        active_tickers = {t for t, r in inventory.items() if _is_recent(r, now)} | \
            {t for t, r in quotes.items() if _is_recent(r, now)}
        tickers = sorted(active_tickers) if active_tickers else sorted(set(config.WATCHED_MARKET_TICKERS))
        # Keep contaminated markets out of the table too, not just the chart,
        # so a stale row cannot present phantom-derived scores as current.
        tickers = [t for t in tickers if t not in config.CONTAMINATED_TICKERS]

        # Deliberately queried AFTER `tickers` is known, and scoped to it.
        # orderbook_snapshots carries the yes_levels/no_levels blobs, so it
        # was the most expensive read in the tick (8.6s of ~25s on
        # 2026-10-03) while returning 29 tickers for a table that renders 3.
        # `book` is only ever read as books.get(ticker) inside the loop
        # below, so narrowing this to `tickers` cannot change the payload.
        books = _latest_per_ticker(
            conn, "orderbook_snapshots", "ticker, ts, yes_levels, no_levels", tickers
        )
        # Scoped to the rendered tickers, same reasoning as books above.
        scores = _latest_per_ticker(
            conn, "snapshot_scores",
            "ticker, ts, qualifies_yes, qualifies_no, my_share_yes, my_share_no, "
            "combined_score, reference_price_yes, reference_price_no",
            tickers,
        )
        programs = _latest_per_ticker(
            conn, "incentive_program_snapshots",
            "ticker, ts, period_reward, target_size, discount_factor_bps, days_remaining, "
            "start_date, end_date",
            tickers,
        )
        qualifying_counts = _qualifying_counts(conn, tickers)

        markets = []
        for ticker in tickers:
            program = programs.get(ticker)
            inv = inventory.get(ticker)
            score = scores.get(ticker)
            book = books.get(ticker)
            quote_row = quotes.get(ticker)
            qual = qualifying_counts.get(ticker, {"qualifying": 0, "total": 0})

            daily_pool = None
            discount_factor = None
            target_size = None
            total_reward = None
            program_start = None
            program_end = None
            if program is not None:
                target_size = program["target_size"]
                discount_factor = (program["discount_factor_bps"] or 0) / 10000.0
                total_reward = program["period_reward"]
                program_start = program["start_date"]
                program_end = program["end_date"]
                rate = _program_reward_per_second(program)
                if rate is not None:
                    # The pool spread over the program's OWN duration. See
                    # _program_reward_per_second for why days_remaining is
                    # the wrong denominator.
                    daily_pool = rate * 86400.0

            unrealized_pnl = None
            realized_pnl = None
            net_pnl = None
            net_yes_contracts = None
            if inv is not None:
                unrealized_pnl = (inv["unrealized_pnl_cents"] or 0.0) / 100.0
                realized_pnl = (inv["realized_pnl_cents"] or 0.0) / 100.0
                net_pnl = unrealized_pnl + realized_pnl
                net_yes_contracts = inv["net_yes_contracts"]

            risk_level = quote_row["risk_level"] if quote_row is not None else None
            quote = None
            crowding_ratio = None
            risk_reason = None
            if quote_row is not None:
                crowding_ratio = quote_row["crowding_ratio"]
                risk_reason = _risk_reason(
                    risk_level,
                    quote_row["volatility_level"],
                    quote_row["inventory_level"],
                    None if quote_row["news_signal"] is None else bool(quote_row["news_signal"]),
                    None if quote_row["event_signal"] is None else bool(quote_row["event_signal"]),
                )
                if quote_row["quote_yes_price"] is not None:
                    quote = {
                        "yes_price": quote_row["quote_yes_price"],
                        "yes_size": quote_row["quote_yes_size"],
                        "no_price": quote_row["quote_no_price"],
                        "no_size": quote_row["quote_no_size"],
                    }

            meta = metadata.get(ticker)

            markets.append({
                "ticker": ticker,
                "title": meta["title"] if meta is not None else None,
                "subtitle": meta["subtitle"] if meta is not None else None,
                "target_size": target_size,
                "discount_factor": discount_factor,
                "daily_pool": daily_pool,
                "total_reward": total_reward,
                "program_start": program_start,
                "program_end": program_end,
                "net_pnl": net_pnl,
                "unrealized_pnl": unrealized_pnl,
                "realized_pnl": realized_pnl,
                "net_yes_contracts": net_yes_contracts,
                "qualifies_yes": bool(score["qualifies_yes"]) if score is not None else None,
                "qualifies_no": bool(score["qualifies_no"]) if score is not None else None,
                "combined_score": score["combined_score"] if score is not None else None,
                "qualifying_snapshots": qual["qualifying"],
                "total_snapshots": qual["total"],
                "risk_level": risk_level,
                "risk_reason": risk_reason,
                "crowding_ratio": crowding_ratio,  # our "competition level" proxy -- Kalshi doesn't publish one directly
                "quote": quote,  # None if pulled (HIGH risk) or no data logged yet
                "flash": ticker in flash_tickers,
                "orderbook": {
                    "yes_levels": json.loads(book["yes_levels"]),
                    "no_levels": json.loads(book["no_levels"]),
                } if book is not None else None,
            })

        return {
            "type": "update",
            "server_ts": time.time(),
            "environment": config.ENVIRONMENT,
            "read_only": config.is_read_only_mode(),
            "markets": markets,
            "recent_fills": _recent_fills(conn),
            "performance_history": _performance_history(conn),
            "starting_capital_usd": config.STARTING_CAPITAL_USD,
            "capital_allocation": _capital_allocation(conn, inventory, now),
        }
    finally:
        conn.close()


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "dashboard.html", {})


@app.get("/api/status")
async def status() -> dict:
    return {"environment": config.ENVIRONMENT, "read_only": config.is_read_only_mode()}


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    logger.info("Dashboard WebSocket client connected")
    # Per-connection watermark for "new since last tick" fill/stop-loss
    # detection -- each client independently sees every event flash
    # exactly once, regardless of how many other clients are connected.
    since_ts = time.time()
    try:
        while True:
            next_since_ts = time.time()
            payload = await asyncio.to_thread(build_dashboard_payload, since_ts)
            since_ts = next_since_ts
            await websocket.send_text(json.dumps(payload))
            await asyncio.sleep(WS_TICK_SECONDS)
    except WebSocketDisconnect:
        logger.info("Dashboard WebSocket client disconnected")


if __name__ == "__main__":
    # Structural protection against running two instances at once (see
    # singleton_lock.py). The OS port bind already prevents true
    # duplicates here, but this gives a clear error message instead of
    # a raw "address already in use" and keeps the same protection
    # pattern as main.py's run() -- called at process-start time, not
    # module-import time, so this module can be imported (e.g. for unit
    # tests, or by another process checking config) without contending
    # with an already-running server for the lock.
    acquire_singleton_lock("server", config.BASE_DIR)
    logger.info(
        "Starting dashboard server (READ-ONLY -- no orders can be placed) at http://127.0.0.1:8000"
    )
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
