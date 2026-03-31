import requests
import json
url = "https://arb-mainnet.g.alchemy.com/v2/9fVR5rZUC-g2L5zbeywTA"
def get_logs(start, end):
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "eth_getLogs",
        "params": [{
            "address": "0x794a61358D6845594F94dc1DB02A252b5b4814aD",
            "fromBlock": hex(start), "toBlock": hex(end),
            "topics": ["0xe410921a33261a758b54010964644061ad574acc3676d093da59f3cb2949640f"]
        }]
    }
    r = requests.post(url, json=payload)
    return r.json()

# Get latest block
r = requests.post(url, json={"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]})
latest = int(r.json()['result'], 16)
print(f"Latest: {latest}")

# Scan last 100 blocks
res = get_logs(latest - 100, latest)
print(res)
