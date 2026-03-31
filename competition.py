"""
competition.py -- Tool to find liquidation bot competitors by scanning Aave V3 events.
Optimized for Alchemy Free Tier (10 block range limit).
"""

import json
import time
import sys
from pathlib import Path
from web3 import Web3
import requests

# Add project root to path
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from bot.utils import load_config, get_web3, logger

def find_competitors(lookback_blocks=1000):
    config = load_config()
    # Use the RPC URL from config
    rpc_url = config['network']['rpc_http']
    if "alchemy" in rpc_url and "9fVR5rZUC-g2L5zbeywTA" not in rpc_url:
        # Use the key from config if available
        pass

    w3 = Web3(Web3.HTTPProvider(rpc_url))

    AAVE_V3_POOL = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"
    LIQUIDATION_TOPIC = "0xe410921a33261a758b54010964644061ad574acc3676d093da59f3cb2949640f"

    try:
        latest_block = w3.eth.block_number
    except Exception as e:
        print(f"Error connecting to RPC: {e}")
        return []

    from_block = latest_block - lookback_blocks

    print(f"Scanning last {lookback_blocks} blocks ({from_block} to {latest_block}) for competitors...")

    # Alchemy Free tier limits eth_getLogs to 10 blocks
    chunk_size = 10
    logs = []

    # Track competitors
    competitors = {}

    for start in range(from_block, latest_block, chunk_size):
        end = min(start + chunk_size - 1, latest_block)

        # Progress every 100 blocks
        if (start - from_block) % 100 == 0:
            print(f"  Progress: {((start - from_block) / lookback_blocks) * 100:.1f}% | Logs found: {len(logs)}")

        try:
            chunk_logs = w3.eth.get_logs({
                "fromBlock": start,
                "toBlock": end,
                "address": Web3.to_checksum_address(AAVE_V3_POOL),
                "topics": [LIQUIDATION_TOPIC]
            })

            for log in chunk_logs:
                logs.append(log)
                tx_hash = log['transactionHash'].hex()

                # Fetch transaction details
                try:
                    tx = w3.eth.get_transaction(tx_hash)
                    bot_wallet = tx['from']
                    bot_contract = tx['to']

                    for addr in [bot_wallet, bot_contract]:
                        if not addr or addr.lower() == AAVE_V3_POOL.lower():
                            continue

                        addr = Web3.to_checksum_address(addr)
                        if addr not in competitors:
                            competitors[addr] = {
                                "address": addr,
                                "count": 0,
                                "is_contract": (addr == bot_contract and addr != bot_wallet),
                                "last_tx": tx_hash
                            }
                        competitors[addr]["count"] += 1
                        competitors[addr]["last_tx"] = tx_hash
                except Exception as e:
                    print(f"    Error fetching tx {tx_hash}: {e}")

        except Exception as e:
            # print(f"    Error fetching logs for range {start}-{end}: {e}")
            time.sleep(0.1) # Brief pause on error

    # Convert to sorted list
    result_list = sorted(competitors.values(), key=lambda x: x["count"], reverse=True)

    # Save to JSON
    with open("competitors.json", "w") as f:
        json.dump(result_list, f, indent=2)

    print(f"\n--- Scan Complete ---")
    print(f"Found {len(logs)} liquidation events and {len(result_list)} unique bots.")
    print(f"Results saved to competitors.json")

    return result_list

if __name__ == "__main__":
    # Scan a larger range to actually find some competitors
    find_competitors(10000)
