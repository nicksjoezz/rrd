import httpx
import json
import asyncio

async def test_targeted_scout():
    async with httpx.AsyncClient() as client:
        # 1. Get pairs from Camelot on Arbitrum
        url = "https://api.dexscreener.com/latest/dex/search/?q=camelot%20arbitrum"
        resp = await client.get(url, timeout=15)
        pairs = resp.json().get("pairs", [])

        tokens_to_check = set()
        for p in pairs:
            if p.get("dexId") == "camelot":
                tokens_to_check.add(p.get("baseToken", {}).get("address"))

        print(f"Found {len(tokens_to_check)} tokens on Camelot to check.")

        found = 0
        for addr in tokens_to_check:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{addr}"
            resp = await client.get(url, timeout=15)
            token_pairs = resp.json().get("pairs", [])

            has_camelot = any(tp.get("dexId") == "camelot" for tp in token_pairs)
            has_univ3 = any(tp.get("dexId") == "uniswap" and "v2" not in tp.get("labels", []) for tp in token_pairs)

            if has_camelot and has_univ3:
                symbol = next(tp.get("baseToken", {}).get("symbol") for tp in token_pairs if tp.get("baseToken", {}).get("address").lower() == addr.lower())
                mcap = float(token_pairs[0].get("fdv", 0) or 0)
                liq = sum(float(tp.get("liquidity", {}).get("usd", 0) or 0) for tp in token_pairs if tp.get("chainId") == "arbitrum")

                print(f"Token {symbol} ({addr}) is DUAL-LISTED. Mcap=${mcap:,.0f}, Total Liq=${liq:,.0f}")
                found += 1
            await asyncio.sleep(0.2)

        print(f"Total dual-listed found: {found}")

if __name__ == "__main__":
    asyncio.run(test_targeted_scout())
