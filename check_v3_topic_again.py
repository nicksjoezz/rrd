from web3 import Web3
import requests

RPC_URL = "https://arb1.arbitrum.io/rpc"
POOL = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"

def main():
    w3 = Web3(Web3.HTTPProvider(RPC_URL))

    # Check TOPIC_V3 from my previous successful test (942 events)
    topic_v3 = "0x44c58d81365b66dd4b1a7f36c25aa97b8c71c361ee4937adc1a00000227db5dd"

    latest = w3.eth.block_number
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "eth_getLogs",
        "params": [{
            "address": POOL,
            "fromBlock": hex(latest - 10000),
            "toBlock": hex(latest),
            "topics": [topic_v3]
        }]
    }

    r = requests.post(RPC_URL, json=payload).json()
    logs = r.get('result', [])
    print(f"Found {len(logs)} logs for {topic_v3}")

    if logs:
        log = logs[0]
        print(f"Sample TX: {log['transactionHash']}")
        print(f"Topics: {len(log['topics'])}")
        # If it's 0x44c5..., what is the signature?
        # Let's try hashing some other potential V3 signatures.

if __name__ == "__main__":
    main()
