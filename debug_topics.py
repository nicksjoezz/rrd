from web3 import Web3
import requests

RPC_URL = "https://arb1.arbitrum.io/rpc"
POOL = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"

def main():
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    latest = w3.eth.block_number

    # Scan 10,000 blocks for ANY event on the pool to find a liquidation
    print(f"Scanning {POOL} for recent events...")

    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "eth_getLogs",
        "params": [{
            "address": POOL,
            "fromBlock": hex(latest - 5000),
            "toBlock": hex(latest)
        }]
    }

    r = requests.post(RPC_URL, json=payload).json()
    logs = r.get('result', [])

    print(f"Found {len(logs)} logs.")

    topics = set()
    for l in logs:
        if l['topics']:
            topics.add(l['topics'][0])

    print("Detected Topics:")
    for t in topics:
        print(f"  {t}")

if __name__ == "__main__":
    main()
