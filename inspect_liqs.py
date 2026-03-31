from web3 import Web3
import requests
import json

RPC_URL = "https://arb1.arbitrum.io/rpc"
POOL = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"

LIQ_ABI = {
  "anonymous": False,
  "inputs": [
    {"indexed": True, "name": "collateralAsset", "type": "address"},
    {"indexed": True, "name": "debtAsset", "type": "address"},
    {"indexed": True, "name": "user", "type": "address"},
    {"indexed": False, "name": "debtToCover", "type": "uint256"},
    {"indexed": False, "name": "liquidatedCollateralAmount", "type": "uint256"},
    {"indexed": False, "name": "liquidator", "type": "address"},
    {"indexed": False, "name": "receiveAToken", "type": "bool"}
  ],
  "name": "LiquidationCall",
  "type": "event"
}

def main():
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    latest = w3.eth.block_number

    print(f"Scanning {POOL} for LiquidationCall events...")

    # Try the calculated topic0
    topic0 = Web3.keccak(text="LiquidationCall(address,address,address,uint256,uint256,address,bool)").hex()
    print(f"Calculated Topic0: {topic0}")

    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "eth_getLogs",
        "params": [{
            "address": POOL,
            "fromBlock": hex(latest - 50000),
            "toBlock": hex(latest),
            "topics": [topic0]
        }]
    }

    r = requests.post(RPC_URL, json=payload).json()
    logs = r.get('result', [])

    print(f"Found {len(logs)} logs with calculated topic.")

    if logs:
        # Try to decode one
        log = logs[0]
        print(f"Sample Log TX: {log['transactionHash']}")
        # We can't easily decode without a full contract object here but we can check if it looks right
        print(f"Topics count: {len(log['topics'])}")
        # LiquidationCall should have 4 topics (sig + 3 indexed)
    else:
        print("No logs found with that topic. Scanning ALL logs in a smaller range to find a liquidation...")
        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "eth_getLogs",
            "params": [{
                "address": POOL,
                "fromBlock": hex(latest - 2000),
                "toBlock": hex(latest)
            }]
        }
        r = requests.post(RPC_URL, json=payload).json()
        all_logs = r.get('result', [])
        for l in all_logs:
            if len(l['topics']) == 4: # Potential liquidation
                print(f"Potential Liquidation found? Topic0: {l['topics'][0]} | TX: {l['transactionHash']}")

if __name__ == "__main__":
    main()
