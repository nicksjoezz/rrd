from web3 import Web3
import requests

RPC_URL = "https://arb1.arbitrum.io/rpc"
POOL = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"

def main():
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    latest = w3.eth.block_number

    print(f"Scanning {POOL} for recent events...")

    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "eth_getLogs",
        "params": [{
            "address": POOL,
            "fromBlock": hex(latest - 1000),
            "toBlock": hex(latest)
        }]
    }

    r = requests.post(RPC_URL, json=payload).json()
    logs = r.get('result', [])

    print(f"Found {len(logs)} logs.")

    for l in logs:
        t0 = l['topics'][0]
        if t0 == "0xe413a321e8681d831f4dbccbca790d2952b56f977908e45be37335533e005286":
            print(f"MATCH! Liquidation in TX: {l['transactionHash']}")
        elif t0 == "0x44c58d81365b66dd4b1a7f36c25aa97b8c71c361ee4937adc1a00000227db5dd":
            print(f"0x44c5... in TX: {l['transactionHash']}")

if __name__ == "__main__":
    main()
