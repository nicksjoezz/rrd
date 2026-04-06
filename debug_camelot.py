import httpx
import json
import asyncio

async def test_camelot_dex():
    url = "https://api.dexscreener.com/latest/dex/search/?q=camelot"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
        if resp.status_code == 200:
            pairs = resp.json().get("pairs", [])
            print(f"Total camelot pairs: {len(pairs)}")
            for p in pairs[:10]:
                print(f"Pair: {p.get('baseToken', {}).get('symbol')} | Chain: {p.get('chainId')} | Dex: {p.get('dexId')}")

if __name__ == "__main__":
    asyncio.run(test_camelot_dex())
