import httpx
import json
import asyncio

async def debug_dexscreener():
    url = "https://api.dexscreener.com/latest/dex/search/?q=arbitrum"
    async with httpx.AsyncClient() as client:
        response = await client.get(url, timeout=15)
        data = response.json()
        pairs = data.get("pairs", [])

        print(f"Total pairs found: {len(pairs)}")

        token_stats = {} # addr -> {dexId: [mcap, liq]}

        for p in pairs:
            if p.get("chainId") != "arbitrum": continue
            addr = p.get("baseToken", {}).get("address")
            dex_id = p.get("dexId")
            mcap = float(p.get("fdv", 0) or 0)
            liq = float(p.get("liquidity", {}).get("usd", 0) or 0)

            if addr not in token_stats: token_stats[addr] = {}
            token_stats[addr][dex_id] = [mcap, liq]

        dual = 0
        for addr, dexes in token_stats.items():
            if "camelot" in dexes and "uniswap" in dexes:
                dual += 1
                print(f"Token {addr}:")
                print(f"  Camelot: Mcap={dexes['camelot'][0]:,.0f}, Liq={dexes['camelot'][1]:,.0f}")
                print(f"  Uniswap: Mcap={dexes['uniswap'][0]:,.0f}, Liq={dexes['uniswap'][1]:,.0f}")

        print(f"Total dual-listed (Camelot + Uniswap): {dual}")

if __name__ == "__main__":
    asyncio.run(debug_dexscreener())
