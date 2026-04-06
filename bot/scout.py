import httpx
import json
import time
import logging
import asyncio
from .utils import ROOT_DIR, logger, checksum

WATCHLIST_PATH = ROOT_DIR / "logs" / "watchlist.json"

async def fetch_watchlist():
    """
    Broadly fetch tokens from DexScreener and Arbitrum Token List.
    Identifies candidates, then checks each for dual-listing (Camelot + UniV3).
    """
    potential_tokens = set()

    async with httpx.AsyncClient() as client:
        # 1. Fetch Arbitrum Whitelist
        logger.info("Fetching official Arbitrum whitelist...")
        try:
            url = "https://tokenlist.arbitrum.io/ArbTokenLists/arbed_arb_whitelist_era.json"
            resp = await client.get(url, timeout=15)
            if resp.status_code == 200:
                tokens = resp.json().get("tokens", [])
                for t in tokens:
                    if t.get("chainId") == 42161:
                        addr = t.get("address", "").lower()
                        if addr: potential_tokens.add(addr)
        except Exception as e:
            logger.error(f"Arbitrum Token List fetch failed: {e}")

        # 2. Fetch Trending/Boosted Tokens
        logger.info("Fetching trending tokens from DexScreener Boosts...")
        try:
            url = "https://api.dexscreener.com/token-boosts/top/v1"
            resp = await client.get(url, timeout=15)
            if resp.status_code == 200:
                boosts = resp.json()
                for b in boosts:
                    if b.get("chainId") == "arbitrum":
                        addr = b.get("tokenAddress", "").lower()
                        if addr: potential_tokens.add(addr)
        except Exception as e:
            logger.error(f"DexScreener Boosts fetch failed: {e}")

        # 3. Broad search for Arbitrum tokens
        queries = ["arbitrum", "camelot", "uniswap", "usdc", "weth", "top", "trending", "gainers", "new", "meme", "pepe", "doge"]
        for q in queries:
            try:
                url = f"https://api.dexscreener.com/latest/dex/search/?q={q}"
                resp = await client.get(url, timeout=15)
                if resp.status_code == 200:
                    pairs = resp.json().get("pairs", [])
                    for p in pairs:
                        if p.get("chainId") == "arbitrum":
                            addr = p.get("baseToken", {}).get("address", "").lower()
                            if addr: potential_tokens.add(addr)
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.error(f"DexScreener search failed for {q}: {e}")

        logger.info(f"Discovered {len(potential_tokens)} potential tokens. Verifying dual-listings...")

        # 4. Deep-check each token for dual-listing (Camelot V2/V3 + UniV3)
        watchlist = []
        usdc_addrs = [
            "0xaf88d065e77c8cC2239327C5EDb3A432268e5831".lower(), # Native
            "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8".lower(), # Bridged
            "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1".lower()  # WETH fallback
        ]

        token_list = list(potential_tokens)
        # Check in batches of 30 for efficiency
        for i in range(0, len(token_list), 30):
            batch = token_list[i:i+30]
            try:
                url = f"https://api.dexscreener.com/latest/dex/tokens/{','.join(batch)}"
                resp = await client.get(url, timeout=15)
                if resp.status_code != 200: continue

                data = resp.json()
                pairs = data.get("pairs", [])
                if not pairs: continue

                # Group pairs by token
                token_to_pairs = {}
                for p in pairs:
                    if p.get("chainId") != "arbitrum": continue
                    addr = p.get("baseToken", {}).get("address", "").lower()
                    if addr not in token_to_pairs: token_to_pairs[addr] = []
                    token_to_pairs[addr].append(p)

                for addr, t_pairs in token_to_pairs.items():
                    # We need a Camelot pool and a UniV3 pool, both against USDC/WETH
                    c_pools = [p for p in t_pairs if p.get("dexId") == "camelot" and p.get("quoteToken", {}).get("address").lower() in usdc_addrs]
                    u_pools = [p for p in t_pairs if p.get("dexId") == "uniswap" and "v2" not in p.get("labels", []) and p.get("quoteToken", {}).get("address").lower() in usdc_addrs]

                    if c_pools and u_pools:
                        # Pick best Camelot (V2 preferred for now, or V3)
                        c_v2 = [p for p in c_pools if not p.get("labels")]
                        c_v3 = [p for p in c_pools if "v3" in p.get("labels", [])]

                        best_c = None
                        is_v3 = False
                        if c_v2:
                            best_c = sorted(c_v2, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0), reverse=True)[0]
                            is_v3 = False
                        elif c_v3:
                            best_c = sorted(c_v3, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0), reverse=True)[0]
                            is_v3 = True

                        if best_c:
                            best_u = sorted(u_pools, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0), reverse=True)[0]

                            mcap = float(best_c.get("fdv", 0) or 0)
                            liq = float(best_c.get("liquidity", {}).get("usd", 0) or 0)
                            symbol = best_c.get("baseToken", {}).get("symbol")

                            # Filtering ($10k - $250M Mcap - wider reach)
                            if (10_000 <= mcap <= 250_000_000) and (1_000 <= liq <= 5_000_000):
                                watchlist.append({
                                    "address": checksum(addr),
                                    "symbol": symbol,
                                    "camelotPool": checksum(best_c.get("pairAddress")),
                                    "univ3Pool": checksum(best_u.get("pairAddress")),
                                    "isCamelotV3": is_v3,
                                    "mcap": mcap,
                                    "liq": liq
                                })
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Batch verification failed: {e}")

    return watchlist

async def update_watchlist():
    logger.info("Scouting for arbitrage-ready tokens...")
    watchlist = await fetch_watchlist()

    with open(WATCHLIST_PATH, "w") as f:
        json.dump(watchlist, f, indent=2)

    logger.info(f"Watchlist updated: {len(watchlist)} tokens found.")
    return watchlist

if __name__ == "__main__":
    asyncio.run(update_watchlist())
