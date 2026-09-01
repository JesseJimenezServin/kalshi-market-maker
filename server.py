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

Run with:
    python3 server.py
"""

from __future__ import annotations

import asyncio
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
# Bounds query cost as the DB grows over a long-running collection window;
# the performance chart only needs recent history, not the full lifetime.
HISTORY_ROW_CAP = 2000


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _latest_per_ticker(conn: sqlite3.Connection, table: str, columns: str) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        f"""
        SELECT {columns}
        FROM (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY ts DESC) AS rn
            FROM {table}
        )
        WHERE rn = 1
        """
    ).fetchall()
    return {row["ticker"]: row for row in rows}


def _recent_fills(conn: sqlite3.Connection, limit: int = RECENT_FILLS_LIMIT) -> list[dict]:
    rows = conn.execute(
        "SELECT ts, ticker, side, price, size FROM simulated_fills ORDER BY ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def _performance_history(conn: sqlite3.Connection) -> dict[str, list]:
    """
    Two proxy series, computed in Python from the last HISTORY_ROW_CAP rows
    of each table (chronological order):
      - reward_score: running cumulative sum of combined_score across all
        watched markets. This is a unitless proxy for reward capture, NOT
        dollars -- Kalshi's actual USD payout formula pools every
        participant's snapshot scores over the full period, which this
        codebase doesn't (and can't, from public data alone) reconstruct.
      - pnl_dollars: running total simulated P&L (unrealized + realized)
        across all watched markets, in dollars, using each market's most
        recently observed mark at every point in time.
    """
    score_rows = list(reversed(conn.execute(
        "SELECT ts, combined_score FROM snapshot_scores ORDER BY ts DESC LIMIT ?",
        (HISTORY_ROW_CAP,),
    ).fetchall()))
    reward_series = []
    cumulative = 0.0
    for r in score_rows:
        cumulative += r["combined_score"] or 0.0
        reward_series.append({"ts": r["ts"], "value": cumulative})

    pnl_rows = list(reversed(conn.execute(
        "SELECT ts, ticker, unrealized_pnl_cents, realized_pnl_cents FROM inventory_marks ORDER BY ts DESC LIMIT ?",
        (HISTORY_ROW_CAP,),
    ).fetchall()))
    last_pnl_by_ticker: dict[str, float] = {}
    pnl_series = []
    for r in pnl_rows:
        total_cents = (r["unrealized_pnl_cents"] or 0.0) + (r["realized_pnl_cents"] or 0.0)
        last_pnl_by_ticker[r["ticker"]] = total_cents
        pnl_series.append({"ts": r["ts"], "value": sum(last_pnl_by_ticker.values()) / 100.0})

    return {"reward_score": reward_series, "pnl_dollars": pnl_series}


def build_dashboard_payload() -> dict:
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
            "unrealized_pnl_cents, realized_pnl_cents",
        )
        programs = _latest_per_ticker(
            conn, "incentive_program_snapshots",
            "ticker, ts, period_reward, target_size, discount_factor_bps, days_remaining",
        )
        books = _latest_per_ticker(conn, "orderbook_snapshots", "ticker, ts, yes_levels, no_levels")

        tickers = sorted(
            set(scores) | set(inventory) | set(programs) | set(books) | set(config.WATCHED_MARKET_TICKERS)
        )

        markets = []
        for ticker in tickers:
            program = programs.get(ticker)
            inv = inventory.get(ticker)
            score = scores.get(ticker)
            book = books.get(ticker)

            daily_pool = None
            discount_factor = None
            target_size = None
            if program is not None:
                target_size = program["target_size"]
                discount_factor = (program["discount_factor_bps"] or 0) / 10000.0
                if program["days_remaining"]:
                    # Same "reward per day" heuristic incentive_screener.py
                    # already uses -- not a literal daily-reset figure.
                    daily_pool = program["period_reward"] / program["days_remaining"]

            net_pnl = None
            if inv is not None:
                net_pnl = ((inv["unrealized_pnl_cents"] or 0.0) + (inv["realized_pnl_cents"] or 0.0)) / 100.0

            markets.append({
                "ticker": ticker,
                "target_size": target_size,
                "discount_factor": discount_factor,
                "daily_pool": daily_pool,
                "net_pnl": net_pnl,
                "qualifies_yes": bool(score["qualifies_yes"]) if score is not None else None,
                "qualifies_no": bool(score["qualifies_no"]) if score is not None else None,
                "combined_score": score["combined_score"] if score is not None else None,
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
    try:
        while True:
            payload = await asyncio.to_thread(build_dashboard_payload)
            await websocket.send_text(json.dumps(payload))
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        logger.info("Dashboard WebSocket client disconnected")


if __name__ == "__main__":
    logger.info(
        "Starting dashboard server (READ-ONLY -- no orders can be placed) at http://127.0.0.1:8000"
    )
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
