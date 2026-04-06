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
ERC20_ABI = json.loads('[{"inputs":[],"name":"decimals","outputs":[{"internalType":"uint8","name":"","type":"uint8"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"token0","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"token1","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"}]')

class ArbMonitor:
    def __init__(self, on_opportunity=None):
        self.on_opportunity = on_opportunity
        self.watchlist = []
        self.metadata = {} # pool_addr -> {token0, token1, dec0, dec1, is_token1_quote}
        self.w3 = get_web3()
        self.mc = self.w3.eth.contract(address=MULTICALL3_ADDR, abi=MULTICALL3_ABI)
        self._load_watchlist()

    def _load_watchlist(self):
        if WATCHLIST_PATH.exists():
            with open(WATCHLIST_PATH, "r") as f:
                self.watchlist = json.load(f)
        self._fetch_metadata()

    def _fetch_metadata(self):
        """Fetch tokens and decimals for all pools in watchlist."""
        usdc_v1 = "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8".lower()
        usdc_v2 = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831".lower()
        weth = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1".lower()
        new_pools = [p for p in self.watchlist if p["univ3Pool"] not in self.metadata or p["camelotPool"] not in self.metadata]
        if not new_pools: return

        for p in new_pools:
            try:
                u_addr = checksum(p["univ3Pool"])
                c_addr = checksum(p["camelotPool"])

                u_pool = self.w3.eth.contract(address=u_addr, abi=ERC20_ABI)
                u_t0 = u_pool.functions.token0().call().lower()
                u_t1 = u_pool.functions.token1().call().lower()

                c_pool = self.w3.eth.contract(address=c_addr, abi=ERC20_ABI)
                c_t0 = c_pool.functions.token0().call().lower()
                c_t1 = c_pool.functions.token1().call().lower()

                # Fetch decimals
                tokens = list(set([u_t0, u_t1, c_t0, c_t1]))
                token_decimals = {}
                for t in tokens:
                    erc20 = self.w3.eth.contract(address=checksum(t), abi=ERC20_ABI)
                    token_decimals[t] = erc20.functions.decimals().call()

                self.metadata[p["univ3Pool"]] = {
                    "token0": u_t0, "token1": u_t1,
                    "dec0": token_decimals.get(u_t0, 18), "dec1": token_decimals.get(u_t1, 18),
                    "is_token1_quote": u_t1 in [usdc_v1, usdc_v2, weth]
                }
                self.metadata[p["camelotPool"]] = {
                    "token0": c_t0, "token1": c_t1,
                    "dec0": token_decimals.get(c_t0, 18), "dec1": token_decimals.get(c_t1, 18),
                    "is_token1_quote": c_t1 in [usdc_v1, usdc_v2, weth]
                }
            except Exception as e:
                logger.error(f"Failed to fetch metadata for {p['symbol']}: {e}")

    def check_all_prices_multicall(self):
        """Fetch prices for all tokens in watchlist."""
        if not self.watchlist:
            return []

        opportunities = []
        for item in self.watchlist:
            try:
                if item["univ3Pool"] not in self.metadata or item["camelotPool"] not in self.metadata:
                    continue

                u_meta = self.metadata[item["univ3Pool"]]
                c_meta = self.metadata[item["camelotPool"]]
                is_c_v3 = item.get("isCamelotV3", False)

                # 1. Fetch Data
                u_pool = self.w3.eth.contract(address=checksum(item["univ3Pool"]), abi=UNIV3_POOL_ABI)
                u_data = self.w3.eth.call({"to": checksum(item["univ3Pool"]), "data": u_pool.encodeABI("slot0")})

                if is_c_v3:
                    c_data = self.w3.eth.call({"to": checksum(item["camelotPool"]), "data": "0x3850c7bd"}) # globalState
                else:
                    c_pool = self.w3.eth.contract(address=checksum(item["camelotPool"]), abi=CAMELOT_POOL_ABI)
                    c_data = self.w3.eth.call({"to": checksum(item["camelotPool"]), "data": c_pool.encodeABI("getReserves")})

                # 2. Decode UniV3 Price
                u_dec = self.w3.codec.decode(["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"], u_data)
                sqrtPriceX96 = u_dec[0]
                raw_u_price = (sqrtPriceX96 / (2**96))**2
                u_price_t0_in_t1 = raw_u_price * (10**u_meta["dec0"] / 10**u_meta["dec1"])

                if u_meta["is_token1_quote"]:
                    u_price = u_price_t0_in_t1
                else:
                    u_price = 1 / u_price_t0_in_t1 if u_price_t0_in_t1 > 0 else 0

                # 3. Decode Camelot Price
                if is_c_v3:
                    sqrtP = self.w3.codec.decode(["uint160"], c_data[:32])[0]
                    c_price_t0_in_t1 = (sqrtP / (2**96))**2 * (10**c_meta["dec0"] / 10**c_meta["dec1"])
                    c_liq = item.get("liq", 10000)
                else:
                    c_dec = self.w3.codec.decode(["uint112", "uint112", "uint32"], c_data)
                    res0, res1 = c_dec[0], c_dec[1]
                    if res0 > 0:
                        c_price_t0_in_t1 = (res1 / 10**c_meta["dec1"]) / (res0 / 10**c_meta["dec0"])
                    else:
                        c_price_t0_in_t1 = 0

                    if c_meta["is_token1_quote"]:
                        c_liq = (res1 / 10**c_meta["dec1"]) * 2
                    else:
                        c_liq = (res0 / 10**c_meta["dec0"]) * 2

                if c_meta["is_token1_quote"]:
                    c_price = c_price_t0_in_t1
                else:
                    c_price = 1 / c_price_t0_in_t1 if c_price_t0_in_t1 > 0 else 0

                # 4. Compare
                gap = abs(u_price - c_price) / min(u_price, c_price) if u_price > 0 and c_price > 0 else 0

                # UniV3 liquidity proxy
                u_liq = c_liq

                if gap > 0.01: # 1% gap
                    opportunities.append({
                        "token": item["address"],
                        "symbol": item["symbol"],
                        "gap": gap,
                        "u_price": u_price,
                        "c_price": c_price,
                        "u_liq": u_liq,
                        "c_liq": c_liq,
                        "univ3Pool": item["univ3Pool"],
                        "camelotPool": item["camelotPool"],
                        "isCamelotV3": is_c_v3
                    })
            except Exception as e:
                logger.error(f"Price check failed for {item['symbol']}: {e}")

        return opportunities

    async def event_listener(self):
        """Subscribe to Swap topics on watchlist addresses via WebSockets with key rotation."""
        keys = cfg("network", "alchemy_keys")
        if not keys:
            logger.warning("No Alchemy keys for WSS. Event listener disabled.")
            return

        SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
        from websockets import connect
        import json

        key_idx = 0
        while True:
            key = keys[key_idx % len(keys)]
            wss_url = f"wss://arb-mainnet.g.alchemy.com/v2/{key}"
            logger.info(f"Connecting to WSS with key index {key_idx % len(keys)}...")

            try:
                async with connect(wss_url) as ws:
                    subscribe_msg = {
                        "jsonrpc": "2.0", "id": 1, "method": "eth_subscribe",
                        "params": ["logs", {"topics": [SWAP_TOPIC]}]
                    }
                    await ws.send(json.dumps(subscribe_msg))
                    _ = await ws.recv() # Subscription ID

                    logger.info("Subscribed to Swap logs via WSS")

                    while True:
                        msg = await ws.recv()
                        data = json.loads(msg)
                        params = data.get("params", {})
                        result = params.get("result", {})
                        emitter = result.get("address", "").lower()

                        match = next((item for item in self.watchlist
                                    if item["univ3Pool"].lower() == emitter
                                    or item["camelotPool"].lower() == emitter), None)

                        if match:
                            logger.info(f"Swap event detected on watched pool: {emitter}")
                            opps = self.check_all_prices_multicall()
                            for opp in opps:
                                if opp["token"].lower() == match["address"].lower():
                                    if self.on_opportunity:
                                        await self.on_opportunity(opp)
            except Exception as e:
                logger.error(f"WSS Error: {e}. Rotating key and reconnecting in 5s...")
                key_idx += 1
                await asyncio.sleep(5)

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
