import requests
def test_graph_v2(url):
    query = "{ users(first: 1) { id } }"
    try:
        r = requests.post(url, json={'query': query}, timeout=10)
        print(f"URL: {url} | Status: {r.status_code}")
        if r.status_code == 200:
            print(f"Data: {r.json()}")
        else:
            print(f"Error Body: {r.text}")
    except Exception as e:
        print(f"URL: {url} | Exception: {e}")

# Try decentralized network endpoints (require API key usually, but some are public)
# Or try the specific ID from Graph Explorer
urls = [
    "https://gateway.thegraph.com/api/deployments/id/QmUGh2KvhrvkVw1fX6wVdobZvCw5mhoHZq3T7guRpuNPf", # deployment id
]

for u in urls:
    test_graph_v2(u)
