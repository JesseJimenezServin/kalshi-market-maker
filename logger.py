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
    size INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS inventory_marks (
    ts REAL NOT NULL,
    ticker TEXT NOT NULL,
    net_yes_contracts INTEGER,
    avg_entry_price REAL,
    mid_price REAL,
    unrealized_pnl_cents REAL,
    realized_pnl_cents REAL
);

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
    days_remaining REAL
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

CREATE INDEX IF NOT EXISTS idx_scores_ticker_ts ON snapshot_scores(ticker, ts);
CREATE INDEX IF NOT EXISTS idx_fills_ticker_ts ON simulated_fills(ticker, ts);
CREATE INDEX IF NOT EXISTS idx_marks_ticker_ts ON inventory_marks(ticker, ts);
CREATE INDEX IF NOT EXISTS idx_orderbook_ticker_ts ON orderbook_snapshots(ticker, ts);
CREATE INDEX IF NOT EXISTS idx_incentive_snap_ticker_ts ON incentive_program_snapshots(ticker, ts);
CREATE INDEX IF NOT EXISTS idx_stop_losses_ticker_ts ON simulated_stop_losses(ticker, ts);
"""


class SimulationLogger:
    def __init__(self, db_path=None):
        self.db_path = str(db_path or config.DB_PATH)
        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)

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

    def log_simulated_fill(self, ticker: str, side: str, price: int, size: int, ts: float | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO simulated_fills (ts, ticker, side, price, size) VALUES (?, ?, ?, ?, ?)",
                (ts or time.time(), ticker, side, price, size),
            )

    def log_inventory_mark(
        self,
        ticker: str,
        net_yes_contracts: int,
        avg_entry_price: float,
        mid_price: float,
        unrealized_pnl_cents: float,
        realized_pnl_cents: float,
        ts: float | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO inventory_marks
                   (ts, ticker, net_yes_contracts, avg_entry_price, mid_price,
                    unrealized_pnl_cents, realized_pnl_cents)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (ts or time.time(), ticker, net_yes_contracts, avg_entry_price, mid_price,
                 unrealized_pnl_cents, realized_pnl_cents),
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
        ts: float | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO incentive_program_snapshots
                   (ts, ticker, period_reward, target_size, discount_factor_bps, days_remaining)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (ts or time.time(), ticker, period_reward, target_size, discount_factor_bps, days_remaining),
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
