import httpx
import json
import asyncio

async def debug_search(q):
    url = f"https://api.dexscreener.com/latest/dex/search/?q={q}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
        if resp.status_code == 200:
            pairs = resp.json().get("pairs", [])
            print(f"Query: {q} | Pairs: {len(pairs)}")
            for p in pairs[:5]:
                print(f"  {p.get('baseToken', {}).get('symbol')} | {p.get('dexId')} | Liq: ${p.get('liquidity', {}).get('usd')}")

if __name__ == "__main__":
    queries = ["arbitrum%20camelot", "arbitrum%20uniswap%20v3"]
    for q in queries:
        asyncio.run(debug_search(q))
