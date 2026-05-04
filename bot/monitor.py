import asyncio
import json
import time
import logging
from typing import List, Dict, Set
from web3 import Web3
from websockets import connect
from .utils import (
    get_web3, cfg, checksum,
    MULTICALL3_ADDR, MULTICALL3_ABI, logger, ROOT_DIR
)

WATCHLIST_PATH = ROOT_DIR / "logs" / "watchlist.json"

# ABIs
UNIV3_POOL_ABI = json.loads('[{"inputs":[],"name":"slot0","outputs":[{"internalType":"uint160","name":"sqrtPriceX96","type":"uint160"},{"internalType":"int24","name":"tick","type":"int24"},{"internalType":"uint16","name":"observationIndex","type":"uint16"},{"internalType":"uint16","name":"observationLength","type":"uint16"},{"internalType":"uint16","name":"observationLengthNext","type":"uint16"},{"internalType":"uint8","name":"feeProtocol","type":"uint8"},{"internalType":"bool","name":"unlocked","type":"bool"}],"stateMutability":"view","type":"function"}]')
ALGEBRA_POOL_ABI = json.loads('[{"inputs":[],"name":"globalState","outputs":[{"internalType":"uint160","name":"price","type":"uint160"},{"internalType":"int24","name":"tick","type":"int24"},{"internalType":"uint16","name":"fee","type":"uint16"},{"internalType":"uint16","name":"timepointIndex","type":"uint16"},{"internalType":"uint16","name":"communityFeeToken0","type":"uint16"},{"internalType":"uint16","name":"communityFeeToken1","type":"uint16"},{"internalType":"bool","name":"unlocked","type":"bool"}],"stateMutability":"view","type":"function"}]')
CAMELOT_POOL_ABI = json.loads('[{"inputs":[],"name":"getReserves","outputs":[{"internalType":"uint112","name":"reserve0","type":"uint112"},{"internalType":"uint112","name":"reserve1","type":"uint112"},{"internalType":"uint32","name":"blockTimestampLast","type":"uint32"}],"stateMutability":"view","type":"function"}]')
POOL_INFO_ABI = json.loads('[{"inputs":[],"name":"token0","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"token1","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"}]')
ERC20_ABI = json.loads('[{"inputs":[],"name":"decimals","outputs":[{"internalType":"uint8","name":"","type":"uint8"}],"stateMutability":"view","type":"function"}]')

class ArbMonitor:
    def __init__(self, on_opportunity=None):
        self.on_opportunity = on_opportunity
        self.watchlist = []
        self.metadata = {} # token_addr -> decimals
        self.w3 = get_web3()
        self.pool_tokens = {} # pool_addr -> (t0, t1)
        self.watched_pools = set()
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
                        self._fetch_pool_tokens()
            except Exception as e:
                logger.error(f"Error loading watchlist: {e}")

    def _update_watched_pools(self):
        self.watched_pools = set()
        for item in self.watchlist:
            for p in item["pools"]:
                self.watched_pools.add(p.lower())

    def _fetch_metadata(self):
        tokens = set()
        for item in self.watchlist:
            for t in item["tokens"]:
                tokens.add(t.lower())

        needed = [t for t in tokens if t not in self.metadata]
        if not needed: return

        logger.info(f"Fetching metadata for {len(needed)} tokens...")
        try:
            mc_contract = self.w3.eth.contract(address=checksum(MULTICALL3_ADDR), abi=MULTICALL3_ABI)
            calls = []
            for t in needed:
                erc20 = self.w3.eth.contract(address=checksum(t), abi=ERC20_ABI)
                calls.append((checksum(t), erc20.encodeABI("decimals")))

            _, return_data = mc_contract.functions.aggregate(calls).call()
            for i, t in enumerate(needed):
                try:
                    self.metadata[t] = self.w3.codec.decode(["uint8"], return_data[i])[0]
                except:
                    self.metadata[t] = 18
        except Exception as e:
            logger.error(f"Metadata fetch failed: {e}")

    def _fetch_pool_tokens(self):
        pools = set()
        for item in self.watchlist:
            for p in item["pools"]:
                pools.add(p.lower())

        needed = [p for p in pools if p not in self.pool_tokens]
        if not needed: return

        logger.info(f"Fetching token info for {len(needed)} pools...")
        try:
            mc_contract = self.w3.eth.contract(address=checksum(MULTICALL3_ADDR), abi=MULTICALL3_ABI)
            calls = []
            for p in needed:
                p_contract = self.w3.eth.contract(address=checksum(p), abi=POOL_INFO_ABI)
                calls.append((checksum(p), p_contract.encodeABI("token0")))
                calls.append((checksum(p), p_contract.encodeABI("token1")))

            _, return_data = mc_contract.functions.aggregate(calls).call()
            for i, p in enumerate(needed):
                try:
                    t0 = self.w3.codec.decode(["address"], return_data[i*2])[0].lower()
                    t1 = self.w3.codec.decode(["address"], return_data[i*2+1])[0].lower()
                    self.pool_tokens[p] = (t0, t1)
                except:
                    pass
        except Exception as e:
            logger.error(f"Pool token fetch failed: {e}")

    def get_price_from_res(self, res, ptype, meta):
        if ptype == "univ3" or ptype == "algebra":
            sqrtP = self.w3.codec.decode(["uint160"], res[:32])[0]
            # price_t0_in_t1 = (sqrtP / 2^96)^2 * (10^dec0 / 10^dec1)
            price_t0_in_t1 = (sqrtP / (2**96))**2 * (10**meta["dec0"] / 10**meta["dec1"])
            return price_t0_in_t1
        else:
            # UniV2 / CamelotV2
            dec = self.w3.codec.decode(["uint112", "uint112", "uint32"], res)
            res0, res1 = dec[0], dec[1]
            # price_t0_in_t1 = (res1 / 10^dec1) / (res0 / 10^dec0)
            return (res1 / 10**meta["dec1"]) / (res0 / 10**meta["dec0"]) if res0 > 0 else 0

    def check_all_prices_multicall(self):
        if not self.watchlist: return []

        opportunities = []
        # We need a version of Multicall ABI that includes tryAggregate
        MC3_RESILIENT_ABI = json.loads('[{"inputs":[{"internalType":"bool","name":"requireSuccess","type":"bool"},{"components":[{"internalType":"address","name":"target","type":"address"},{"internalType":"bytes","name":"callData","type":"bytes"}],"internalType":"struct Multicall3.Call[]","name":"calls","type":"tuple[]"}],"name":"tryAggregate","outputs":[{"components":[{"internalType":"bool","name":"success","type":"bool"},{"internalType":"bytes","name":"returnData","type":"bytes"}],"internalType":"struct Multicall3.Result[]","name":"returnData","type":"tuple[]"}],"stateMutability":"payable","type":"function"}]')
        mc_contract = self.w3.eth.contract(address=checksum(MULTICALL3_ADDR), abi=MC3_RESILIENT_ABI)

        calls = []
        pool_meta = []
        added_pools = set()

        for item in self.watchlist:
            for i, p_addr in enumerate(item["pools"]):
                p_addr = p_addr.lower()
                if p_addr not in self.pool_tokens: continue
                if p_addr in added_pools: continue

                added_pools.add(p_addr)
                ptype = item["versions"][i]
                if ptype in ["univ3", "algebra"]:
                    # slot0() for UniV3, globalState() for Algebra
                    sig = "0x3850c7bd" if ptype == "univ3" else "0x1ad57897"
                    calls.append({"target": checksum(p_addr), "callData": sig})
                else:
                    # getReserves() for V2
                    calls.append({"target": checksum(p_addr), "callData": "0x0902f1ac"})

                t0, t1 = self.pool_tokens[p_addr]
                pool_meta.append({
                    "addr": p_addr,
                    "type": ptype,
                    "token0": t0,
                    "token1": t1,
                    "dec0": self.metadata.get(t0, 18),
                    "dec1": self.metadata.get(t1, 18)
                })

        if not calls: return []

        try:
            results = mc_contract.functions.tryAggregate(False, calls).call()
            prices = {}
            for i, (success, res) in enumerate(results):
                if not success or not res: continue
                meta = pool_meta[i]
                try:
                    price_t0_in_t1 = self.get_price_from_res(res, meta["type"], meta)
                    prices[meta["addr"]] = price_t0_in_t1
                except: continue

            for item in self.watchlist:
                amount = 1.0
                valid = True
                for i in range(len(item["pools"])):
                    p_addr = item["pools"][i].lower()
                    if p_addr not in prices:
                        valid = False; break

                    t_in = item["tokens"][i].lower()
                    t0, _ = self.pool_tokens[p_addr]

                    p_t0_t1 = prices[p_addr]
                    if t_in == t0:
                        # token0 -> token1
                        amount *= p_t0_t1
                    else:
                        # token1 -> token0
                        amount /= p_t0_t1 if p_t0_t1 > 0 else 1

                if valid and amount > 1.002: # 0.2% threshold
                    opportunities.append({
                        "symbol": item["symbol"],
                        "type": item["type"],
                        "tokens": item["tokens"],
                        "pools": item["pools"],
                        "versions": item["versions"],
                        "fees": item["fees"],
                        "gap": amount - 1.0,
                        "profit_pct": amount - 1.0,
                        "expected_output": amount
                    })
        except Exception as e:
            logger.error(f"Multicall check failed: {e}")

        return opportunities

    async def static_scanner_loop(self):
        logger.info("Sentinel Static Scanner started (12s interval)")
        while True:
            self._load_watchlist()
            opps = self.check_all_prices_multicall()
            for opp in opps:
                if self.on_opportunity: await self.on_opportunity(opp)
            await asyncio.sleep(12)

    async def event_listener(self):
        """
        WSS Listener for real-time backrunning.
        """
        TOPIC_V3 = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
        TOPIC_V2 = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"

        keys = cfg("network", "alchemy_keys")
        if not keys: return

        key_idx = 0
        while True:
            key = keys[key_idx % len(keys)]
            wss_url = f"wss://arb-mainnet.g.alchemy.com/v2/{key}"
            try:
                async with connect(wss_url) as ws:
                    sub = {
                        "jsonrpc":"2.0", "id":1, "method":"eth_subscribe",
                        "params":["logs", {"topics":[[TOPIC_V3, TOPIC_V2]]}]
                    }
                    await ws.send(json.dumps(sub))
                    await ws.recv()
                    logger.info(f"Sentinel WSS active on key index {key_idx % len(keys)}")

                    while True:
                        msg = await ws.recv()
                        data = json.loads(msg)
                        res = data.get("params", {}).get("result", {})
                        emitter = res.get("address", "").lower()

                        if emitter in self.watched_pools:
                            logger.info(f"BACKRUN TRIGGER on {emitter}")
                            opps = self.check_all_prices_multicall()
                            for opp in opps:
                                if emitter in [p.lower() for p in opp["pools"]]:
                                    if self.on_opportunity: await self.on_opportunity(opp)
            except Exception as e:
                wait = min(60, 5 * (2**(key_idx % 3)))
                logger.warning(f"WSS Error: {e}. Reconnecting in {wait}s...")
                key_idx += 1
                await asyncio.sleep(wait)
