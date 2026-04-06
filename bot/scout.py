import httpx
import json
import time
import logging
import asyncio
from .utils import ROOT_DIR, logger, checksum

WATCHLIST_PATH = ROOT_DIR / "logs" / "watchlist.json"

async def fetch_watchlist():
    """
    ULTRA-AGGRESSIVE Discovery.
    Uses official Arbitrum Token List and multiple DEX endpoints to maximize watchlist.
    """
    potential_tokens = set()
    headers = {"Accept": "application/json"}

    usdc_addrs = [
        "0xaf88d065e77c8cC2239327C5EDb3A432268e5831".lower(), # Native
        "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8".lower(), # Bridged
        "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1".lower()  # WETH
    ]

    async with httpx.AsyncClient(headers=headers) as client:
        # 1. Official Arbitrum Token List (The best source for established tokens)
        logger.info("Fetching official Arbitrum whitelist...")
        try:
            url = "https://tokenlist.arbitrum.io/ArbTokenLists/arbed_arb_whitelist_era.json"
            resp = await client.get(url, timeout=15)
            if resp.status_code == 200:
                for t in resp.json().get("tokens", []):
                    if t.get("chainId") == 42161:
                        potential_tokens.add(t.get("address", "").lower())
        except Exception: pass

        # 2. GeckoTerminal Discovery (All DEXes)
        dexes = ["uniswap_v3_arbitrum", "camelot", "camelot-v3", "sushiswap_arbitrum", "pancakeswap-v3-arbitrum"]
        for dex in dexes:
            logger.info(f"Gecko Discovery: {dex}...")
            for page in range(1, 11):
                try:
                    url = f"https://api.geckoterminal.com/api/v2/networks/arbitrum/dexes/{dex}/pools?page={page}"
                    resp = await client.get(url, timeout=15)
                    if resp.status_code != 200: break
                    data = resp.json()
                    for p in data.get('data', []):
                        rel = p.get("relationships", {})
                        base = rel.get("base_token", {}).get("data", {}).get("id", "").split("_")[-1].lower()
                        quote = rel.get("quote_token", {}).get("data", {}).get("id", "").split("_")[-1].lower()
                        if base and base not in usdc_addrs: potential_tokens.add(base)
                        if quote and quote not in usdc_addrs: potential_tokens.add(quote)
                    await asyncio.sleep(1.1)
                except Exception: break

        logger.info(f"Verifying {len(potential_tokens)} candidates for dual-listing...")
        watchlist = []
        token_list = list(potential_tokens)

        # 3. Batch verify via DexScreener
        for i in range(0, len(token_list), 30):
            batch = token_list[i:i+30]
            try:
                ds_url = f"https://api.dexscreener.com/latest/dex/tokens/{','.join(batch)}"
                resp = await client.get(ds_url, timeout=15)
                if resp.status_code != 200: continue
                data = resp.json()
                token_to_pairs = {}
                for p in data.get("pairs", []):
                    if p.get("chainId") != "arbitrum": continue
                    addr = p.get("baseToken", {}).get("address", "").lower()
                    if addr not in token_to_pairs: token_to_pairs[addr] = []
                    token_to_pairs[addr].append(p)

                for addr, t_pairs in token_to_pairs.items():
                    c_pools = [p for p in t_pairs if p.get("dexId") == "camelot"]
                    u_pools = [p for p in t_pairs if p.get("dexId") == "uniswap" and "v2" not in p.get("labels", [])]

                    if c_pools and u_pools:
                        best_c = sorted(c_pools, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0), reverse=True)[0]
                        best_u = sorted(u_pools, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0), reverse=True)[0]

                        mcap = float(best_c.get("fdv", 0) or 0)
                        liq = float(best_c.get("liquidity", {}).get("usd", 0) or 0)
                        symbol = best_c.get("baseToken", {}).get("symbol", "???")

                        # High Discovery mode: Added regardless of size if it has liquidity
                        if liq >= 100:
                            watchlist.append({
                                "address": checksum(addr),
                                "symbol": symbol,
                                "camelotPool": checksum(best_c.get("pairAddress")),
                                "univ3Pool": checksum(best_u.get("pairAddress")),
                                "isCamelotV3": "v3" in best_c.get("labels", []),
                                "mcap": mcap,
                                "liq": liq
                            })
                await asyncio.sleep(0.5)
            except Exception: pass

    return watchlist

async def update_watchlist():
    logger.info("Executing global discovery cycle...")
    watchlist = await fetch_watchlist()
    with open(WATCHLIST_PATH, "w") as f:
        json.dump(watchlist, f, indent=2)
    logger.info(f"Watchlist updated: {len(watchlist)} tokens found.")
    return watchlist

if __name__ == "__main__":
    asyncio.run(update_watchlist())
