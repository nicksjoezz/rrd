import requests
import json

url = "https://arb-mainnet.g.alchemy.com/v2/9fVR5rZUC-g2L5zbeywTA"
payload = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "eth_getLogs",
    "params": [{
        "address": "0x794a61358D6845594F94dc1DB02A252b5b4814aD",
        "fromBlock": "0x1AA5B640",
        "toBlock": "0x1AA5B834",
        "topics": ["0xb3d084820fb1a9decffb176436bd02558d15fac9b0ddfed8c465bc7359d7dce0"]
    }]
}
response = requests.post(url, json=payload)
print(f"Status: {response.status_code}")
print(f"Response: {response.text[:200]}")
