import httpx
import asyncio

async def debug_search():
    queries = ["arbitrum%20camelot", "arbitrum%20uniswap", "arbitrum%20usdc"]
    async with httpx.AsyncClient() as client:
        for q in queries:
            url = f"https://api.dexscreener.com/latest/dex/search/?q={q}"
            resp = await client.get(url)
            pairs = resp.json().get("pairs", [])
            print(f"Query {q} returned {len(pairs)} pairs.")
            for p in pairs[:5]:
                print(f"  {p.get('baseToken', {}).get('symbol')} on {p.get('dexId')} ({p.get('chainId')})")

if __name__ == "__main__":
    asyncio.run(debug_search())
