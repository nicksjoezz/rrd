"""
ws_prices.py — Real-time price streaming via WebSocket.

Subscribes to Chainlink AnswerUpdated events to maintain a
near-instant map of token prices.
"""

import asyncio
import logging
import threading
import time
from typing import Dict, Optional, Set
from web3 import Web3
from web3 import AsyncWeb3

from .utils import cfg, get_web3, checksum, logger, load_config

# Event signature for AnswerUpdated(int256,uint256,uint256)
ANSWER_UPDATED_SIG = Web3.keccak(text="AnswerUpdated(int256,uint256,uint256)").hex()

class RealTimePriceStreamer:
    """
    Maintains a thread-safe map of real-time prices for all monitored assets.
    """
    def __init__(self):
        self.prices: Dict[str, float] = {} # addr_lower -> price_usd
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._feed_to_token: Dict[str, str] = {} # feed_addr_lower -> token_addr_lower

    def _build_feed_map(self):
        """Map oracle feeds to their corresponding tokens."""
        # This is a bit complex as Aave tokens don't directly map to Chainlink feeds 1:1 in config
        # We'll use the ones explicitly defined in config.json as a starting point.
        feed_cfg = cfg("oracle", "chainlink_feeds")
        token_cfg = cfg("tokens")

        # Reverse map for symbols
        sym_to_addr = {sym.upper(): info['address'].lower() for sym, info in token_cfg.items()}

        for feed_key, feed_addr in feed_cfg.items():
            sym = feed_key.split('_')[0]
            if sym in sym_to_addr:
                self._feed_to_token[feed_addr.lower()] = sym_to_addr[sym]

    def get_price(self, token_addr: str) -> Optional[float]:
        with self._lock:
            return self.prices.get(token_addr.lower())

    async def _watch_ws(self, ws_url: str):
        self._running = True
        self._build_feed_map()
        feed_addrs = list(self._feed_to_token.keys())

        if not feed_addrs:
            logger.warning("[WS-PRICES] No feeds configured to watch")
            return

        logger.info(f"[WS-PRICES] Connecting to WebSocket: {ws_url[:40]}...")

        try:
            async with AsyncWeb3(AsyncWeb3.WebSocketProvider(ws_url)) as w3:
                # Subscribe to logs from oracle feeds
                sub_id = await w3.eth.subscribe("logs", {
                    "address": [checksum(a) for a in feed_addrs],
                    "topics":  [[ANSWER_UPDATED_SIG]]
                })
                logger.info(f"[WS-PRICES] Subscribed to {len(feed_addrs)} price feed(s)")

                async for response in w3.socket.process_subscriptions():
                    if not self._running: break
                    event = response.get("result", {})
                    if event:
                        feed_addr = event.get("address", "").lower()
                        token_addr = self._feed_to_token.get(feed_addr)
                        if not token_addr: continue

                        # Data contains: current (int256), roundId (uint256), updatedAt (uint256)
                        data_hex = event.get("data", "")
                        if len(data_hex) >= 66:
                            try:
                                raw_price = int(data_hex[2:66], 16)
                                # Handle signed int (2s complement if negative)
                                if raw_price > 2**255: raw_price -= 2**256
                                price = raw_price / 1e8
                                with self._lock:
                                    self.prices[token_addr] = price
                                logger.debug(f"[WS-PRICES] Updated {token_addr[:8]} -> ${price:.2f}")
                            except Exception as e:
                                logger.debug(f"[WS-PRICES] Parse error: {e}")

        except Exception as e:
            logger.error(f"[WS-PRICES] WebSocket error: {e}")
            self._running = False

    def start(self):
        ws_url = cfg("network", "rpc_ws")
        if not ws_url or "YOUR" in ws_url:
            logger.warning("[WS-PRICES] WebSocket URL not set -- real-time prices disabled")
            return

        self._thread = threading.Thread(
            target=lambda: asyncio.run(self._watch_ws(ws_url)),
            daemon=True,
            name="ws-prices"
        )
        self._thread.start()

    def stop(self):
        self._running = False

_streamer: Optional[RealTimePriceStreamer] = None

def get_price_streamer() -> RealTimePriceStreamer:
    global _streamer
    if _streamer is None:
        _streamer = RealTimePriceStreamer()
        _streamer.start()
    return _streamer
