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
    ULTRA AGGRESSIVE HYBRID DISCOVERY (FINAL OVERHAUL):
    1. GeckoTerminal Trending & New Pools (Arbitrum) - 50 Pages each.
    2. DexScreener Keyword Search (camelot, arbitrum, univ3).
    3. DexScreener Prefix Search (arbitrum a-z, 0-9).
    4. Bulk verification for Dual-Listing (Camelot + UniV3).
    """
    found_candidates = {} # token_addr -> symbol
    headers = {"Accept": "application/json"}

    usdc_addrs = [
        "0xaf88d065e77c8cC2239327C5EDb3A432268e5831".lower(), # Native
        "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8".lower(), # Bridged
        "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1".lower()  # WETH
    ]

    async with httpx.AsyncClient(headers=headers) as client:
        # 1. Gecko Discovery (Aggressive Pagination)
        logger.info("Aggressive Scouting GeckoTerminal (50 pages)...")
        gecko_urls = [
            "https://api.geckoterminal.com/api/v2/networks/arbitrum/pools?page={}",
            "https://api.geckoterminal.com/api/v2/networks/arbitrum/new_pools?page={}"
        ]
        for base_url in gecko_urls:
            for page in range(1, 51):
                pools = await fetch_gecko_pools(client, base_url.format(page))
                if not pools: break
                for p in pools:
                    rel = p.get("relationships", {})
                    for k in ["base_token", "quote_token"]:
                        tk = rel.get(k, {}).get("data", {})
                        if tk:
                            addr = tk.get("id", "").split("_")[-1].lower()
                            if addr and addr not in usdc_addrs:
                                found_candidates[addr] = "GECKO"
                await asyncio.sleep(0.02)

        # 2. DexScreener Keyword Discovery
        terms = ["camelot", "arbitrum", "uniswap", "v3", "magic", "gmx", "grail", "spell", "gns", "pendle", "arb", "uxlink", "corn", "pepe", "wbtc", "sol", "ethfi"]
        logger.info("Aggressive Scouting DexScreener Keywords...")
        for q in terms:
            try:
                url = f"https://api.dexscreener.com/latest/dex/search/?q={q}"
                resp = await client.get(url, timeout=15)
                if resp.status_code == 200:
                    for p in resp.json().get("pairs", []):
                        if p.get("chainId") == "arbitrum":
                            addr = p.get("baseToken", {}).get("address", "").lower()
                            if addr and addr not in usdc_addrs:
                                found_candidates[addr] = p.get("baseToken", {}).get("symbol")
                await asyncio.sleep(0.05)
            except: pass

        # 3. DexScreener Prefix Discovery (Finding long-tail tokens)
        prefixes = [f"arbitrum {c}" for c in string.ascii_lowercase + string.digits]
        logger.info(f"Deep scouting DexScreener ({len(prefixes)} prefix queries)...")
        for q in prefixes:
            try:
                url = f"https://api.dexscreener.com/latest/dex/search/?q={q.replace(' ', '%20')}"
                resp = await client.get(url, timeout=10)
                if resp.status_code == 200:
                    for p in resp.json().get("pairs", []):
                        if p.get("chainId") == "arbitrum":
                            addr = p.get("baseToken", {}).get("address", "").lower()
                            if addr and addr not in usdc_addrs:
                                found_candidates[addr] = p.get("baseToken", {}).get("symbol")
                await asyncio.sleep(0.01)
            except: pass

        logger.info(f"Analyzing {len(found_candidates)} unique candidates for dual-listing...")
        watchlist = []
        token_list = list(found_candidates.keys())

        # 4. Batch verification
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
                    u_pools = [p for p in t_pairs if p.get("dexId") == "uniswap" or "v3" in p.get("labels", [])]

                    if c_pools and u_pools:
                        best_c = sorted(c_pools, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0), reverse=True)[0]
                        best_u = sorted(u_pools, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0), reverse=True)[0]

                        mcap = float(best_c.get("fdv", 0) or 0)
                        liq_c = float(best_c.get("liquidity", {}).get("usd", 0) or 0)
                        liq_u = float(best_u.get("liquidity", {}).get("usd", 0) or 0)
                        min_liq = min(liq_c, liq_u)

                        # RELAXED FILTERS: MCAP $50k-$50M, Liquidity > $500
                        if 50000 <= mcap <= 50000000 and min_liq >= 500:
                            watchlist.append({
                                "address": checksum(addr),
                                "symbol": best_c.get("baseToken", {}).get("symbol", "???"),
                                "camelotPool": checksum(best_c.get("pairAddress")),
                                "univ3Pool": checksum(best_u.get("pairAddress")),
                                "isCamelotV3": "v3" in best_c.get("labels", []) or "algebra" in best_c.get("labels", []),
                                "mcap": mcap,
                                "liq": min_liq
                            })
                            logger.info(f"FOUND DUAL: {best_c.get('baseToken', {}).get('symbol')} | Liq: ${min_liq:,.0f}")
                await asyncio.sleep(0.4)
            except Exception: pass

    return watchlist

async def update_watchlist():
    logger.info("Executing Ultra-High-Discovery Scout Cycle...")
    watchlist = await fetch_watchlist()
    with open(WATCHLIST_PATH, "w") as f:
        json.dump(watchlist, f, indent=2)
    logger.info(f"Watchlist updated: {len(watchlist)} candidates found.")
    return watchlist

if __name__ == "__main__":
    asyncio.run(update_watchlist())
