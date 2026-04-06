import httpx
import json
import asyncio

async def check_token(addr):
    url = f"https://api.dexscreener.com/latest/dex/tokens/{addr}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
        if resp.status_code == 200:
            data = resp.json()
            pairs = data.get("pairs", [])
            print(f"Token: {addr}")
            for p in pairs:
                if p.get("chainId") == "arbitrum":
                    print(f"  Dex: {p.get('dexId')} | Liq: ${p.get('liquidity', {}).get('usd')} | FDV: ${p.get('fdv')}")

if __name__ == "__main__":
    # GRAIL, GNS, SPELL, MAGIC, GMX, ZRO
    addrs = [
        "0x3d9907f9a368ad0a51be60f7da3b97cf940982d8",
        "0x296f34f34fed6740685938d82137955c4794c96a",
        "0x3e6648c5a1740947748891583ccca7d79ec2aa3d",
        "0x539bdE0d7Dbd336b79148AA742883198BBF60342",
        "0xfc5A1A6EB076a2C7aD06eD22C90d7E710E35ad0a",
        "0x6985884C4392D348587B19cb9eAAf157F13271cd"
    ]
    for a in addrs:
        asyncio.run(check_token(a))
