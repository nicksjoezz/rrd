import json
import time
import requests
from web3 import Web3
from concurrent.futures import ThreadPoolExecutor

RPC_URL = "https://arb1.arbitrum.io/rpc"
POOL = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"
# TOPIC_V2 = "0xe413a321e8681d831f4dbccbca790d2952b56f977908e45be37335533e005286"
TOPIC_V3 = "0xe413a321e8681d831f4dbccbca790d2952b56f977908e45be37335533e005286" # wait, earlier I said this was V3?

def get_logs_large_chunk(start, end):
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "eth_getLogs",
        "params": [{
            "address": POOL,
            "fromBlock": hex(start), "toBlock": hex(end),
            "topics": [TOPIC_V3]
        }]
    }
    try:
        r = requests.post(RPC_URL, json=payload, timeout=30)
        return r.json().get('result', [])
    except: return []

def main():
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    try: latest = w3.eth.block_number
    except: return

    # 15 days is ~5.2M blocks. Let's scan 1M for speed.
    lookback = 1000000
    start_block = latest - lookback

    print(f"Scanning {lookback:,} blocks for competitors...")

    # Use 200k chunks for public RPC
    chunk_size = 200000
    chunks = [(i, min(i+chunk_size-1, latest)) for i in range(start_block, latest, chunk_size)]

    all_logs = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda x: get_logs_large_chunk(x[0], x[1]), chunks))
        for res in results:
            if isinstance(res, list): all_logs.extend(res)

    print(f"Found {len(all_logs)} liquidation events. Analyzing bots...")

    competitors = {}

    def process_log(log):
        try:
            tx_hash = log['transactionHash']
            tx = w3.eth.get_transaction(tx_hash)
            wallet = Web3.to_checksum_address(tx['from'])
            contract = Web3.to_checksum_address(tx['to'])
            return [wallet, contract]
        except: return []

    with ThreadPoolExecutor(max_workers=10) as executor:
        addr_lists = list(executor.map(process_log, all_logs))
        for addrs in addr_lists:
            for addr in addrs:
                if addr.lower() == POOL.lower(): continue
                competitors[addr] = competitors.get(addr, 0) + 1

    result = [{"address": k, "count": v} for k, v in sorted(competitors.items(), key=lambda x: x[1], reverse=True)]

    # Save to JSON
    with open("competitors_debug.json", "w") as f:
        json.dump(result, f, indent=2)

    print(f"\n--- COMPETITION DATA (DEBUG) ---")
    print(f"Total Unique Bots Found: {len(result)}")
    if result:
        print("Top 10 Bots:")
        for r in result[:10]:
            print(f"  {r['address']} - {r['count']} liquidations")

if __name__ == "__main__":
    main()
