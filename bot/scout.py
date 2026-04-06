import httpx
import json
import time
import logging
import asyncio
from .utils import ROOT_DIR, logger

WATCHLIST_PATH = ROOT_DIR / "logs" / "watchlist.json"

async def fetch_watchlist():
    """
    Fetch tokens from DexScreener and filter for dual-listed small caps.
    Uses the /search and /tokens endpoints for robustness.
    """
    filtered = []
    candidates = []

    async with httpx.AsyncClient() as client:
        # 1. Broad search for Camelot pairs on Arbitrum
        logger.info("Fetching candidates from Camelot...")
        try:
            url = "https://api.dexscreener.com/latest/dex/search/?q=camelot"
            resp = await client.get(url, timeout=15)
            if resp.status_code == 200:
                pairs = resp.json().get("pairs", [])
                for p in pairs:
                    # Filter for Arbitrum, Camelot, and explicitly V2 pairs if possible
                    # Camelot V2 pairs usually don't have a label, V3/Algebra do.
                    labels = p.get("labels", [])
                    if p.get("chainId") == "arbitrum" and p.get("dexId") == "camelot" and not labels:
                        mcap = float(p.get("fdv", 0) or 0)
                        liq = float(p.get("liquidity", {}).get("usd", 0) or 0)

                        # Apply initial Mcap/Liquidity filters
                        # Plan: $100k - $5M Mcap, $15k - $100k Liq
                        if (80_000 <= mcap <= 6_000_000) and (10_000 <= liq <= 150_000):
                            candidates.append({
                                "address": p.get("baseToken", {}).get("address"),
                                "symbol": p.get("baseToken", {}).get("symbol"),
                                "camelotPool": p.get("pairAddress"),
                                "mcap": mcap,
                                "liq": liq
                            })
        except Exception as e:
            logger.error(f"Error fetching Camelot candidates: {e}")

        logger.info(f"Found {len(candidates)} candidates. Checking for Uniswap V3 dual-listings...")

        # 2. For each candidate, check for a Uniswap V3 pair
        for c in candidates:
            try:
                url = f"https://api.dexscreener.com/latest/dex/tokens/{c['address']}"
                resp = await client.get(url, timeout=15)
                if resp.status_code == 200:
                    pairs = resp.json().get("pairs", [])
                    uni_pool = None
                    for p in pairs:
                        if p.get("chainId") == "arbitrum" and p.get("dexId") == "uniswap":
                            labels = p.get("labels", [])
                            if "v2" not in labels:
                                uni_pool = p.get("pairAddress")
                                break

                    if uni_pool:
                        filtered.append({
                            "address": c["address"],
                            "symbol": c["symbol"],
                            "camelotPool": c["camelotPool"],
                            "univ3Pool": uni_pool,
                            "mcap": c["mcap"],
                            "liq": c["liq"]
                        })
                await asyncio.sleep(0.5) # Be nice to API
            except Exception as e:
                logger.error(f"Error checking dual-listing for {c['symbol']}: {e}")

    return filtered

async def update_watchlist():
    logger.info("Scouting for arbitrage-ready tokens...")
    watchlist = await fetch_watchlist()

    with open(WATCHLIST_PATH, "w") as f:
        json.dump(watchlist, f, indent=2)

    logger.info(f"Watchlist updated: {len(watchlist)} tokens found.")
    return watchlist

if __name__ == "__main__":
    asyncio.run(update_watchlist())
