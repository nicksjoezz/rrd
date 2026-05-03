import json
import time
import asyncio
import logging
from typing import List, Dict, Set
from collections import defaultdict
from web3 import Web3
from .utils import ROOT_DIR, logger, checksum, cfg, MULTICALL3_ADDR, MULTICALL3_ABI

WATCHLIST_PATH = ROOT_DIR / "logs" / "watchlist.json"
POOL_CACHE_PATH = ROOT_DIR / "logs" / "pool_cache.json"

# Public RPCs for discovery
DISCOVERY_RPCS = [
    "https://arb1.arbitrum.io/rpc",
    "https://arbitrum-one.public.blastapi.io",
    "https://rpc.ankr.com/arbitrum"
]

def get_discovery_w3():
    for rpc in DISCOVERY_RPCS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 30}))
            if w3.is_connected():
                return w3
        except:
            continue
    return Web3(Web3.HTTPProvider(cfg("network", "rpc_http")))

# Factory Configs - Limited historical scanning for demo/initial run
FACTORIES = {
    "uniswap_v3": {
        "address": "0x1F98431c8aD98523631AE4a59f267346ea31F984",
        "deploy_block": 200000000, # Scan from relatively recent block to avoid timeout
        "type": "univ3",
        "topic": Web3.keccak(text="PoolCreated(address,address,uint24,int24,address)").hex()
    },
    "camelot_v3": {
        "address": "0x1a3c9B1d2F0529D97f2afC5136Cc23e58f1FD35B",
        "deploy_block": 200000000,
        "type": "algebra",
        "topic": Web3.keccak(text="Pool(address,address,address)").hex()
    },
    "camelot_v2": {
        "address": "0x6EcCab422D763aC031210895C81787E87B43A652",
        "deploy_block": 200000000,
        "type": "univ2",
        "topic": Web3.keccak(text="PairCreated(address,address,address,uint256)").hex()
    },
    "uniswap_v2": {
        "address": "0xf1D7CC64Fb4452F05c498126312eBE29f30Fbcf9",
        "deploy_block": 200000000,
        "type": "univ2",
        "topic": Web3.keccak(text="PairCreated(address,address,address,uint256)").hex()
    }
}

ERC20_ABI = json.loads('[{"constant":true,"inputs":[],"name":"symbol","outputs":[{"name":"","type":"string"}],"type":"function"},{"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"}]')

class OnChainScout:
    def __init__(self):
        self.w3 = get_discovery_w3()
        self.pool_cache = self._load_cache()

    def _load_cache(self):
        if POOL_CACHE_PATH.exists():
            try:
                with open(POOL_CACHE_PATH, "r") as f:
                    return json.load(f)
            except:
                return {"last_blocks": {}, "pools": []}
        return {"last_blocks": {}, "pools": []}

    def _save_cache(self):
        with open(POOL_CACHE_PATH, "w") as f:
            json.dump(self.pool_cache, f, indent=2)

    async def fetch_logs(self, factory_name, config, to_block):
        from_block = self.pool_cache["last_blocks"].get(factory_name, config["deploy_block"])
        if from_block >= to_block: return []

        all_logs = []
        # Smaller batch for reliability
        batch_size = 50000
        current = from_block

        logger.info(f"Scanning {factory_name} from {current} to {to_block}...")

        # Limit total blocks per scan to avoid long timeouts
        max_blocks = 5000000
        if to_block - from_block > max_blocks:
            logger.info(f"Range too large, limiting to {max_blocks} blocks")
            to_block = from_block + max_blocks

        while current < to_block:
            end = min(current + batch_size, to_block)
            try:
                logs = self.w3.eth.get_logs({
                    "address": checksum(config["address"]),
                    "fromBlock": current,
                    "toBlock": end,
                    "topics": [config["topic"]]
                })
                all_logs.extend(logs)
                current = end + 1
                await asyncio.sleep(0.05)
            except Exception as e:
                if "limit" in str(e).lower() or "range" in str(e).lower() or "too many" in str(e).lower():
                    batch_size //= 2
                    if batch_size < 100: break
                else:
                    logger.error(f"Error fetching logs for {factory_name}: {e}")
                    break

        self.pool_cache["last_blocks"][factory_name] = current
        return all_logs

    def parse_log(self, log, factory_name):
        config = FACTORIES[factory_name]
        try:
            topics = log["topics"]
            data = log["data"]
            if isinstance(data, bytes): data = data.hex()
            if data.startswith("0x"): data = data[2:]

            token0 = checksum("0x" + topics[1].hex()[-40:])
            token1 = checksum("0x" + topics[2].hex()[-40:])

            pool_address = ""
            fee = 0

            if config["type"] == "univ3":
                fee = int(topics[3].hex(), 16)
                pool_address = checksum("0x" + data[-40:])
            elif config["type"] == "algebra":
                pool_address = checksum("0x" + data[-40:])
            elif config["type"] == "univ2":
                pool_address = checksum("0x" + data[24:64])
                fee = 3000

            return {
                "dex": factory_name,
                "pool": pool_address,
                "token0": token0,
                "token1": token1,
                "fee": fee,
                "type": config["type"]
            }
        except Exception:
            return None

    async def scan_factories(self):
        try:
            latest_block = self.w3.eth.block_number
        except:
            return

        new_pools = []
        for name, config in FACTORIES.items():
            logs = await self.fetch_logs(name, config, latest_block)
            for log in logs:
                p = self.parse_log(log, name)
                if p: new_pools.append(p)

        existing_pools = {p["pool"].lower() for p in self.pool_cache["pools"]}
        for p in new_pools:
            if p["pool"].lower() not in existing_pools:
                self.pool_cache["pools"].append(p)
                existing_pools.add(p["pool"].lower())

        self._save_cache()
        logger.info(f"Total pools in cache: {len(self.pool_cache['pools'])}")

    async def get_token_metadata(self, token_addresses: List[str]):
        token_addresses = list(set(token_addresses))
        results = {}
        batch_size = 50

        mc_contract = self.w3.eth.contract(address=checksum(MULTICALL3_ADDR), abi=MULTICALL3_ABI)

        for i in range(0, len(token_addresses), batch_size):
            batch = token_addresses[i:i+batch_size]
            calls = []
            for addr in batch:
                contract = self.w3.eth.contract(address=checksum(addr), abi=ERC20_ABI)
                calls.append((checksum(addr), contract.encodeABI("symbol")))
                calls.append((checksum(addr), contract.encodeABI("decimals")))

            try:
                _, return_data = mc_contract.functions.aggregate(calls).call()

                for j, addr in enumerate(batch):
                    try:
                        # Decode symbol (can be bytes32 or string)
                        try:
                            sym = self.w3.codec.decode(["string"], return_data[j*2])[0]
                        except:
                            sym = self.w3.codec.decode(["bytes32"], return_data[j*2])[0].decode('utf-8').strip('\x00')

                        dec = self.w3.codec.decode(["uint8"], return_data[j*2+1])[0]
                        results[addr.lower()] = {"symbol": sym, "decimals": dec}
                    except:
                        results[addr.lower()] = {"symbol": addr[:6], "decimals": 18}
            except Exception as e:
                for addr in batch:
                    results[addr.lower()] = {"symbol": addr[:6], "decimals": 18}

        return results

    async def filter_and_build_watchlist(self):
        pools = self.pool_cache["pools"]
        by_pair = defaultdict(list)
        for p in pools:
            pair = tuple(sorted([p["token0"].lower(), p["token1"].lower()]))
            by_pair[pair].append(p)

        usdc = checksum("0xaf88d065e77c8cC2239327C5EDb3A432268e5831").lower()
        usdc_e = checksum("0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8").lower()
        weth = checksum("0x82aF49447D8a07e3bd95BD0d56f35241523fBab1").lower()
        base_tokens = [usdc, usdc_e, weth]

        watchlist = []
        needed_tokens = set()

        for pair, pair_pools in by_pair.items():
            dexes = {p["dex"] for p in pair_pools}
            if len(dexes) >= 2:
                if pair[0] in base_tokens or pair[1] in base_tokens:
                    needed_tokens.add(pair[0]); needed_tokens.add(pair[1])
                    best_by_dex = {}
                    for p in pair_pools:
                        if p["dex"] not in best_by_dex: best_by_dex[p["dex"]] = p

                    dex_names = list(best_by_dex.keys())
                    for i in range(len(dex_names)):
                        for j in range(i+1, len(dex_names)):
                            d1, d2 = dex_names[i], dex_names[j]
                            p1, p2 = best_by_dex[d1], best_by_dex[d2]
                            watchlist.append({
                                "type": "dual",
                                "tokens": [p1["token0"], p1["token1"]],
                                "dexes": [d1, d2],
                                "pools": [p1["pool"], p2["pool"]],
                                "versions": [p1["type"], p2["type"]],
                                "fees": [p1["fee"], p2["fee"]]
                            })

        adj = defaultdict(list)
        for p in pools:
            adj[p["token0"].lower()].append(p)
            adj[p["token1"].lower()].append(p)

        for base in base_tokens:
            for p1 in adj.get(base, []):
                mid = p1["token1"].lower() if p1["token0"].lower() == base else p1["token0"].lower()
                if mid == base: continue
                for p2 in adj.get(mid, []):
                    end = p2["token1"].lower() if p2["token0"].lower() == mid else p2["token0"].lower()
                    if end == base or end == mid: continue
                    for p3 in adj.get(end, []):
                        final = p3["token1"].lower() if p3["token0"].lower() == end else p3["token0"].lower()
                        if final == base:
                            needed_tokens.add(base); needed_tokens.add(mid); needed_tokens.add(end)
                            watchlist.append({
                                "type": "triangular",
                                "tokens": [base, mid, end, base],
                                "dexes": [p1["dex"], p2["dex"], p3["dex"]],
                                "pools": [p1["pool"], p2["pool"], p3["pool"]],
                                "versions": [p1["type"], p2["type"], p3["type"]],
                                "fees": [p1["fee"], p2["fee"], p3["fee"]]
                            })

        token_meta = await self.get_token_metadata(list(needed_tokens))
        for item in watchlist:
            syms = [token_meta.get(t.lower(), {}).get("symbol", t[:6]) for t in item["tokens"]]
            if item["type"] == "dual":
                item["symbol"] = f"{syms[0]}/{syms[1]}"
            else:
                item["symbol"] = " -> ".join(syms)

        watchlist = watchlist[:100]
        with open(WATCHLIST_PATH, "w") as f:
            json.dump(watchlist, f, indent=2)
        logger.info(f"Watchlist updated: {len(watchlist)} entries.")

async def update_watchlist():
    scout = OnChainScout()
    await scout.scan_factories()
    await scout.filter_and_build_watchlist()

if __name__ == "__main__":
    asyncio.run(update_watchlist())
