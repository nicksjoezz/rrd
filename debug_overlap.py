import httpx
import asyncio

async def find_overlap():
    headers = {"Accept": "application/json"}
    async with httpx.AsyncClient(headers=headers) as client:
        # Fetch UniV3 tokens
        uni_tokens = set()
        for page in range(1, 11):
            url = f"https://api.geckoterminal.com/api/v2/networks/arbitrum/dexes/uniswap_v3_arbitrum/pools?page={page}"
            resp = await client.get(url)
            if resp.status_code == 200:
                for p in resp.json().get('data', []):
                    rel = p.get('relationships', {})
                    base = rel.get('base_token', {}).get('data', {}).get('id', '').split('_')[-1].lower()
                    quote = rel.get('quote_token', {}).get('data', {}).get('id', '').split('_')[-1].lower()
                    uni_tokens.add(base)
                    uni_tokens.add(quote)
            await asyncio.sleep(1.1)

        print(f"Total UniV3 tokens found: {len(uni_tokens)}")

        # Fetch Camelot tokens
        cam_tokens = set()
        for page in range(1, 11):
            url = f"https://api.geckoterminal.com/api/v2/networks/arbitrum/dexes/camelot_arbitrum/pools?page={page}"
            resp = await client.get(url)
            if resp.status_code == 200:
                for p in resp.json().get('data', []):
                    rel = p.get('relationships', {})
                    base = rel.get('base_token', {}).get('data', {}).get('id', '').split('_')[-1].lower()
                    quote = rel.get('quote_token', {}).get('data', {}).get('id', '').split('_')[-1].lower()
                    cam_tokens.add(base)
                    cam_tokens.add(quote)
            await asyncio.sleep(1.1)

        print(f"Total Camelot tokens found: {len(cam_tokens)}")

        overlap = uni_tokens.intersection(cam_tokens)
        print(f"Overlap size: {len(overlap)}")

        # Exclude common ones
        exclude = ["0x82af49447d8a07e3bd95bd0d56f35241523fbab1", "0xaf88d065e77c8cc2239327c5edb3a432268e5831", "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8", "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9", "0x2f2a2543b76a4166549f7aab2e75bef0aefc5b0f", "0xda10009cbd5d07dd0cecc66161fc93d7c9000da1"]
        real_overlap = [a for a in overlap if a not in exclude]
        print(f"Real candidates: {len(real_overlap)}")
        print(real_overlap[:10])

if __name__ == "__main__":
    asyncio.run(find_overlap())
