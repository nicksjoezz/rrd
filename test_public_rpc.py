import requests
import json
url = "https://arb1.arbitrum.io/rpc"
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

# Test 1000 blocks
import time
start_time = time.time()
res = get_logs(447500000, 447501000)
print(f"Status: {res.get('error', 'OK')}")
if 'result' in res:
    print(f"Found {len(res['result'])} logs in 1000 blocks")
