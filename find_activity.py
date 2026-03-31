import requests
import json
url = "https://arb1.arbitrum.io/rpc"
# Correct topic 0 for LiquidationCall in Aave V3
topic = "0xe413a321e8681d831f4dbccbca790d2952b56f977908e45be37335533e005286"
pool = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"

r = requests.post(url, json={"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]})
latest = int(r.json()['result'], 16)

# Check 1 million blocks
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
    try:
        resp = requests.post(url, json=payload, timeout=20)
        data = resp.json()
        if 'result' in data and len(data['result']) > 0:
            print(f"FOUND {len(data['result'])} liquidations in range {start}-{end}!")
            break
    except: pass
