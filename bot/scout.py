import httpx
import json
import time
import logging
import asyncio
from .utils import ROOT_DIR, logger, checksum

WATCHLIST_PATH = ROOT_DIR / "logs" / "watchlist.json"

async def fetch_watchlist():
    """
    Broadly fetch tokens from DexScreener and filter for dual-listed small caps.
    Identifies candidates via search, then deep-checks each for dual-listing.
    """
    potential_tokens = {} # addr -> symbol

    async with httpx.AsyncClient() as client:
        # 1. Search for a large batch of pairs on Arbitrum
        # Use more diverse queries to overcome the 30-result limit per search
        queries = [
            "arbitrum%20camelot", "arbitrum%20uniswap", "arbitrum%20usdc", "arbitrum%20weth",
            "arbitrum%20top", "arbitrum%20trending", "arbitrum%20gainers", "arbitrum%20new",
            "arbitrum%200", "arbitrum%201", "arbitrum%202", "arbitrum%203"
        ]
        for q in queries:
            try:
                url = f"https://api.dexscreener.com/latest/dex/search/?q={q}"
                resp = await client.get(url, timeout=15)
                if resp.status_code == 200:
                    pairs = resp.json().get("pairs", [])
                    for p in pairs:
                        if p.get("chainId") == "arbitrum":
                            addr = p.get("baseToken", {}).get("address").lower()
                            symbol = p.get("baseToken", {}).get("symbol")
                            potential_tokens[addr] = symbol
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.error(f"DexScreener search failed for {q}: {e}")

        logger.info(f"Discovered {len(potential_tokens)} potential tokens. Verifying dual-listings...")

        # 2. Deep-check each token for dual-listing (Camelot V2/V3 + UniV3)
        # We need tokens that have USDC pairs on BOTH to fit the flash loan logic
        watchlist = []
        usdc_addrs = [
            "0xaf88d065e77c8cC2239327C5EDb3A432268e5831".lower(), # Native
            "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8".lower(), # Bridged
            "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1".lower()  # WETH fallback
        ]
        for addr, symbol in potential_tokens.items():
            try:
                url = f"https://api.dexscreener.com/latest/dex/tokens/{addr}"
                resp = await client.get(url, timeout=15)
                if resp.status_code != 200: continue

                pairs = resp.json().get("pairs", [])

                # We need a Camelot pool and a UniV3 pool, both against USDC
                c_pools = [p for p in pairs if p.get("dexId") == "camelot" and p.get("quoteToken", {}).get("address").lower() in usdc_addrs]
                u_pools = [p for p in pairs if p.get("dexId") == "uniswap" and "v2" not in p.get("labels", []) and p.get("quoteToken", {}).get("address").lower() in usdc_addrs]

                if c_pools and u_pools:
                    # Prefer Camelot V2 if available
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

                        # Filtering ($50k - $15M Mcap - Volatile small caps)
                        if (50_000 <= mcap <= 15_000_000) and (5_000 <= liq <= 500_000):
                            watchlist.append({
                                "address": checksum(addr),
                                "symbol": symbol,
                                "camelotPool": checksum(best_c.get("pairAddress")),
                                "univ3Pool": checksum(best_u.get("pairAddress")),
                                "isCamelotV3": is_v3,
                                "mcap": mcap,
                                "liq": liq
                            })
                await asyncio.sleep(0.3) # Avoid rate limits
            except Exception as e:
                logger.error(f"Dual-listing verification failed for {symbol}: {e}")

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
