"""
oracle_watcher.py — Monitors Chainlink price feeds for large drops.

Edge: Pre-compute which positions will become liquidatable when a 
pending oracle update lands. Fire liquidation in the same block as 
the price update — before reactive bots even notice.

Real-world evidence: March 2025 CAPO oracle glitch → 34 positions 
liquidated, $1.2M in bonuses captured by bots that were watching.
"""

import time
import logging
from typing import Optional, Callable
from web3 import Web3

from .utils import (
    get_web3, cfg, checksum, CHAINLINK_FEED_ABI
)

import threading
import asyncio
logger = logging.getLogger("liquidation_bot.oracle")


class OracleWatcher:
    """
    Polls Chainlink price feeds and triggers a callback when a
    significant price drop is detected (configurable threshold).
    """

    def __init__(self, on_price_drop: Optional[Callable] = None):
        self._on_price_drop = on_price_drop
        self._last_prices: dict = {}
        self._feeds: dict = {}
        self._init_feeds()

    def _init_feeds(self):
        if not cfg("oracle", "watch_chainlink"):
            return
        w3       = get_web3()
        feed_cfg = cfg("oracle", "chainlink_feeds")
        for name, addr in feed_cfg.items():
            try:
                self._feeds[name] = w3.eth.contract(
                    address=checksum(addr),
                    abi=CHAINLINK_FEED_ABI
                )
                logger.info(f"Oracle: watching {name} at {addr[:10]}...")
            except Exception as e:
                logger.warning(f"Oracle: failed to init feed {name}: {e}")

    def get_price(self, feed_name: str) -> Optional[float]:
        """Get latest price from a Chainlink feed."""
        feed = self._feeds.get(feed_name)
        if not feed:
            return None
        try:
            data = feed.functions.latestRoundData().call()
            return data[1] / 1e8  # 8 decimals
        except Exception as e:
            logger.debug(f"Oracle price error for {feed_name}: {e}")
            return None

    def check_all_feeds(self) -> list:
        """
        Check all feeds. Returns list of significant moves:
        [{"feed": "ETH_USD", "old_price": 3000, "new_price": 2800, "pct_drop": -6.7}]
        """
        threshold = cfg("oracle", "price_drop_trigger_pct")
        significant_moves = []

        for name in self._feeds:
            price = self.get_price(name)
            if price is None:
                continue

            old_price = self._last_prices.get(name)
            self._last_prices[name] = price

            if old_price is None:
                continue

            pct_change = (price - old_price) / old_price * 100

            if abs(pct_change) >= threshold:
                move = {
                    "feed":      name,
                    "old_price": old_price,
                    "new_price": price,
                    "pct_change": round(pct_change, 3),
                    "direction": "DROP" if pct_change < 0 else "PUMP"
                }
                significant_moves.append(move)
                logger.warning(
                    f"[ORACLE] {name}: {move['direction']} "
                    f"{old_price:.2f} → {price:.2f} ({pct_change:+.2f}%)"
                )

                if pct_change < 0 and self._on_price_drop:
                    self._on_price_drop(move)

        return significant_moves

    def get_all_prices(self) -> dict:
        """Snapshot of all current prices."""
        return {name: self.get_price(name) for name in self._feeds}

    async def _watch_price_feeds_ws(self, ws_url: str):
        """
        Targeted real-time re-check when prices move.
        Note: Simple price polling is already efficient,
        but we can listen for new heads to trigger checks.
        """
        from web3 import AsyncWeb3
        try:
            async with AsyncWeb3(AsyncWeb3.WebSocketProvider(ws_url)) as w3:
                sub_id = await w3.eth.subscribe("newHeads")
                logger.info(f"[ORACLE] Price watcher WebSocket active | sub_id={sub_id}")
                async for head in w3.socket.process_subscriptions():
                    # Every new block, check price moves
                    self.check_all_feeds()
        except Exception as e:
            logger.debug(f"[ORACLE] WebSocket price watcher error: {e}")

    def start_websocket_watcher(self):
        ws_url = cfg("network", "rpc_ws")
        if ws_url and not ws_url.startswith("wss://arb1") and "YOUR" not in ws_url:
            threading.Thread(
                target=lambda: asyncio.run(self._watch_price_feeds_ws(ws_url)),
                daemon=True,
                name="oracle-ws"
            ).start()
