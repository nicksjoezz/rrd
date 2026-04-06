import httpx
import json
import asyncio

async def test_gecko():
    url = "https://api.geckoterminal.com/api/v2/networks/arbitrum/pools?page=1"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
        print(f"Status: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            pools = data.get("data", [])
            print(f"Pools found: {len(pools)}")
            for p in pools[:3]:
                print(f"Pool: {p.get('attributes', {}).get('name')} | Dex: {p.get('attributes', {}).get('dex_id')}")
                print(f"Base Token: {p.get('relationships', {}).get('base_token', {}).get('data', {}).get('id')}")
        else:
            print(resp.text)

if __name__ == "__main__":
    asyncio.run(test_gecko())
