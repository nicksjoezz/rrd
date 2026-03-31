import requests
from web3 import Web3

RPC_URL = "https://arb1.arbitrum.io/rpc"
POOL = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"

def main():
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    latest = w3.eth.block_number

    print(f"Scanning {POOL} for logs with 4 topics...")

    step = 100000
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "eth_getLogs",
        "params": [{
            "address": POOL,
            "fromBlock": hex(latest - step),
            "toBlock": hex(latest)
        }]
    }
    r = requests.post(RPC_URL, json=payload).json()
    logs = r.get('result', [])
    topics = {}
    for l in logs:
        t0 = l['topics'][0]
        count = len(l['topics'])
        if count == 4:
            topics[t0] = topics.get(t0, 0) + 1
            print(f"Found 4-topic log: {t0} | TX: {l['transactionHash']}")

    print("\nSummary of 4-topic events:")
    for t, c in topics.items():
        print(f"  {t}: {c} times")

if __name__ == "__main__":
    main()
