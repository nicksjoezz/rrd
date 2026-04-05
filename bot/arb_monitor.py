import asyncio
import json
import time
import logging
from typing import List, Dict
from web3 import Web3
from .utils import (
    get_web3, get_alchemy_web3, cfg, checksum,
    MULTICALL3_ADDR, MULTICALL3_ABI, logger, ROOT_DIR
)

WATCHLIST_PATH = ROOT_DIR / "logs" / "watchlist.json"
UNIV3_POOL_ABI = json.loads('[{"inputs":[],"name":"slot0","outputs":[{"internalType":"uint160","name":"sqrtPriceX96","type":"uint160"},{"internalType":"int24","name":"tick","type":"int24"},{"internalType":"uint16","name":"observationIndex","type":"uint16"},{"internalType":"uint16","name":"observationLength","type":"uint16"},{"internalType":"uint16","name":"observationLengthNext","type":"uint16"},{"internalType":"uint8","name":"feeProtocol","type":"uint8"},{"internalType":"bool","name":"unlocked","type":"bool"}],"stateMutability":"view","type":"function"}]')
CAMELOT_POOL_ABI = json.loads('[{"inputs":[],"name":"getReserves","outputs":[{"internalType":"uint112","name":"reserve0","type":"uint112"},{"internalType":"uint112","name":"reserve1","type":"uint112"},{"internalType":"uint32","name":"blockTimestampLast","type":"uint32"}],"stateMutability":"view","type":"function"}]')

class ArbMonitor:
    def __init__(self, on_opportunity=None):
        self.on_opportunity = on_opportunity
        self.watchlist = []
        self._load_watchlist()
        self.w3 = get_web3()
        self.mc = self.w3.eth.contract(address=MULTICALL3_ADDR, abi=MULTICALL3_ABI)

    def _load_watchlist(self):
        if WATCHLIST_PATH.exists():
            with open(WATCHLIST_PATH, "r") as f:
                self.watchlist = json.load(f)

    def check_all_prices_multicall(self):
        """Batch all price checks into one Multicall3 call."""
        if not self.watchlist:
            return []

        calls = []
        for item in self.watchlist:
            # UniV3 slot0
            u_call = self.w3.eth.contract(address=checksum(item["univ3Pool"]), abi=UNIV3_POOL_ABI).encodeABI("slot0")
            calls.append({"target": checksum(item["univ3Pool"]), "callData": u_call})
            # Camelot getReserves
            c_call = self.w3.eth.contract(address=checksum(item["camelotPool"]), abi=CAMELOT_POOL_ABI).encodeABI("getReserves")
            calls.append({"target": checksum(item["camelotPool"]), "callData": c_call})

        try:
            _, return_data = self.mc.functions.aggregate(calls).call()

            opportunities = []
            for i, item in enumerate(self.watchlist):
                u_raw = return_data[i*2]
                c_raw = return_data[i*2 + 1]

                # Decode UniV3 slot0
                u_dec = self.w3.codec.decode(["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"], u_raw)
                sqrtPriceX96 = u_dec[0]
                # Price = (sqrtPriceX96 / 2^96)^2
                u_price = (sqrtPriceX96 / (2**96))**2

                # Decode Camelot getReserves
                c_dec = self.w3.codec.decode(["uint112", "uint112", "uint32"], c_raw)
                res0, res1 = c_dec[0], c_dec[1]
                # Assuming USDC/WETH or USDC/TokenX, needs careful logic for token0/token1
                # For simplicity, let's assume price is res1/res0 or vice versa
                c_price = res1 / res0 if res0 > 0 else 0

                gap = abs(u_price - c_price) / min(u_price, c_price) if u_price > 0 and c_price > 0 else 0
                if gap > 0.02: # 2% gap
                    opportunities.append({
                        "token": item["address"],
                        "symbol": item["symbol"],
                        "gap": gap,
                        "u_price": u_price,
                        "c_price": c_price,
                        "univ3Pool": item["univ3Pool"],
                        "camelotPool": item["camelotPool"]
                    })

            return opportunities
        except Exception as e:
            logger.error(f"Multicall price check failed: {e}")
            return []

    async def event_listener(self):
        """Subscribe to Swap topics on watchlist addresses via WebSockets."""
        # This requires a Web3 instance with a WebSocket provider
        # For this example, we'll use a mock structure as full WSS setup is environment-dependent
        logger.info("Starting WebSocket event listener (Mock)...")
        # In a real scenario, you'd use w3.eth.subscribe('logs', {'topics': [SWAP_TOPIC]})
        # and filter by watchlist addresses.
        while True:
            await asyncio.sleep(60) # Keep loop alive

    async def static_scanner_loop(self):
        """Run the static scanner every 12 seconds."""
        logger.info("Starting Static Scanner loop...")
        while True:
            self._load_watchlist()
            opportunities = self.check_all_prices_multicall()
            for opp in opportunities:
                if self.on_opportunity:
                    await self.on_opportunity(opp)
            await asyncio.sleep(12)
