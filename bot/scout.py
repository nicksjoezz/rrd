import httpx
import json
import time
import logging
import asyncio
import string
from .utils import ROOT_DIR, logger, checksum

WATCHLIST_PATH = ROOT_DIR / "logs" / "watchlist.json"

async def fetch_gecko_pools(client, url):
    """Fetch pools from GeckoTerminal."""
    try:
        resp = await client.get(url, timeout=15)
        if resp.status_code == 200:
            return resp.json().get("data", [])
    except Exception as e:
        logger.error(f"GeckoTerminal Error: {e}")
    return []

async def fetch_watchlist():
    """
    ULTRA DISCOVERY (SENTINEL SCOUT):
    1. Official Arbitrum Token List for high-quality assets.
    2. GeckoTerminal (Pages 1-30) for active trending pools.
    3. DexScreener Search (keyword-based) for high-intent terms.
    4. DexScreener Bulk API for dual-listing (Camelot + UniV3) & broad Sentinel filtering.
    """
    found_candidates = {} # token_addr -> symbol
    headers = {"Accept": "application/json"}

    usdc_addrs = [
        "0xaf88d065e77c8cC2239327C5EDb3A432268e5831".lower(), # Native
        "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8".lower(), # Bridged
        "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1".lower()  # WETH
    ]

    async with httpx.AsyncClient(headers=headers) as client:
        # 1. Official Arbitrum Token List
        logger.info("Fetching Arbitrum official token list...")
        try:
            resp = await client.get("https://bridge.arbitrum.io/token-list-42161.json", timeout=15)
            if resp.status_code == 200:
                for t in resp.json().get("tokens", []):
                    addr = t.get("address", "").lower()
                    if addr and addr not in usdc_addrs:
                        found_candidates[addr] = t.get("symbol")
        except: pass

        # 2. GeckoTerminal Discovery
        logger.info("Scouting GeckoTerminal (Pages 1-30)...")
        for page in range(1, 31):
            pools = await fetch_gecko_pools(client, f"https://api.geckoterminal.com/api/v2/networks/arbitrum/pools?page={page}")
            for p in pools:
                rel = p.get("relationships", {})
                for k in ["base_token", "quote_token"]:
                    tk = rel.get(k, {}).get("data", {})
                    if tk:
                        addr = tk.get("id", "").split("_")[-1].lower()
                        if addr and addr not in usdc_addrs:
                            found_candidates[addr] = "GECKO"
            await asyncio.sleep(0.05)

        # 3. DexScreener Keyword Search
        terms = ["camelot", "uniswap%20v3", "magic", "gmx", "grail", "pendle", "arb", "gns", "spell", "usnd", "corn", "uxlink"]
        logger.info(f"Keyword scouting DexScreener ({len(terms)} queries)...")
        for q in terms:
            try:
                url = f"https://api.dexscreener.com/latest/dex/search/?q={q}"
                resp = await client.get(url, timeout=10)
                if resp.status_code == 200:
                    for p in resp.json().get("pairs", []):
                        if p.get("chainId") == "arbitrum":
                            addr = p.get("baseToken", {}).get("address", "").lower()
                            if addr and addr not in usdc_addrs:
                                found_candidates[addr] = p.get("baseToken", {}).get("symbol")
                await asyncio.sleep(0.05)
            except: pass

        logger.info(f"Analyzing {len(found_candidates)} candidates for dual-listing...")
        watchlist = []
        token_list = list(found_candidates.keys())

        # 4. Batch verification & BROAD Filtering (Inclusive for Sentinel)
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
                    # Fix: Uniswap V3 is often labeled as just 'uniswap' on Arbitrum
                    u_pools = [p for p in t_pairs if p.get("dexId") == "uniswap"]

                    if c_pools and u_pools:
                        best_c = sorted(c_pools, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0), reverse=True)[0]
                        best_u = sorted(u_pools, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0), reverse=True)[0]

                        mcap = float(best_c.get("fdv", 0) or 0)
                        liq_c = float(best_c.get("liquidity", {}).get("usd", 0) or 0)
                        liq_u = float(best_u.get("liquidity", {}).get("usd", 0) or 0)
                        min_liq = min(liq_c, liq_u)

                        # SENTINEL FILTERS (BROAD):
                        if min_liq >= 1000:
                            watchlist.append({
                                "address": checksum(addr),
                                "symbol": best_c.get("baseToken", {}).get("symbol", "???"),
                                "camelotPool": checksum(best_c.get("pairAddress")),
                                "univ3Pool": checksum(best_u.get("pairAddress")),
                                "isCamelotV3": "v3" in best_c.get("labels", []),
                                "mcap": mcap,
                                "liq": min_liq
                            })
                            logger.info(f"Verified Match: {best_c.get('baseToken', {}).get('symbol')} | Liq: ${min_liq:,.0f}")
                await asyncio.sleep(0.4)
            except Exception: pass

    return watchlist

async def update_watchlist():
    logger.info("Executing Hybrid Sentinel Scout Cycle...")
    watchlist = await fetch_watchlist()
    with open(WATCHLIST_PATH, "w") as f:
        json.dump(watchlist, f, indent=2)
    logger.info(f"Watchlist updated: {len(watchlist)} candidates found.")
    return watchlist

if __name__ == "__main__":
    asyncio.run(update_watchlist())
