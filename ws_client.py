"""
Authenticated WebSocket client for Kalshi market data.

Key facts baked into this implementation (see Kalshi API docs/changelog):
  - The connection itself is authenticated at the HTTP-upgrade step, even
    for public channels like orderbook_delta/ticker/trade. Sign the exact
    WS path ("/trade-api/ws/v2"), method GET, no query string.
  - Kalshi sends a Ping control frame ~every 10s; the `websockets` library
    answers Pongs automatically, so we don't need custom keepalive logic.
  - orderbook_delta supports a "get_snapshot" action to resync without
    dropping the subscription -- use this instead of a full reconnect
    when we detect a sequence gap.
  - Reconnect with exponential backoff is required, not optional.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from typing import Awaitable, Callable

import certifi
import websockets

import config
from auth import KalshiSigner
from orderbook import OrderbookManager

logger = logging.getLogger(__name__)

_WS_PATH = "/trade-api/ws/v2"

# `websockets` builds its own TLS context via the `ssl` module's default
# trust store, which (unlike `requests`/urllib3) does NOT automatically use
# certifi's bundle. On python.org's macOS builds this default store is
# frequently missing root CAs ("unable to get local issuer certificate"),
# so the handshake fails even though plain `requests` calls to the same
# Kalshi API work fine. Building the context from certifi explicitly fixes
# this regardless of the local Python install's certificate configuration.
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

TradeHandler = Callable[[dict], Awaitable[None]]


class KalshiWebSocketClient:
    def __init__(
        self,
        tickers: list[str],
        orderbook_manager: OrderbookManager,
        signer: KalshiSigner | None = None,
        ws_url: str | None = None,
        on_trade: TradeHandler | None = None,
    ):
        self.tickers = tickers
        self.orderbook_manager = orderbook_manager
        self.signer = signer
        self.ws_url = ws_url or config.WS_URL
        self.on_trade = on_trade
        self._cmd_id = 0
        self._ws = None
        self._orderbook_delta_sid: int | None = None
        self._trade_sid: int | None = None
        # Tickers with a resync already requested but not yet answered by a
        # fresh snapshot -- avoids firing a new get_snapshot request on
        # every single delta that arrives while still stale (confirmed live
        # 2026-09-01: without this, a busy market can trigger several
        # redundant resync requests within milliseconds of each other).
        self._resync_pending: set[str] = set()

    def _next_id(self) -> int:
        self._cmd_id += 1
        return self._cmd_id

    def _auth_headers(self) -> dict[str, str]:
        if self.signer is None:
            return {}
        return self.signer.sign("GET", _WS_PATH)

    async def _subscribe(self, ws) -> None:
        msg = {
            "id": self._next_id(),
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta", "trade"],
                "market_tickers": self.tickers,
            },
        }
        await ws.send(json.dumps(msg))
        logger.info("Subscribed to orderbook_delta + trade for %d tickers", len(self.tickers))

    async def _request_resync(self, ws, ticker: str) -> None:
        """Ask for a fresh snapshot on an existing subscription without
        dropping it (per the April 2026 get_snapshot addition).

        CONFIRMED live 2026-09-01: this requires "sid" identifying which
        subscription to update -- omitting it fails with Kalshi error code
        12 ("Exactly one subscription ID is required"). We only learn the
        sid from the "subscribed" confirmation after _subscribe() sends,
        so skip (log + retry will naturally happen on the next stale
        delta) if we haven't seen it yet.
        """
        if self._orderbook_delta_sid is None:
            logger.warning("%s: can't request resync yet, no orderbook_delta sid seen", ticker)
            return

        msg = {
            "id": self._next_id(),
            "cmd": "update_subscription",
            "params": {
                "sid": self._orderbook_delta_sid,
                "market_tickers": [ticker],
                "action": "get_snapshot",
            },
        }
        await ws.send(json.dumps(msg))
        self._resync_pending.add(ticker)
        logger.info("%s: requested resync snapshot", ticker)

    def _known_sids(self) -> list[int]:
        return [sid for sid in (self._orderbook_delta_sid, self._trade_sid) if sid is not None]

    async def add_ticker(self, ticker: str) -> bool:
        """
        Adds a ticker to the live subscription (both channels) without
        dropping the connection. CONFIRMED live 2026-09-02 against a
        standalone throwaway connection: Kalshi's update_subscription
        supports action="add_markets" for this, returning a "type": "ok"
        confirmation. Returns False (no-op) if not currently connected or
        the sids aren't known yet -- caller should retry once connected.
        """
        if ticker in self.tickers:
            return True
        if self._ws is None or not self._known_sids():
            logger.warning("%s: can't add ticker yet, not connected or no sid known", ticker)
            return False

        for sid in self._known_sids():
            await self._ws.send(json.dumps({
                "id": self._next_id(),
                "cmd": "update_subscription",
                "params": {"sid": sid, "market_tickers": [ticker], "action": "add_markets"},
            }))
        self.tickers.append(ticker)
        logger.info("%s: added to live subscription", ticker)
        return True

    async def remove_ticker(self, ticker: str) -> bool:
        """
        Removes a ticker from the live subscription (both channels) and
        drops its local order book state. CONFIRMED live 2026-09-02:
        action="delete_markets". Returns False if not currently connected.
        """
        if ticker not in self.tickers:
            return True
        if self._ws is None or not self._known_sids():
            logger.warning("%s: can't remove ticker yet, not connected or no sid known", ticker)
            return False

        for sid in self._known_sids():
            await self._ws.send(json.dumps({
                "id": self._next_id(),
                "cmd": "update_subscription",
                "params": {"sid": sid, "market_tickers": [ticker], "action": "delete_markets"},
            }))
        self.tickers.remove(ticker)
        self.orderbook_manager.remove(ticker)
        self._resync_pending.discard(ticker)
        logger.info("%s: removed from live subscription", ticker)
        return True

    async def run_forever(self) -> None:
        backoff = 1.0
        while True:
            try:
                await self._run_once()
                backoff = 1.0  # reset after a clean session
            except Exception:
                logger.exception("WebSocket session ended with error, reconnecting in %.1fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _run_once(self) -> None:
        # Fresh connection = fresh sids; stale ones from a prior dropped
        # connection would make add_ticker/remove_ticker target a
        # subscription that no longer exists.
        self._orderbook_delta_sid = None
        self._trade_sid = None
        self._resync_pending.clear()

        headers = self._auth_headers()
        async with websockets.connect(self.ws_url, additional_headers=headers, ssl=_SSL_CONTEXT) as ws:
            self._ws = ws
            await self._subscribe(ws)

            async for raw in ws:
                msg = json.loads(raw)
                await self._handle(ws, msg)

    async def _handle(self, ws, msg: dict) -> None:
        msg_type = msg.get("type")

        if msg_type in ("orderbook_snapshot", "orderbook_delta"):
            # handle_message tells us exactly which tickers need a fresh
            # resync as a result of this message -- could be just the one
            # tied to this message (unrecognized shape), or several at
            # once (a sequence gap, since seq is shared across the whole
            # subscription -- see orderbook.py's module docstring).
            needs_resync_tickers = self.orderbook_manager.handle_message(msg)

            if msg_type == "orderbook_snapshot":
                ticker = msg.get("msg", {}).get("market_ticker")
                if ticker:
                    # A fresh snapshot answers any outstanding resync request.
                    self._resync_pending.discard(ticker)

            for ticker in needs_resync_tickers:
                if ticker not in self._resync_pending:
                    await self._request_resync(ws, ticker)

        elif msg_type == "trade":
            if self.on_trade is not None:
                await self.on_trade(msg.get("msg", {}))

        elif msg_type == "error":
            logger.error("WebSocket error message: %s", msg)

        elif msg_type == "subscribed":
            logger.info("Subscription confirmed: %s", msg)
            channel = msg.get("msg", {}).get("channel")
            sid = msg.get("msg", {}).get("sid")
            if channel == "orderbook_delta" and sid is not None:
                self._orderbook_delta_sid = sid
            elif channel == "trade" and sid is not None:
                self._trade_sid = sid

        else:
            logger.debug("Unhandled message type=%s: %s", msg_type, msg)
