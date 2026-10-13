"""
Persists Phase 0 simulation output to SQLite so we can run an analysis
pass after a data-collection window (days/weeks) without keeping
everything in memory.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot_scores (
    ts REAL NOT NULL,
    ticker TEXT NOT NULL,
    qualifies_yes INTEGER,
    qualifies_no INTEGER,
    my_share_yes REAL,
    my_share_no REAL,
    combined_score REAL,
    reference_price_yes INTEGER,
    reference_price_no INTEGER
);

CREATE TABLE IF NOT EXISTS simulated_fills (
    ts REAL NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    price INTEGER NOT NULL,
    size INTEGER NOT NULL,
    exit_reason TEXT  -- NULL (entry/accumulating), "take_profit_exit", or
                      -- "exit" (reduced inventory below full margin, e.g.
                      -- during the pre-resolution time-decay window) --
                      -- see quoting_policy.classify_fill_exit_reason
);

CREATE TABLE IF NOT EXISTS inventory_marks (
    ts REAL NOT NULL,
    ticker TEXT NOT NULL,
    net_yes_contracts INTEGER,
    avg_entry_price REAL,
    mid_price REAL,
    unrealized_pnl_cents REAL,
    realized_pnl_cents REAL,
    max_inventory INTEGER  -- this market's current capital_allocator.py
                           -- allocation (see MarketRuntimeState.max_inventory);
                           -- added so the dashboard can show "where is the
                           -- money currently allocated" without a second
                           -- table, self-correcting when a market is
                           -- switched away since it simply stops getting
                           -- new rows (see server.py's latest-per-ticker pattern)
);

-- Raw public trade tape. Added 2026-10-13.
--
-- NOT previously persisted, and its absence blocks the one experiment that
-- decides whether this strategy can work: how fill probability trades off
-- against reward score as we rest further behind the Reference Price. The
-- score half of that curve is computable from orderbook_snapshots alone,
-- but the FILL half needs the tape -- simulated_fills records OUR quote
-- price, not the trade price, so there is no way to tell from it whether a
-- deeper quote would also have been touched.
--
-- Every field here is what adverse_selection_sim.process_trade_for_inventory
-- consumes, so a replay at any offset is exact rather than modelled.
CREATE TABLE IF NOT EXISTS trades (
    ts REAL NOT NULL,
    ticker TEXT NOT NULL,
    trade_yes_price INTEGER NOT NULL,
    trade_count REAL NOT NULL,
    taker_side TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_ticker_ts ON trades(ticker, ts);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);

CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    ts REAL NOT NULL,
    ticker TEXT NOT NULL,
    yes_levels TEXT NOT NULL,  -- JSON list of [price_cents, size]
    no_levels TEXT NOT NULL    -- JSON list of [price_cents, size]
);

CREATE TABLE IF NOT EXISTS incentive_program_snapshots (
    ts REAL NOT NULL,
    ticker TEXT NOT NULL,
    period_reward REAL,
    target_size REAL,
    discount_factor_bps INTEGER,
    days_remaining REAL,
    total_period_days REAL,  -- full program period length, e.g. 14.0 for a
                             -- 2-week program -- needed to fairly prorate
                             -- an estimated reward by how much of the
                             -- period we've actually observed (see
                             -- analysis.py; added after a 2026-09-01 estimate
                             -- projected a ~1hr sample across an entire
                             -- 14-day pool with no time-scaling at all)
    start_date TEXT,  -- ISO8601, the program's real start/end (not
    end_date TEXT     -- derivable from days_remaining/total_period_days
                      -- alone once observed at a single point in time)
);

CREATE TABLE IF NOT EXISTS simulated_stop_losses (
    ts REAL NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    price INTEGER NOT NULL,
    size INTEGER NOT NULL,
    net_yes_contracts_before INTEGER NOT NULL,
    net_yes_contracts_after INTEGER NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS news_events (
    ts REAL NOT NULL,
    ticker TEXT NOT NULL,
    headline TEXT NOT NULL,
    source_url TEXT,
    relevance_score REAL NOT NULL,
    theme_tags TEXT  -- JSON list of tags; always [] currently -- Google
                     -- News RSS has no GKG-style tag concept (see news_feed.py)
);

CREATE TABLE IF NOT EXISTS quote_snapshots (
    ts REAL NOT NULL,
    ticker TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    -- NULL for all four when the quote is pulled (HIGH risk) -- pure
    -- instrumentation for the dashboard, purely additive: main.py's
    -- risk_engine/quoting_policy decisions are unaffected by this table.
    quote_yes_price INTEGER,
    quote_yes_size REAL,
    quote_no_price INTEGER,
    quote_no_size REAL,
    -- risk_level's individual inputs (see risk_engine.RiskEngine.update),
    -- so the dashboard can explain WHY risk_level is what it is, not
    -- just show the final composite badge.
    volatility_level TEXT,
    inventory_level TEXT,
    news_signal INTEGER,
    event_signal INTEGER,
    -- Real resting depth vs target_size (see
    -- market_switcher.compute_crowding_ratio) -- Kalshi doesn't publish
    -- a "competition level" directly, this is our own proxy for it.
    crowding_ratio REAL,
    displacement_cents REAL
);

CREATE TABLE IF NOT EXISTS market_switches (
    ts REAL NOT NULL,
    old_ticker TEXT NOT NULL,
    new_ticker TEXT NOT NULL,
    reason TEXT NOT NULL,  -- "volatile" or "crowded"
    net_yes_contracts_flattened INTEGER  -- NULL if there was nothing to flatten
);

CREATE TABLE IF NOT EXISTS event_calendar_snapshots (
    ts REAL NOT NULL,
    ticker TEXT NOT NULL,
    close_time TEXT,           -- ISO8601, NULL if unavailable this poll
    hours_until_close REAL,    -- NULL if unavailable this poll
    flagged INTEGER NOT NULL   -- whether this nudged risk toward ELEVATED
);

CREATE TABLE IF NOT EXISTS market_metadata (
    ticker TEXT PRIMARY KEY,  -- one row per ticker (upserted), not a time
                              -- series -- title/subtitle are effectively
                              -- static once a market exists
    title TEXT,
    subtitle TEXT,
    ts REAL NOT NULL          -- when this row was last (re)fetched
);

CREATE INDEX IF NOT EXISTS idx_scores_ticker_ts ON snapshot_scores(ticker, ts);
CREATE INDEX IF NOT EXISTS idx_fills_ticker_ts ON simulated_fills(ticker, ts);
CREATE INDEX IF NOT EXISTS idx_marks_ticker_ts ON inventory_marks(ticker, ts);
CREATE INDEX IF NOT EXISTS idx_orderbook_ticker_ts ON orderbook_snapshots(ticker, ts);
CREATE INDEX IF NOT EXISTS idx_incentive_snap_ticker_ts ON incentive_program_snapshots(ticker, ts);
CREATE INDEX IF NOT EXISTS idx_stop_losses_ticker_ts ON simulated_stop_losses(ticker, ts);
CREATE INDEX IF NOT EXISTS idx_news_events_ticker_ts ON news_events(ticker, ts);
CREATE INDEX IF NOT EXISTS idx_quote_snapshots_ticker_ts ON quote_snapshots(ticker, ts);
CREATE INDEX IF NOT EXISTS idx_market_switches_ts ON market_switches(ts);

-- ts-leading indexes for server.py's _performance_history, which filters
-- `WHERE ts > ?` with no ticker. The (ticker, ts) indexes above cannot serve
-- that: SQLite will not use a composite index for a predicate on its second
-- column, so those queries were full table scans (CONFIRMED 2026-10-03 via
-- EXPLAIN QUERY PLAN: `SCAN snapshot_scores` before, `SEARCH ... USING INDEX
-- idx_scores_ts (ts>?)` after).
--
-- Measured honestly: adding these did NOT speed up the dashboard today,
-- because PERFORMANCE_HISTORY_LOOKBACK_HOURS is 336h while the run had only
-- 74.7h of data, so `ts > cut` matched every row and an index range scan
-- over all rows is no cheaper than a table scan. They start paying off once
-- a run exceeds the lookback window. Kept because the plan is correct and
-- they cost ~25MB on a ~1GB database.
CREATE INDEX IF NOT EXISTS idx_scores_ts ON snapshot_scores(ts);
CREATE INDEX IF NOT EXISTS idx_marks_ts ON inventory_marks(ts);
-- ts-leading index for server.py's _active_tickers_since probe. Without it
-- that query falls back to walking the whole (ticker, ts) covering index,
-- because SQLite will not use a composite index's SECOND column to restrict
-- a range. With it, "who is quoting right now" is a 0.000s index seek
-- instead of a ~4s full scan (measured 2026-10-06 on a 1.3GB database).
CREATE INDEX IF NOT EXISTS idx_quote_snapshots_ts ON quote_snapshots(ts);
CREATE INDEX IF NOT EXISTS idx_event_calendar_ticker_ts ON event_calendar_snapshots(ticker, ts);
"""


# How long a write waits for a competing lock before raising. See _conn.
DB_BUSY_TIMEOUT_SEC = 30.0


class SimulationLogger:
    def __init__(self, db_path=None):
        self.db_path = str(db_path or config.DB_PATH)
        self._init_schema()

    @contextmanager
    def _conn(self):
        # timeout: Python's default is 5s, which is not enough on this
        # database. In the default rollback-journal mode a READER blocks a
        # writer, and reads here are not short -- backup_db.py's online
        # .backup() and the off-box push's VACUUM INTO both scan a >1GB
        # file, and the dashboard's tick does several full-table reads. On
        # 2026-10-04 the 09:00 backup produced a real
        # "sqlite3.OperationalError: database is locked" that lost that
        # scoring tick, and an ad-hoc analytical query against the live DB
        # stalled the collector the same way.
        conn = sqlite3.connect(self.db_path, timeout=DB_BUSY_TIMEOUT_SEC)
        try:
            # WAL lets readers and the writer run concurrently instead of
            # excluding each other, which removes the whole class of
            # conflict above rather than just waiting longer for it. It is
            # a persistent property of the DB file, so this is a no-op
            # after the first connection. synchronous=NORMAL is the
            # standard WAL pairing: still crash-safe, without an fsync per
            # commit on a once-per-second writer.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """
        Lightweight in-place migrations for columns added after a table
        may already exist on disk -- CREATE TABLE IF NOT EXISTS alone
        won't add a new column to a pre-existing table.
        """
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(incentive_program_snapshots)")}
        if "total_period_days" not in existing_cols:
            conn.execute("ALTER TABLE incentive_program_snapshots ADD COLUMN total_period_days REAL")

        inventory_cols = {row[1] for row in conn.execute("PRAGMA table_info(inventory_marks)")}
        if "max_inventory" not in inventory_cols:
            conn.execute("ALTER TABLE inventory_marks ADD COLUMN max_inventory INTEGER")

        program_cols = {row[1] for row in conn.execute("PRAGMA table_info(incentive_program_snapshots)")}
        if "start_date" not in program_cols:
            conn.execute("ALTER TABLE incentive_program_snapshots ADD COLUMN start_date TEXT")
        if "end_date" not in program_cols:
            conn.execute("ALTER TABLE incentive_program_snapshots ADD COLUMN end_date TEXT")

        quote_cols = {row[1] for row in conn.execute("PRAGMA table_info(quote_snapshots)")}
        for col, coltype in (
            ("volatility_level", "TEXT"), ("inventory_level", "TEXT"),
            ("news_signal", "INTEGER"), ("event_signal", "INTEGER"), ("crowding_ratio", "REAL"),
            # Added 2026-10-06. The incident that day was hard to diagnose
            # because a single "volatility: LOW" badge hid WHICH term was
            # being measured; logging the raw displacement makes the next one
            # readable straight from the data.
            ("displacement_cents", "REAL"),
        ):
            if col not in quote_cols:
                conn.execute(f"ALTER TABLE quote_snapshots ADD COLUMN {col} {coltype}")

        # Realized-P&L decomposition, added 2026-10-13. See InventoryState.
        marks_cols = {row[1] for row in conn.execute("PRAGMA table_info(inventory_marks)")}
        for col, coltype in (
            ("realized_passive_cents", "REAL"),
            ("realized_taker_cents", "REAL"),
            ("realized_settlement_cents", "REAL"),
        ):
            if col not in marks_cols:
                conn.execute(f"ALTER TABLE inventory_marks ADD COLUMN {col} {coltype}")

        fills_cols = {row[1] for row in conn.execute("PRAGMA table_info(simulated_fills)")}
        if "exit_reason" not in fills_cols:
            conn.execute("ALTER TABLE simulated_fills ADD COLUMN exit_reason TEXT")

    def log_snapshot_score(
        self,
        ticker: str,
        qualifies_yes: bool,
        qualifies_no: bool,
        my_share_yes: float,
        my_share_no: float,
        reference_price_yes: int | None,
        reference_price_no: int | None,
        ts: float | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO snapshot_scores
                   (ts, ticker, qualifies_yes, qualifies_no, my_share_yes, my_share_no,
                    combined_score, reference_price_yes, reference_price_no)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ts or time.time(),
                    ticker,
                    int(qualifies_yes),
                    int(qualifies_no),
                    my_share_yes,
                    my_share_no,
                    my_share_yes + my_share_no,
                    reference_price_yes,
                    reference_price_no,
                ),
            )

    def log_simulated_fill(
        self,
        ticker: str,
        side: str,
        price: int,
        size: int,
        exit_reason: str | None = None,
        ts: float | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO simulated_fills (ts, ticker, side, price, size, exit_reason) VALUES (?, ?, ?, ?, ?, ?)",
                (ts or time.time(), ticker, side, price, size, exit_reason),
            )

    def log_inventory_mark(
        self,
        ticker: str,
        net_yes_contracts: int,
        avg_entry_price: float,
        mid_price: float,
        unrealized_pnl_cents: float,
        realized_pnl_cents: float,
        max_inventory: int | None = None,
        realized_passive_cents: float | None = None,
        realized_taker_cents: float | None = None,
        realized_settlement_cents: float | None = None,
        ts: float | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO inventory_marks
                   (ts, ticker, net_yes_contracts, avg_entry_price, mid_price,
                    unrealized_pnl_cents, realized_pnl_cents, max_inventory,
                    realized_passive_cents, realized_taker_cents, realized_settlement_cents)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ts or time.time(), ticker, net_yes_contracts, avg_entry_price, mid_price,
                 unrealized_pnl_cents, realized_pnl_cents, max_inventory,
                 realized_passive_cents, realized_taker_cents, realized_settlement_cents),
            )

    def log_orderbook_snapshot(
        self,
        ticker: str,
        yes_levels: list[tuple[int, int]],
        no_levels: list[tuple[int, int]],
        ts: float | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO orderbook_snapshots (ts, ticker, yes_levels, no_levels) VALUES (?, ?, ?, ?)",
                (ts or time.time(), ticker, json.dumps(yes_levels), json.dumps(no_levels)),
            )

    def log_incentive_program_snapshot(
        self,
        ticker: str,
        period_reward: float,
        target_size: float,
        discount_factor_bps: int,
        days_remaining: float,
        total_period_days: float | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        ts: float | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO incentive_program_snapshots
                   (ts, ticker, period_reward, target_size, discount_factor_bps,
                    days_remaining, total_period_days, start_date, end_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ts or time.time(), ticker, period_reward, target_size, discount_factor_bps,
                 days_remaining, total_period_days, start_date, end_date),
            )

    def log_simulated_stop_loss(
        self,
        ticker: str,
        side: str,
        price: int,
        size: int,
        net_yes_contracts_before: int,
        net_yes_contracts_after: int,
        reason: str,
        ts: float | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO simulated_stop_losses
                   (ts, ticker, side, price, size, net_yes_contracts_before,
                    net_yes_contracts_after, reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (ts or time.time(), ticker, side, price, size,
                 net_yes_contracts_before, net_yes_contracts_after, reason),
            )

    def log_news_event(
        self,
        ticker: str,
        headline: str,
        source_url: str,
        relevance_score: float,
        theme_tags: list[str],
        ts: float | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO news_events
                   (ts, ticker, headline, source_url, relevance_score, theme_tags)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (ts or time.time(), ticker, headline, source_url, relevance_score, json.dumps(theme_tags)),
            )

    def log_trade(
        self,
        ticker: str,
        trade_yes_price: int,
        trade_count: float,
        taker_side: str,
        ts: float | None = None,
    ) -> None:
        """
        One row per public trade print. Logged BEFORE any fill inference, so
        the tape stays independent of whatever quote we happened to have --
        that independence is the whole point (see the trades table comment).
        """
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO trades (ts, ticker, trade_yes_price, trade_count, taker_side) "
                "VALUES (?, ?, ?, ?, ?)",
                (ts or time.time(), ticker, trade_yes_price, trade_count, taker_side),
            )

    def log_quote_snapshot(
        self,
        ticker: str,
        risk_level: str,
        quote_yes_price: int | None,
        quote_yes_size: float | None,
        quote_no_price: int | None,
        quote_no_size: float | None,
        volatility_level: str | None = None,
        inventory_level: str | None = None,
        news_signal: bool | None = None,
        event_signal: bool | None = None,
        crowding_ratio: float | None = None,
        displacement_cents: float | None = None,
        ts: float | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO quote_snapshots
                   (ts, ticker, risk_level, quote_yes_price, quote_yes_size,
                    quote_no_price, quote_no_size, volatility_level, inventory_level,
                    news_signal, event_signal, crowding_ratio, displacement_cents)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ts or time.time(), ticker, risk_level,
                 quote_yes_price, quote_yes_size, quote_no_price, quote_no_size,
                 volatility_level, inventory_level,
                 None if news_signal is None else int(news_signal),
                 None if event_signal is None else int(event_signal),
                 crowding_ratio, displacement_cents),
            )

    def log_market_switch(
        self,
        old_ticker: str,
        new_ticker: str,
        reason: str,
        net_yes_contracts_flattened: int | None = None,
        ts: float | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO market_switches
                   (ts, old_ticker, new_ticker, reason, net_yes_contracts_flattened)
                   VALUES (?, ?, ?, ?, ?)""",
                (ts or time.time(), old_ticker, new_ticker, reason, net_yes_contracts_flattened),
            )

    def upsert_market_metadata(
        self,
        ticker: str,
        title: str | None,
        subtitle: str | None,
        ts: float | None = None,
    ) -> None:
        """One row per ticker, not a time series -- see market_metadata's
        schema comment. Overwrites any previous row for this ticker."""
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO market_metadata (ticker, title, subtitle, ts)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(ticker) DO UPDATE SET title=excluded.title,
                       subtitle=excluded.subtitle, ts=excluded.ts""",
                (ticker, title, subtitle, ts or time.time()),
            )

    def log_event_calendar_snapshot(
        self,
        ticker: str,
        close_time: str | None,
        hours_until_close: float | None,
        flagged: bool,
        ts: float | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO event_calendar_snapshots
                   (ts, ticker, close_time, hours_until_close, flagged)
                   VALUES (?, ?, ?, ?, ?)""",
                (ts or time.time(), ticker, close_time, hours_until_close, int(flagged)),
            )
