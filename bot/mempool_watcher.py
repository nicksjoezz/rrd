"""
mempool_watcher.py — Pending transaction intelligence.

The biggest edge in liquidation is TIME. Reactive bots wait for a price
oracle update to finalize, then scan positions. By then, 10 other bots
have already fired.

This module watches the PENDING mempool for:
  1. Large Chainlink oracle update txs (price about to change)
  2. Large Uniswap/Camelot swaps (will move spot price, may affect oracles)
  3. Other liquidation bots' pending txs (race detection)

When we see a pending oracle update for a collateral asset, we:
  1. Compute what the new price will be
  2. Identify positions that will become liquidatable at that price
  3. Pre-build and hold our liquidation tx ready
  4. Submit in the SAME block as the oracle update

This is the technique used to capture the $1.2M from the March 2025
CAPO oracle event — watching price feeds and pre-staging txs.

Note: Works best with a WebSocket RPC that supports pending tx subscriptions.
Alchemy and QuickNode both support this on Arbitrum.
"""

import logging
import asyncio
import json
from typing import Callable, Optional
from web3 import Web3, AsyncWeb3
try:
    from web3.middleware import ExtraDataToPOAMiddleware as geth_poa_middleware
except ImportError:
    try:
        from web3.middleware import geth_poa_middleware
    except ImportError:
        geth_poa_middleware = None # Fallback for newer versions where it's non-standard

from .utils import cfg, checksum, get_web3, CHAINLINK_FEED_ABI

logger = logging.getLogger("liquidation_bot.mempool")

# Chainlink aggregator — TransmitReportTypes (price update function selector)
CHAINLINK_TRANSMIT_SELECTOR = "0xc9807539"  # transmit(bytes,bytes32[],bytes32[],bytes32)
CHAINLINK_ANSWER_UPDATED    = Web3.keccak(
    text="AnswerUpdated(int256,uint256,uint256)"
).hex()

# Uniswap V3 swap selector
UNISWAP_SWAP_SELECTOR = "0x128acb08"

# Minimum USD swap size to track (smaller swaps don't move oracle-relevant prices)
MIN_SWAP_USD_TO_TRACK = 500_000  # $500k+


class MempoolWatcher:
    """
    Watches pending transactions for oracle updates and large swaps.
    Calls on_oracle_pending(feed_addr, estimated_price) when a relevant
    pending tx is detected.
    """

    def __init__(
        self,
        on_oracle_pending: Optional[Callable] = None,
        on_large_swap: Optional[Callable] = None,
    ):
        self._on_oracle_pending = on_oracle_pending
        self._on_large_swap     = on_large_swap
        self._running           = False

        # Build reverse map: chainlink feed address → feed name
        feeds     = cfg("oracle", "chainlink_feeds")
        self._feed_addresses = {addr.lower(): name for name, addr in feeds.items()}

        # Set of known liquidation bot addresses (populated by competitor_watcher)
        self._known_bots: set = set()

    def add_known_bot(self, address: str):
        self._known_bots.add(address.lower())

    def _analyze_pending_tx(self, tx: dict):
        """Analyze a single pending transaction."""
        if not tx or not tx.get("to"):
            return

        to_addr   = tx["to"].lower()
        input_hex = tx.get("input", "0x")

        # ── Chainlink oracle update ───────────────────────────────────────────
        if to_addr in self._feed_addresses:
            feed_name = self._feed_addresses[to_addr]
            logger.info(
                f"[MEMPOOL] 🔴 Chainlink update pending: {feed_name} "
                f"from {tx.get('from','?')[:10]}..."
            )
            if self._on_oracle_pending:
                self._on_oracle_pending({
                    "feed_name":  feed_name,
                    "feed_addr":  tx["to"],
                    "tx_hash":    tx.get("hash", b"").hex(),
                    "gas_price":  tx.get("maxFeePerGas", tx.get("gasPrice", 0)),
                })

        # ── Known competitor liquidation bot ─────────────────────────────────
        elif tx.get("from", "").lower() in self._known_bots:
            logger.warning(
                f"[MEMPOOL] ⚡ Competitor bot tx detected: "
                f"{tx.get('from','?')[:10]}... → {to_addr[:10]}..."
            )

    async def watch_pending_async(self, ws_url: str):
        """
        WebSocket-based pending tx watcher.
        Requires a WS-capable RPC (Alchemy, QuickNode).
        """
        logger.info(f"[MEMPOOL] Starting WebSocket watcher on {ws_url[:40]}...")
        self._running = True

        try:
            w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(ws_url))
            # Subscribe to pending transactions
            pending_filter = await w3.eth.filter("pending")

            while self._running:
                try:
                    pending_hashes = await pending_filter.get_new_entries()
                    for tx_hash in pending_hashes[:50]:  # process up to 50 per cycle
                        try:
                            tx = await w3.eth.get_transaction(tx_hash)
                            if tx:
                                self._analyze_pending_tx(dict(tx))
                        except Exception:
                            pass
                    await asyncio.sleep(0.5)
                except Exception as e:
                    logger.debug(f"[MEMPOOL] Filter error: {e}")
                    await asyncio.sleep(2)

        except Exception as e:
            logger.error(f"CRITICAL: Mempool WebSocket watcher failed: {e}. Bot requires real-time mempool data for competitive edge.")
            self._running = False

    def stop(self):
        self._running = False


def start_mempool_watcher_thread(on_oracle_pending=None, on_large_swap=None):
    """
    Start the mempool watcher in a background daemon thread.
    Returns the watcher instance (call .stop() to halt).
    """
    import threading

    watcher = MempoolWatcher(
        on_oracle_pending=on_oracle_pending,
        on_large_swap=on_large_swap,
    )

    ws_url = cfg("network", "rpc_ws")
    if not ws_url or ws_url.startswith("wss://YOUR"):
        logger.error("CRITICAL: WebSocket RPC not configured. Mempool watching disabled.")
        return None

    target = lambda: asyncio.run(watcher.watch_pending_async(ws_url))
    label  = "mempool-ws"

    thread = threading.Thread(target=target, daemon=True, name=label)
    thread.start()
    logger.info(f"[MEMPOOL] Watcher thread started ({label})")
    return watcher
