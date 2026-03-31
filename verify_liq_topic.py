from web3 import Web3
import requests

RPC_URL = "https://arb1.arbitrum.io/rpc"
POOL = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"

def main():
    w3 = Web3(Web3.HTTPProvider(RPC_URL))

    # We'll search for the LiquidationCall topic we know (0xe413...)
    # to find recent transactions, then inspect them.
    topic = "0xe413a321e8681d831f4dbccbca790d2952b56f977908e45be37335533e005286"

    latest = w3.eth.block_number
    print(f"Searching for {topic} in last 100,000 blocks...")

    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "eth_getLogs",
        "params": [{
            "address": POOL,
            "fromBlock": hex(latest - 100000),
            "toBlock": hex(latest),
            "topics": [topic]
        }]
    }

    r = requests.post(RPC_URL, json=payload).json()
    logs = r.get('result', [])

    print(f"Found {len(logs)} liquidations.")
    if logs:
        for l in logs[:5]:
            print(f"TX: {l['transactionHash']} | Topic0: {l['topics'][0]}")

if __name__ == "__main__":
    main()
