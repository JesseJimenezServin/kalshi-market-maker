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
from typing import Awaitable, Callable

import websockets

import config
from auth import KalshiSigner
from orderbook import OrderbookManager

logger = logging.getLogger(__name__)

_WS_PATH = "/trade-api/ws/v2"

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
        dropping it (per the April 2026 get_snapshot addition)."""
        msg = {
            "id": self._next_id(),
            "cmd": "update_subscription",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": [ticker],
                "action": "get_snapshot",
            },
        }
        await ws.send(json.dumps(msg))
        logger.info("%s: requested resync snapshot", ticker)

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
        headers = self._auth_headers()
        async with websockets.connect(self.ws_url, additional_headers=headers) as ws:
            self._ws = ws
            await self._subscribe(ws)

            async for raw in ws:
                msg = json.loads(raw)
                await self._handle(ws, msg)

    async def _handle(self, ws, msg: dict) -> None:
        msg_type = msg.get("type")

        if msg_type in ("orderbook_snapshot", "orderbook_delta"):
            self.orderbook_manager.handle_message(msg)

            if msg_type == "orderbook_delta":
                body = msg.get("msg", {})
                ticker = body.get("market_ticker")
                if ticker and self.orderbook_manager.needs_resync(ticker):
                    await self._request_resync(ws, ticker)

        elif msg_type == "trade":
            if self.on_trade is not None:
                await self.on_trade(msg.get("msg", {}))

        elif msg_type == "error":
            logger.error("WebSocket error message: %s", msg)

        elif msg_type == "subscribed":
            logger.info("Subscription confirmed: %s", msg)

        else:
            logger.debug("Unhandled message type=%s: %s", msg_type, msg)
