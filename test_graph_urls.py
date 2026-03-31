import requests
def test_graph(url):
    query = "{ accounts(first: 1) { id } }"
    try:
        r = requests.post(url, json={'query': query}, timeout=10)
        print(f"URL: {url} | Status: {r.status_code}")
        if r.status_code == 200:
            print(f"Data: {r.json()}")
        else:
            print(f"Error Body: {r.text}")
    except Exception as e:
        print(f"URL: {url} | Exception: {e}")

urls = [
    "https://api.thegraph.com/subgraphs/name/messari/aave-v3-arbitrum",
    "https://api.thegraph.com/subgraphs/name/aave/protocol-v3-arbitrum",
    "https://api.thegraph.com/subgraphs/name/aave/protocol-v3-arbitrum-one"
]

for u in urls:
    test_graph(u)
