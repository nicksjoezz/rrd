import httpx
import json
import asyncio
import string

async def debug_scout():
    found_candidates = {}
    headers = {"Accept": "application/json"}

    async with httpx.AsyncClient(headers=headers) as client:
        # Prefix discovery
        prefixes = [f"arbitrum {c}" for c in string.ascii_lowercase + string.digits]
        for q in prefixes:
            try:
                url = f"https://api.dexscreener.com/latest/dex/search/?q={q.replace(' ', '%20')}"
                resp = await client.get(url, timeout=10)
                if resp.status_code == 200:
                    for p in resp.json().get("pairs", []):
                        if p.get("chainId") == "arbitrum":
                            addr = p.get("baseToken", {}).get("address", "").lower()
                            if addr:
                                found_candidates[addr] = p.get("baseToken", {}).get("symbol")
            except: pass

        token_list = list(found_candidates.keys())
        print(f"Total tokens found: {len(token_list)}")

        results = []
        for i in range(0, len(token_list), 30):
            batch = token_list[i:i+30]
            url = f"https://api.dexscreener.com/latest/dex/tokens/{','.join(batch)}"
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                for p in data.get("pairs", []):
                    if p.get("chainId") == "arbitrum":
                        results.append(p)

        # Filter for dual listing manually here
        token_to_dexes = {}
        for p in results:
            addr = p.get("baseToken", {}).get("address", "").lower()
            dex = p.get("dexId")
            if addr not in token_to_dexes: token_to_dexes[addr] = set()
            token_to_dexes[addr].add(dex)

        dual = [addr for addr, dexes in token_to_dexes.items() if "camelot" in dexes and "uniswap" in dexes]
        print(f"Dual listed: {len(dual)}")

        for addr in dual:
            pairs = [p for p in results if p.get("baseToken", {}).get("address", "").lower() == addr]
            c_p = [p for p in pairs if p.get("dexId") == "camelot"][0]
            mcap = float(c_p.get("fdv", 0) or 0)
            liq = float(c_p.get("liquidity", {}).get("usd", 0) or 0)
            symbol = c_p.get("baseToken", {}).get("symbol")
            print(f"Token: {symbol} | MCAP: ${mcap/1e6:.2f}M | Liq: ${liq/1e3:.1f}k")

if __name__ == "__main__":
    asyncio.run(debug_scout())
