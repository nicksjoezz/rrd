import requests
import json
from web3 import Web3

RPC_URL = "https://arb1.arbitrum.io/rpc"
POOL = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"

def main():
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    latest = w3.eth.block_number

    print(f"Scanning {POOL} for 0xe410... in last 500,000 blocks...")

    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "eth_getLogs",
        "params": [{
            "address": POOL,
            "fromBlock": hex(latest - 500000),
            "toBlock": hex(latest)
        }]
    }
    r = requests.post(RPC_URL, json=payload).json()
    logs = r.get('result', [])
    found = False
    for l in logs:
        if l['topics'] and l['topics'][0].startswith('0xe410'):
            print(f"FOUND! {l['topics'][0]} in TX {l['transactionHash']}")
            found = True
    if not found:
        print("Still nothing for 0xe410.")

if __name__ == "__main__":
    main()
