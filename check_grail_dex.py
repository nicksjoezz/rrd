import httpx
import asyncio

async def check_grail():
    addr = "0x3d9907F9a368ad0a51Be60f7Da3b97cf940982D8"
    url = f"https://api.dexscreener.com/latest/dex/tokens/{addr}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
        pairs = resp.json().get("pairs", [])
        for p in pairs:
            if p.get("dexId") == "camelot":
                print(f"Camelot Pair: {p.get('pairAddress')} Labels: {p.get('labels')}")
            if p.get("dexId") == "uniswap":
                print(f"Uniswap Pair: {p.get('pairAddress')} Labels: {p.get('labels')}")

if __name__ == "__main__":
    asyncio.run(check_grail())
