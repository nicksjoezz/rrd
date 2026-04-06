import httpx
import json
import asyncio

async def debug_dexscreener():
    # Try searching for Camelot to get a base list of small caps
    url = "https://api.dexscreener.com/latest/dex/search/?q=camelot"
    async with httpx.AsyncClient() as client:
        response = await client.get(url, timeout=15)
        data = response.json()
        pairs = data.get("pairs", [])

        print(f"Total pairs found for 'camelot': {len(pairs)}")

        candidates = []
        for p in pairs:
            if p.get("chainId") == "arbitrum" and p.get("dexId") == "camelot":
                mcap = float(p.get("fdv", 0) or 0)
                liq = float(p.get("liquidity", {}).get("usd", 0) or 0)
                base = p.get("baseToken", {}).get("symbol")
                addr = p.get("baseToken", {}).get("address")
                candidates.append((addr, base, mcap, liq))

        print(f"Camelot candidates on Arbitrum: {len(candidates)}")

        dual_found = []
        for addr, symbol, mcap, liq in candidates:
            # Now check if this token has a Uniswap V3 pair
            search_url = f"https://api.dexscreener.com/latest/dex/tokens/{addr}"
            resp = await client.get(search_url, timeout=15)
            token_data = resp.json()
            t_pairs = token_data.get("pairs", [])

            has_uni = False
            for tp in t_pairs:
                if tp.get("chainId") == "arbitrum" and tp.get("dexId") == "uniswap":
                    labels = tp.get("labels", [])
                    if "v2" not in labels:
                        has_uni = True
                        break

            if has_uni:
                print(f"FOUND DUAL: {symbol} ({addr}) - Mcap: ${mcap:,.0f}, Liq: ${liq:,.0f}")
                dual_found.append(addr)

            await asyncio.sleep(0.5)

        print(f"Total dual-listed found: {len(dual_found)}")

if __name__ == "__main__":
    asyncio.run(debug_dexscreener())
