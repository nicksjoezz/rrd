import httpx
import json
import asyncio

async def debug_dexscreener_all_pairs():
    url = "https://api.dexscreener.com/latest/dex/pairs/arbitrum"
    # This endpoint doesn't exist for all pairs, it's search based.
    # Let's use search with just "arbitrum"
    url = "https://api.dexscreener.com/latest/dex/search/?q=arbitrum"
    async with httpx.AsyncClient() as client:
        response = await client.get(url, timeout=15)
        data = response.json()
        pairs = data.get("pairs", [])

        print(f"Total pairs found for 'arbitrum': {len(pairs)}")

        token_to_dexes = {} # addr -> [dexId]
        for p in pairs:
            if p.get("chainId") != "arbitrum": continue
            addr = p.get("baseToken", {}).get("address").lower()
            dex = p.get("dexId")
            if addr not in token_to_dexes: token_to_dexes[addr] = set()
            token_to_dexes[addr].add(dex)

        dual = 0
        for addr, dexes in token_to_dexes.items():
            if "camelot" in dexes and "uniswap" in dexes:
                dual += 1
                # Check quote tokens
                q_tokens = [p.get("quoteToken", {}).get("symbol") for p in pairs if p.get("baseToken", {}).get("address").lower() == addr]
                print(f"Dual: {addr} dexes={dexes} quotes={set(q_tokens)}")

        print(f"Total dual-listed in this search: {dual}")

if __name__ == "__main__":
    asyncio.run(debug_dexscreener_all_pairs())
