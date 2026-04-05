import requests
import json
import time
import logging
from .utils import ROOT_DIR, logger

WATCHLIST_PATH = ROOT_DIR / "logs" / "watchlist.json"

def fetch_small_caps():
    """Fetch tokens from DexScreener for Arbitrum."""
    try:
        # DexScreener doesn't have a simple "all tokens" API, but we can search for Arbitrum pairs
        # or use their 'tokens' endpoint if we have addresses.
        # For a general scout, we might want to use their "latest" or "search" endpoint.
        # Alternatively, use a more suitable API for discovery if DexScreener is limited.
        # Let's try searching for Arbitrum pairs.
        url = "https://api.dexscreener.com/latest/dex/search/?q=arbitrum"
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            logger.error(f"DexScreener API error: {response.status_code}")
            return []

        data = response.json()
        return data.get("pairs", [])
    except Exception as e:
        logger.error(f"Error fetching tokens from DexScreener: {e}")
        return []

def filter_tokens(pairs):
    """
    Filter pairs based on:
    - Chain: arbitrum
    - Market Cap: $100k - $5M
    - Liquidity: $15k - $100k
    - Dual-Listed: The token must have a pair on Camelot AND Uniswap V3.
    """
    filtered = []

    # Track tokens and their pools
    token_pools = {} # token_address -> { 'camelot': pool_addr, 'univ3': pool_addr, 'symbol': sym }

    for p in pairs:
        if p.get("chainId") != "arbitrum":
            continue

        base_token = p.get("baseToken", {})
        token_addr = base_token.get("address")
        if not token_addr:
            continue

        # Check if it's USDC or WETH pair (we want to arbitrage against USDC)
        quote_token = p.get("quoteToken", {})
        if quote_token.get("symbol") not in ["USDC", "USDT", "WETH"]:
            continue

        mcap = p.get("fdv", 0) # Use FDV as market cap proxy
        liquidity = p.get("liquidity", {}).get("usd", 0)
        dex_id = p.get("dexId")

        # Filter by MCAP and Liquidity
        if not (100_000 <= mcap <= 5_000_000):
            continue
        if not (15_000 <= liquidity <= 100_000):
            continue

        if token_addr not in token_pools:
            token_pools[token_addr] = {"symbol": base_token.get("symbol"), "pools": {}}

        if dex_id == "uniswap": # DexScreener uses uniswap for v3/v2
            token_pools[token_addr]["pools"]["univ3"] = p.get("pairAddress")
        elif dex_id == "camelot":
            token_pools[token_addr]["pools"]["camelot"] = p.get("pairAddress")

    # Final pass: check for dual listing
    for addr, info in token_pools.items():
        if "camelot" in info["pools"] and "univ3" in info["pools"]:
            filtered.append({
                "address": addr,
                "symbol": info["symbol"],
                "camelotPool": info["pools"]["camelot"],
                "univ3Pool": info["pools"]["univ3"]
            })

    return filtered

def update_watchlist():
    logger.info("Scouting for arbitrage-ready tokens...")
    pairs = fetch_small_caps()
    watchlist = filter_tokens(pairs)

    with open(WATCHLIST_PATH, "w") as f:
        json.dump(watchlist, f, indent=2)

    logger.info(f"Watchlist updated: {len(watchlist)} tokens found.")
    return watchlist

if __name__ == "__main__":
    update_watchlist()
