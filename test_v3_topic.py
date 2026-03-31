import json
import time
import requests
from web3 import Web3
from concurrent.futures import ThreadPoolExecutor

RPC_URL = "https://arb1.arbitrum.io/rpc"
POOL = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"
TOPIC_V2 = "0xe413a321e8681d831f4dbccbca790d2952b56f977908e45be37335533e005286"
TOPIC_V3 = "0x44c58d81365b66dd4b1a7f36c25aa97b8c71c361ee4937adc1a00000227db5dd"

def get_logs_large_chunk(start, end, topic):
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "eth_getLogs",
        "params": [{
            "address": POOL,
            "fromBlock": hex(start), "toBlock": hex(end),
            "topics": [topic]
        }]
    }
    try:
        r = requests.post(RPC_URL, json=payload, timeout=30)
        return r.json().get('result', [])
    except: return []

def main():
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    latest = w3.eth.block_number

    lookback = 1000000
    start_block = latest - lookback

    print(f"Scanning {lookback:,} blocks for TOPIC_V2...")
    logs_v2 = get_logs_large_chunk(start_block, latest, TOPIC_V2)
    print(f"Found {len(logs_v2)} logs for V2.")

    print(f"Scanning {lookback:,} blocks for TOPIC_V3...")
    logs_v3 = get_logs_large_chunk(start_block, latest, TOPIC_V3)
    print(f"Found {len(logs_v3)} logs for V3.")

if __name__ == "__main__":
    main()
