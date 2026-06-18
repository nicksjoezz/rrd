import asyncio
import json
import time
import logging
from typing import List, Dict, Set
from web3 import Web3
from .utils import (
    get_web3, cfg, checksum,
    MULTICALL3_ADDR, MULTICALL3_ABI, logger, ROOT_DIR
)

WATCHLIST_PATH = ROOT_DIR / "logs" / "watchlist.json"

# ABIs
UNIV3_POOL_ABI = json.loads('[{"inputs":[],"name":"slot0","outputs":[{"internalType":"uint160","name":"sqrtPriceX96","type":"uint160"},{"internalType":"int24","name":"tick","type":"int24"},{"internalType":"uint16","name":"observationIndex","type":"uint16"},{"internalType":"uint16","name":"observationLength","type":"uint16"},{"internalType":"uint16","name":"observationLengthNext","type":"uint16"},{"internalType":"uint8","name":"feeProtocol","type":"uint8"},{"internalType":"bool","name":"unlocked","type":"bool"}],"stateMutability":"view","type":"function"}]')
CAMELOT_POOL_ABI = json.loads('[{"inputs":[],"name":"getReserves","outputs":[{"internalType":"uint112","name":"reserve0","type":"uint112"},{"internalType":"uint112","name":"reserve1","type":"uint112"},{"internalType":"uint32","name":"blockTimestampLast","type":"uint32"}],"stateMutability":"view","type":"function"}]')
ERC20_ABI = json.loads('[{"inputs":[],"name":"decimals","outputs":[{"internalType":"uint8","name":"","type":"uint8"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"token0","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"token1","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"}]')

class ArbMonitor:
    def __init__(self, on_opportunity=None):
        self.on_opportunity = on_opportunity
        self.watchlist = []
        self.metadata = {} # pool_addr -> {token0, token1, dec0, dec1, is_token1_quote}
        self.watched_pools: Set[str] = set()
        self.w3 = get_web3()
        self._load_watchlist()

    def _load_watchlist(self):
        if WATCHLIST_PATH.exists():
            try:
                with open(WATCHLIST_PATH, "r") as f:
                    new_watchlist = json.load(f)
                    if new_watchlist != self.watchlist:
                        self.watchlist = new_watchlist
                        self._update_watched_pools()
                        self._fetch_metadata()
            except Exception as e:
                logger.error(f"Error loading watchlist: {e}")

    def _update_watched_pools(self):
        self.watched_pools = set()
        for item in self.watchlist:
            self.watched_pools.add(item["univ3Pool"].lower())
            self.watched_pools.add(item["camelotPool"].lower())

    def _fetch_metadata(self):
        """Fetch tokens and decimals for all pools in watchlist."""
        usdc_native = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831".lower()
        usdc_bridged = "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8".lower()
        weth = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1".lower()
        quotes = [usdc_native, usdc_bridged, weth]

        for p in self.watchlist:
            u_addr = p["univ3Pool"]
            c_addr = p["camelotPool"]

            if u_addr not in self.metadata or c_addr not in self.metadata:
                try:
                    u_pool = self.w3.eth.contract(address=checksum(u_addr), abi=ERC20_ABI)
                    u_t0 = u_pool.functions.token0().call().lower()
                    u_t1 = u_pool.functions.token1().call().lower()

                    c_pool = self.w3.eth.contract(address=checksum(c_addr), abi=ERC20_ABI)
                    c_t0 = c_pool.functions.token0().call().lower()
                    c_t1 = c_pool.functions.token1().call().lower()

                    token_decimals = {}
                    for t in set([u_t0, u_t1, c_t0, c_t1]):
                        erc20 = self.w3.eth.contract(address=checksum(t), abi=ERC20_ABI)
                        token_decimals[t] = erc20.functions.decimals().call()

                    self.metadata[u_addr] = {
                        "token0": u_t0, "token1": u_t1,
                        "dec0": token_decimals.get(u_t0, 18), "dec1": token_decimals.get(u_t1, 18),
                        "is_token1_quote": u_t1 in quotes
                    }
                    self.metadata[c_addr] = {
                        "token0": c_t0, "token1": c_t1,
                        "dec0": token_decimals.get(c_t0, 18), "dec1": token_decimals.get(c_t1, 18),
                        "is_token1_quote": c_t1 in quotes
                    }
                except Exception as e:
                    logger.error(f"Metadata fetch failed for {p['symbol']}: {e}")

    def check_all_prices_multicall(self):
        """Fetch prices for all tokens in watchlist using Multicall3 tryAggregate."""
        if not self.watchlist: return []

        opportunities = []
        # tryAggregate(bool requireSuccess, (address target, bytes callData)[] calls)
        MC3_ABI = json.loads('[{"inputs":[{"internalType":"bool","name":"requireSuccess","type":"bool"},{"components":[{"internalType":"address","name":"target","type":"address"},{"internalType":"bytes","name":"callData","type":"bytes"}],"internalType":"struct Multicall3.Call[]","name":"calls","type":"tuple[]"}],"name":"tryAggregate","outputs":[{"components":[{"internalType":"bool","name":"success","type":"bool"},{"internalType":"bytes","name":"returnData","type":"bytes"}],"internalType":"struct Multicall3.Result[]","name":"returnData","type":"tuple[]"}],"stateMutability":"payable","type":"function"}]')
        mc_contract = self.w3.eth.contract(address=checksum(MULTICALL3_ADDR), abi=MC3_ABI)

        calls = []
        pool_order = []

        for item in self.watchlist:
            u_addr = item["univ3Pool"]
            c_addr = item["camelotPool"]
            if u_addr not in self.metadata or c_addr not in self.metadata: continue

            u_pool = self.w3.eth.contract(address=checksum(u_addr), abi=UNIV3_POOL_ABI)
            calls.append({"target": checksum(u_addr), "callData": u_pool.encodeABI("slot0")})
            pool_order.append(("u", u_addr))

            if item.get("isCamelotV3"):
                calls.append({"target": checksum(c_addr), "callData": "0x3850c7bd"}) # globalState
            else:
                c_pool = self.w3.eth.contract(address=checksum(c_addr), abi=CAMELOT_POOL_ABI)
                calls.append({"target": checksum(c_addr), "callData": c_pool.encodeABI("getReserves")})
            pool_order.append(("c", c_addr))

        if not calls: return []

        try:
            results = mc_contract.functions.tryAggregate(False, calls).call()

            decoded_data = {}
            for i, (success, res) in enumerate(results):
                if not success or not res: continue
                ptype, paddr = pool_order[i]
                if ptype == "u":
                    try:
                        dec = self.w3.codec.decode(["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"], res)
                        sqrtP = dec[0]
                        meta = self.metadata[paddr]
                        price_t0_in_t1 = (sqrtP / (2**96))**2 * (10**meta["dec0"] / 10**meta["dec1"])
                        decoded_data[paddr] = price_t0_in_t1 if meta["is_token1_quote"] else (1/price_t0_in_t1 if price_t0_in_t1 > 0 else 0)
                    except: pass
                else:
                    try:
                        meta = self.metadata[paddr]
                        item = next(it for it in self.watchlist if it["camelotPool"] == paddr)
                        if item.get("isCamelotV3"):
                            sqrtP = self.w3.codec.decode(["uint160"], res[:32])[0]
                            price_t0_in_t1 = (sqrtP / (2**96))**2 * (10**meta["dec0"] / 10**meta["dec1"])
                        else:
                            dec = self.w3.codec.decode(["uint112", "uint112", "uint32"], res)
                            res0, res1 = dec[0], dec[1]
                            price_t0_in_t1 = (res1 / 10**meta["dec1"]) / (res0 / 10**meta["dec0"]) if res0 > 0 else 0

                        decoded_data[paddr] = price_t0_in_t1 if meta["is_token1_quote"] else (1/price_t0_in_t1 if price_t0_in_t1 > 0 else 0)
                    except: pass

            for item in self.watchlist:
                u_p = decoded_data.get(item["univ3Pool"])
                c_p = decoded_data.get(item["camelotPool"])
                if u_p is None or c_p is None or u_p == 0 or c_p == 0: continue

                gap = abs(u_p - c_p) / min(u_p, c_p)
                if gap > 0.02:
                    opportunities.append({
                        "token": item["address"], "symbol": item["symbol"],
                        "gap": gap, "u_price": u_p, "c_price": c_p,
                        "u_liq": item.get("liq", 10000), "c_liq": item.get("liq", 10000),
                        "univ3Pool": item["univ3Pool"], "camelotPool": item["camelotPool"],
                        "isCamelotV3": item.get("isCamelotV3", False)
                    })
        except Exception as e:
            logger.error(f"Multicall price check failed: {e}")

        return opportunities

    async def event_listener(self):
        """
        Hybrid Sentinel WSS:
        1. Subscribes to Swap events on watchlist tokens.
        2. Filters by pool addresses to save Alchemy Compute Units.
        3. Triggers immediate price check on any relevant buy/sell.
        """
        from websockets import connect
        # Topic 0 for Uniswap V3 / Algebra Swap
        TOPIC_V3 = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
        # Topic 0 for Uniswap V2 / Camelot Legacy Swap
        TOPIC_V2 = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"

        keys = cfg("network", "alchemy_keys")
        if not keys: return

        key_idx = 0
        while True:
            key = keys[key_idx % len(keys)]
            wss_url = f"wss://arb-mainnet.g.alchemy.com/v2/{key}"
            try:
                async with connect(wss_url) as ws:
                    # We subscribe to BOTH V2 and V3 Swap topics
                    sub = {
                        "jsonrpc":"2.0", "id":1, "method":"eth_subscribe",
                        "params":["logs", {"topics":[[TOPIC_V3, TOPIC_V2]]}]
                    }
                    await ws.send(json.dumps(sub))
                    await ws.recv()
                    logger.info(f"Sentinel WSS active on key {key_idx % len(keys)}")

                    while True:
                        msg = await ws.recv()
                        data = json.loads(msg)
                        res = data.get("params", {}).get("result", {})
                        emitter = res.get("address", "").lower()

                        # Use local filter for efficiency
                        if emitter in self.watched_pools:
                            match = next((it for it in self.watchlist if it["univ3Pool"].lower() == emitter or it["camelotPool"].lower() == emitter), None)
                            if match:
                                logger.info(f"BACKRUN TRIGGER: Swap detected on {match['symbol']} pool {emitter}")
                                # Immediate price check for this token
                                opps = self.check_all_prices_multicall()
                                for opp in opps:
                                    if opp["token"].lower() == match["address"].lower():
                                        if self.on_opportunity: await self.on_opportunity(opp)
            except Exception as e:
                wait = min(60, 5 * (2**(key_idx % 3)))
                logger.warning(f"WSS Error: {e}. Reconnecting in {wait}s...")
                key_idx += 1
                await asyncio.sleep(wait)

    async def static_scanner_loop(self):
        """Static scan every 12 seconds using Multicall."""
        logger.info("Sentinel Static Scanner started (12s interval)")
        while True:
            self._load_watchlist()
            opps = self.check_all_prices_multicall()
            for opp in opps:
                if self.on_opportunity: await self.on_opportunity(opp)
            await asyncio.sleep(12)
