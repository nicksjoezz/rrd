import requests
url = "https://gateway-arbitrum.network.thegraph.com/api/deploy-key/subgraphs/id/4xyasjQeREe7PxnF6wVdobZvCw5mhoHZq3T7guRpuNPf"
# Note: I don't have a deploy key. Let's try the decentralized gateway or a public one.
# Usually Aave has public ones.
url = "https://api.thegraph.com/subgraphs/name/aave/protocol-v3-arbitrum"
q = "{ _meta { block { number } } }"
try:
    r = requests.post(url, json={"query": q}, timeout=10)
    print(r.status_code)
    print(r.text)
except Exception as e:
    print(e)
