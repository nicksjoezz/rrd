import requests
import json
url = "https://arb1.arbitrum.io/rpc"
topic = "0xe410921a33261a758b54010964644061ad574acc3676d093da59f3cb2949640f"
pool = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"

r = requests.post(url, json={"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]})
latest = int(r.json()['result'], 16)

# Try chunks of 100k blocks until we find something or hit 1M blocks
for i in range(10):
    end = latest - (i * 100000)
    start = end - 100000
    print(f"Checking {start} to {end}...")
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "eth_getLogs",
        "params": [{
            "address": pool,
            "fromBlock": hex(start), "toBlock": hex(end),
            "topics": [topic]
        }]
    }
    resp = requests.post(url, json=payload)
    data = resp.json()
    if 'result' in data and len(data['result']) > 0:
        print(f"FOUND {len(data['result'])} liquidations in this range!")
        break
    elif 'error' in data:
        print(f"Error: {data['error']['message']}")
