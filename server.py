"""
Read-only FastAPI dashboard for the Kalshi Liquidity Lab paper-trading
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
from logger import SimulationLogger
from singleton_lock import acquire_singleton_lock

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("server")

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app = FastAPI(title="Kalshi Liquidity Lab Dashboard")

# Ensures the schema exists even if main.py has never been run yet, so the
# dashboard can start up and show an empty (rather than broken) state.
SimulationLogger()

RECENT_FILLS_LIMIT = 10


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
    total = program["period_reward"]
    start, end = program["start_date"], program["end_date"]
    if not total or not start or not end:
        return None
    try:
        t0 = datetime.datetime.fromisoformat(str(start))
        t1 = datetime.datetime.fromisoformat(str(end))
    except (TypeError, ValueError):
        return None
    seconds = (t1 - t0).total_seconds()
    if seconds <= 0:
        return None
    return total / seconds


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


def _latest_per_ticker(
    conn: sqlite3.Connection,
    table: str,
    columns: str,
    tickers: list[str] | None = None,
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
    if tickers is None:
        rows = conn.execute(
            f"""
            SELECT {columns}, max(ts) AS _max_ts
            FROM {table}
            GROUP BY ticker
            """
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
            GROUP BY ticker
            """,
            wanted,
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


def _qualifying_counts(conn: sqlite3.Connection) -> dict[str, dict]:
    """
    Running count (over everything collected so far) of "clean" snapshots
    where both sides reached Target Size, vs. total -- reuses the same
    qualifies_yes/qualifies_no values main.py already computed and logged,
    no rescoring.
    """
    rows = conn.execute(
        """
        SELECT ticker,
               SUM(CASE WHEN qualifies_yes = 1 AND qualifies_no = 1 THEN 1 ELSE 0 END) AS qualifying,
               COUNT(*) AS total
        FROM snapshot_scores
        GROUP BY ticker
        """
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
    cutoff_ts = time.time() - config.PERFORMANCE_HISTORY_LOOKBACK_HOURS * 3600
    bucket = config.PERFORMANCE_HISTORY_BUCKET_SECONDS

    excl_sql, excl_params = _exclusion_clause()

    score_rows = conn.execute(
        f"""
        SELECT ts, cumulative FROM (
            SELECT ts,
                   SUM(combined_score) OVER (ORDER BY ts) AS cumulative,
                   ROW_NUMBER() OVER (
                       PARTITION BY CAST(ts / ? AS INTEGER) ORDER BY ts DESC
                   ) AS rn
            FROM snapshot_scores
            WHERE ts > ?{excl_sql}
        )
        WHERE rn = 1
        ORDER BY ts ASC
        """,
        (bucket, cutoff_ts, *excl_params),
    ).fetchall()
    reward_series = [{"ts": r["ts"], "value": r["cumulative"] or 0.0} for r in score_rows]

    pnl_rows = conn.execute(
        f"""
        SELECT ts, ticker, unrealized_pnl_cents, realized_pnl_cents FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY ticker, CAST(ts / ? AS INTEGER) ORDER BY ts DESC
            ) AS rn
            FROM inventory_marks
            WHERE ts > ?{excl_sql}
        )
        WHERE rn = 1
        ORDER BY ts ASC
        """,
        (bucket, cutoff_ts, *excl_params),
    ).fetchall()
    # pnl_rows has one row per (ticker, bucket) -- up to num_tickers rows
    # landing in the SAME bucket. Emit exactly one COMBINED point per
    # bucket (not one point per ticker-row), so pnl_dollars/balance_dollars
    # have the same point density as reward_score above -- otherwise the
    # two series end up with very different lengths and, plotted against
    # a shared x-axis, one visibly stops well short of the other's actual
    # end (CONFIRMED live 2026-09-02: reward_score had 169 points vs
    # pnl_dollars' 363, so the reward line stopped roughly halfway across
    # a chart whose x-axis was sized to the longer series).
    last_pnl_by_ticker: dict[str, float] = {}
    pnl_series = []
    balance_series = []
    current_bucket = None
    bucket_end_ts = None
    for r in pnl_rows:
        row_bucket = int(r["ts"] // bucket)
        if current_bucket is not None and row_bucket != current_bucket:
            pnl_dollars = sum(last_pnl_by_ticker.values()) / 100.0
            pnl_series.append({"ts": bucket_end_ts, "value": pnl_dollars})
            balance_series.append({"ts": bucket_end_ts, "value": config.STARTING_CAPITAL_USD + pnl_dollars})
        current_bucket = row_bucket
        bucket_end_ts = r["ts"]
        total_cents = (r["unrealized_pnl_cents"] or 0.0) + (r["realized_pnl_cents"] or 0.0)
        last_pnl_by_ticker[r["ticker"]] = total_cents
    if current_bucket is not None:
        pnl_dollars = sum(last_pnl_by_ticker.values()) / 100.0
        pnl_series.append({"ts": bucket_end_ts, "value": pnl_dollars})
        balance_series.append({"ts": bucket_end_ts, "value": config.STARTING_CAPITAL_USD + pnl_dollars})

    # ------------------------------------------------------------------
    # reward_dollars: an ESTIMATE of incentive income, so the two halves of
    # the strategy are finally in the same units.
    #
    # Until now the dashboard measured costs in dollars (pnl_dollars) and
    # revenue in a unitless score, which made the central question of this
    # project -- does reward income exceed adverse selection? -- impossible
    # to read off the chart.
    #
    # Kalshi pools 2.0 of snapshot score per snapshot across ALL
    # participants, so our fraction of a program's pool is
    # combined_score / 2.0. Multiplying by the program's dollars-per-second
    # and the time actually spent quoting gives dollars.
    #
    # THIS IS AN UPPER BOUND, not a forecast. my_share models our order
    # added on top of the existing book with nobody reacting; resting real
    # size in a $60k/day program would draw a competitive response that
    # grows the denominator and shrinks our share. reward_simulator.py's
    # module docstring says the same thing. Treat it as "what we would earn
    # if the rest of the book ignored us".
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

    bucket_rows = conn.execute(
        f"""
        SELECT CAST(ts / ? AS INTEGER) AS b, ticker,
               AVG(combined_score) AS mean_score, MAX(ts) AS ts
        FROM snapshot_scores
        WHERE ts > ?{excl_sql}
        GROUP BY b, ticker
        ORDER BY b ASC
        """,
        (bucket, cutoff_ts, *excl_params),
    ).fetchall()

    reward_dollars_series = []
    cumulative_reward = 0.0
    current_bucket = None
    bucket_end_ts = None
    bucket_accrual = 0.0
    for r in bucket_rows:
        if current_bucket is not None and r["b"] != current_bucket:
            cumulative_reward += bucket_accrual
            reward_dollars_series.append({"ts": bucket_end_ts, "value": cumulative_reward})
            bucket_accrual = 0.0
        current_bucket = r["b"]
        bucket_end_ts = r["ts"]
        rate = rate_by_ticker.get(r["ticker"])
        if rate:
            # share of pool  x  $/s  x  seconds in this bucket
            bucket_accrual += ((r["mean_score"] or 0.0) / 2.0) * rate * bucket
    if current_bucket is not None:
        cumulative_reward += bucket_accrual
        reward_dollars_series.append({"ts": bucket_end_ts, "value": cumulative_reward})

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

# Seconds between dashboard payload builds. Was 1.0, which was aspirational
# rather than real: a build cost ~25s on 2026-10-03, so an attached browser
# kept the loop ~96% busy and starved main.py of the Droplet's single vCPU
# while contributing nothing -- nobody saw a 1s refresh. Making the interval
# explicit hands the core back between builds. Keep this comfortably above
# the real build cost; if the build is ever optimised well below this,
# lower it rather than leaving the dashboard needlessly stale.
WS_TICK_SECONDS = 15.0


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
    conn = _connect()
    try:
        scores = _latest_per_ticker(
            conn, "snapshot_scores",
            "ticker, ts, qualifies_yes, qualifies_no, my_share_yes, my_share_no, "
            "combined_score, reference_price_yes, reference_price_no",
        )
        inventory = _latest_per_ticker(
            conn, "inventory_marks",
            "ticker, ts, net_yes_contracts, avg_entry_price, mid_price, "
            "unrealized_pnl_cents, realized_pnl_cents, max_inventory",
        )
        programs = _latest_per_ticker(
            conn, "incentive_program_snapshots",
            "ticker, ts, period_reward, target_size, discount_factor_bps, days_remaining, "
            "start_date, end_date",
        )
        quotes = _latest_per_ticker(
            conn, "quote_snapshots",
            "ticker, ts, risk_level, quote_yes_price, quote_yes_size, quote_no_price, quote_no_size, "
            "volatility_level, inventory_level, news_signal, event_signal, crowding_ratio",
        )
        qualifying_counts = _qualifying_counts(conn)
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
        now = time.time()
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
