"""
ws_streamer.py — Real-time event streaming via WebSocket.

Why this replaces polling:
  Polling getUserAccountData for 25,000 wallets every 30 seconds is slow and
  RPC-expensive. Real-time event streaming is the professional approach:

  - Subscribe to Aave's Borrow, Repay, LiquidationCall, Supply, Withdraw events
  - When any position-affecting event fires, immediately re-check THAT wallet only
  - Result: position state is always fresh, reaction time drops from 30s → <1s

Edge:
  Reactive bots poll. Pro bots stream.
  During a price crash, the first liquidation happens within 2-3 blocks of an
  oracle update. Polling at 30s intervals misses most of these.

Requirements:
  WebSocket-capable RPC endpoint (Alchemy or QuickNode — NOT the free public RPC)
  Set network.rpc_ws in config.json

Fallback:
  If WebSocket is unavailable, falls back gracefully to polling mode.
"""

import asyncio
import logging
import threading
import time
from typing import Callable, Optional, Set
from web3 import Web3

from .utils import cfg, get_web3, checksum, AAVE_POOL_ABI

logger = logging.getLogger("liquidation_bot.ws_streamer")

# Aave V3 event signatures — these are the on-chain events we watch
EVENT_SIGS = {
    "Borrow":          Web3.keccak(text="Borrow(address,address,address,uint256,uint8,uint256,uint16)").hex(),
    "Repay":           Web3.keccak(text="Repay(address,address,address,uint256,bool)").hex(),
    "Supply":          Web3.keccak(text="Supply(address,address,address,uint256,uint16)").hex(),
    "Withdraw":        Web3.keccak(text="Withdraw(address,address,address,uint256)").hex(),
    "LiquidationCall": Web3.keccak(text="LiquidationCall(address,address,address,uint256,uint256,address,bool)").hex(),
}


class WebSocketStreamer:
    """
    Streams Aave events and immediately re-checks affected wallets.
    Runs in a background thread — main bot continues its polling loop.
    """

    def __init__(self, on_position_changed: Optional[Callable] = None):
        """
        Args:
            on_position_changed: callback(user_address, protocol_name)
                called whenever a wallet's position changes on-chain.
                The main bot should immediately re-check that wallet.
        """
        self._on_position_changed = on_position_changed
        self._running             = False
        self._thread: Optional[threading.Thread] = None
        self._hotlist: Set[str]   = set()   # wallets that changed this cycle
        self._hotlist_lock        = threading.Lock()
        self._protocol_pools: dict = {}
        self._build_pool_map()

    def _build_pool_map(self):
        """Map pool addresses → protocol names for event attribution."""
        protocols = cfg("protocols")
        for name, pcfg in protocols.items():
            if pcfg.get("enabled") and pcfg.get("pool"):
                self._protocol_pools[pcfg["pool"].lower()] = name

    def get_and_clear_hotlist(self) -> list:
        """
        Return wallets that had on-chain activity since last call.
        Used by the main bot to prioritize which wallets to re-check.
        """
        with self._hotlist_lock:
            hot = list(self._hotlist)
            self._hotlist.clear()
            return hot

    def _handle_log(self, event_log: dict):
        """Process a single event log — extract the affected user address."""
        try:
            pool_addr = event_log.get("address", "").lower()
            protocol  = self._protocol_pools.get(pool_addr, "unknown")
            topic0    = event_log.get("topics", [None])[0]

            if not topic0:
                return

            topic0_hex = topic0.hex() if isinstance(topic0, bytes) else topic0

            # Aave V3 Event Parameter Mapping:
            # Borrow:          [sig, reserve(idx), onBehalfOf(idx), ref(idx)] | user(non-idx)
            # Repay:           [sig, reserve(idx), user(idx), repayer(idx)]
            # Supply:          [sig, reserve(idx), onBehalfOf(idx), ref(idx)] | user(non-idx)
            # Withdraw:        [sig, reserve(idx), user(idx), to(idx)]
            # LiquidationCall: [sig, col(idx), debt(idx), user(idx)]

            topics    = event_log.get("topics", [])
            user_addr = None

            if topic0_hex == EVENT_SIGS["LiquidationCall"]:
                if len(topics) >= 4:
                    raw = topics[3]
                    user_addr = checksum("0x" + (raw.hex() if isinstance(raw, bytes) else raw)[-40:])
            elif len(topics) >= 3:
                # For Borrow/Repay/Supply/Withdraw, topic[2] is either user or onBehalfOf
                raw = topics[2]
                user_addr = checksum("0x" + (raw.hex() if isinstance(raw, bytes) else raw)[-40:])

            if user_addr and user_addr != "0x0000000000000000000000000000000000000000":
                with self._hotlist_lock:
                    self._hotlist.add(user_addr)

                ev_name = next(
                    (k for k, v in EVENT_SIGS.items() if v == topic0_hex),
                    "Event"
                )
                logger.debug(
                    f"[WS] {ev_name} | user={user_addr[:10]}... | [{protocol}]"
                )

                if self._on_position_changed:
                    self._on_position_changed(user_addr, protocol)

        except Exception as e:
            logger.debug(f"[WS] Log parse error: {e}")

    def _watch_http_polling(self):
        """
        HTTP polling fallback — creates event filters and polls for new logs.
        Less real-time than WebSocket but works with any RPC.
        """
        w3 = get_web3()
        pool_addrs = list(self._protocol_pools.keys())
        if not pool_addrs:
            return

        logger.info("[WS] Starting HTTP event polling (WebSocket not configured)")
        self._running = True

        try:
            # Create a filter for all tracked pool events
            event_filter = w3.eth.filter({
                "address": [checksum(a) for a in pool_addrs],
                "topics":  [list(EVENT_SIGS.values())]
            })
        except Exception as e:
            logger.warning(f"[WS] Cannot create event filter: {e}")
            return

        while self._running:
            try:
                new_logs = event_filter.get_new_entries()
                for log in new_logs:
                    self._handle_log(dict(log))
                time.sleep(3)  # poll every 3 seconds
            except Exception as e:
                logger.debug(f"[WS] Poll error: {e}")
                time.sleep(10)

    async def _watch_ws_async(self, ws_url: str):
        """
        True WebSocket subscription — events arrive in near real-time.
        """
        logger.info(f"[WS] Connecting WebSocket: {ws_url[:40]}...")
        self._running = True

        from web3 import AsyncWeb3
        pool_addrs = list(self._protocol_pools.keys())

        try:
            async with AsyncWeb3(AsyncWeb3.WebSocketProvider(ws_url)) as w3:
                # Subscribe to logs from all tracked pools
                sub_id = await w3.eth.subscribe("logs", {
                    "address": [checksum(a) for a in pool_addrs],
                    "topics":  [list(EVENT_SIGS.values())]
                })
                logger.info(f"[WS] Subscribed to {len(pool_addrs)} pool(s) | sub_id={sub_id}")

                async for response in w3.socket.process_subscriptions():
                    if not self._running:
                        break
                    event = response.get("result", {})
                    if event:
                        self._handle_log(dict(event))

        except Exception as e:
            logger.error(f"[WS] WebSocket error: {e}")
            logger.info("[WS] Falling back to HTTP polling")
            self._watch_http_polling()

    def start(self):
        """Start the streamer in a daemon thread."""
        ws_url = cfg("network", "rpc_ws")
        use_ws = ws_url and not ws_url.startswith("wss://arb1") and "YOUR" not in ws_url

        if use_ws:
            target = lambda: asyncio.run(self._watch_ws_async(ws_url))
            label  = "ws-stream"
        else:
            target = self._watch_http_polling
            label  = "http-events"

        self._thread = threading.Thread(target=target, daemon=True, name=label)
        self._thread.start()
        logger.info(f"[WS] Event streamer started ({label})")

    def stop(self):
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running and (self._thread is None or self._thread.is_alive())
