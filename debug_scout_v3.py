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

        for p in pairs:
            dex_id = p.get("dexId")
            base = p.get("baseToken", {}).get("symbol")
            mcap = float(p.get("fdv", 0) or 0)
            liq = float(p.get("liquidity", {}).get("usd", 0) or 0)
            print(f"  {base} ({dex_id}): Mcap={mcap:,.0f}, Liq={liq:,.0f}")

if __name__ == "__main__":
    asyncio.run(debug_dexscreener())
