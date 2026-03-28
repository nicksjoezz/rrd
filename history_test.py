"""
history_test.py — Advanced Liquidation History Tracer (Arbitrum).
Scans for ALL LiquidationCall events on the network (Global Trace).
Identifies WHO was liquidated and WHERE (which protocol).
"""

import sys
import time
import json
import argparse
from web3 import Web3
from datetime import datetime, timedelta

# ── Configuration Defaults ───────────────────────────────────────────────────
RPC_DEFAULT = "https://arb1.arbitrum.io/rpc"
# Aave V3 LiquidationCall(address,address,address,uint256,uint256,address,bool)
TOPIC0 = "0xe413a321e8681d831f4dbccbca790d2952b56f977908e45be37335533e005286"

# ── Colors & Design ──────────────────────────────────────────────────────────
def G(text): return f"\033[92m{text}\033[0m"
def R(text): return f"\033[91m{text}\033[0m"
def Y(text): return f"\033[93m{text}\033[0m"
def B(text): return f"\033[94m{text}\033[0m"
def DIM(text): return f"\033[2m{text}\033[0m"

# ── Helpers ──────────────────────────────────────────────────────────────────
def load_config():
    try:
        with open("config.json") as f:
            return json.load(f)
    except:
        return {}

def get_token_map(cfg):
    tokens = cfg.get("tokens", {})
    return {info["address"].lower(): sym for sym, info in tokens.items()}

def get_protocol_map(cfg):
    protocols = cfg.get("protocols", {})
    res = {}
    for name, p in protocols.items():
        if isinstance(p, dict) and p.get("pool"):
            res[p["pool"].lower()] = name.upper()
    return res

def format_addr(addr):
    s = str(addr)
    return f"{s[:6]}...{s[-4:]}"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=float, default=7.0, help="Days to look back")
    parser.add_argument("--global-scan", action="store_true", default=True, help="Scan all addresses for topic0")
    args = parser.parse_args()

    cfg = load_config()
    token_map = get_token_map(cfg)
    proto_map = get_protocol_map(cfg)
    
    # Try to get best RPC from config
    rpc_url = cfg.get("network", {}).get("rpc_http", RPC_DEFAULT)
    alc_key = cfg.get("network", {}).get("alchemy_key", "")
    if alc_key and alc_key != "YOUR_ALCHEMY_KEY_HERE":
        if not alc_key.startswith("http"):
            rpc_url = f"https://arb-mainnet.g.alchemy.com/v2/{alc_key}"
        else:
            rpc_url = alc_key

    print(f"\n{B('═'*62)}")
    print(B("  LiqBot — Global Liquidation Tracer"))
    print(B(f"  Arbitrum One · Last {args.days} Days"))
    print(f"{B('═'*62)}\n")

    print(f"  Using RPC: {rpc_url[:35]}...")
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if not w3.is_connected():
        print(R("  Error: Could not connect to Arbitrum RPC"))
        return

    current_block = w3.eth.block_number
    # Arbitrum is ~4 blocks/sec.
    blocks_back = int(args.days * 24 * 3600 * 4)
    from_block = max(0, current_block - blocks_back)

    print(f"  Scanning: block {from_block:,} to {current_block:,}")
    print(f"  Filter: LiquidationCall (Global TRACE)\n")

    # We'll scan in chunks. Alchemy handles global topic0 very well.
    chunk = 50000
    liquidations = []
    errors = 0

    for start in range(from_block, current_block, chunk):
        end = min(start + chunk - 1, current_block)
        pct = int((start - from_block) / blocks_back * 100)
        pct = min(100, pct)
        print(f"\r  Progress: {pct}% | Chunks with data: {G(len(liquidations))}...", end="", flush=True)
        
        try:
            logs = w3.eth.get_logs({
                "topics":  [TOPIC0],
                "fromBlock": start,
                "toBlock":   end
            })
            
            for log in logs:
                # topics: [topic0, collateralAsset, debtAsset, user]
                topics = log.get("topics", [])
                if len(topics) < 4: continue
                
                col_token  = "0x" + topics[1].hex()[-40:]
                debt_token = "0x" + topics[2].hex()[-40:]
                borrower   = "0x" + topics[3].hex()[-40:]
                
                source = log["address"].lower()
                proto  = proto_map.get(source, format_addr(source))
                
                liquidations.append({
                    "block": log["blockNumber"],
                    "tx":    log["transactionHash"].hex(),
                    "borrower": borrower,
                    "protocol": proto,
                    "col": col_token,
                    "debt": debt_token,
                    "address": source
                })
        except Exception as e:
            errors += 1
            if "Query exceeds" in str(e):
                # If chunk is too big for global scan, try smaller
                pass

    print(f"\r  Progress: 100% | Scan Complete.              \n")

    if not liquidations:
        print(Y(f"  No liquidations found in the last {args.days} days."))
        print(DIM(f"  Note: This scanned ALL contracts for the LiquidationCall topic0."))
        return

    print(f"  Found {G(len(liquidations))} liquidations across the network:\n")
    
    # Table Header
    head = f"{'BLOCK':<10} | {'PROTOCOL':<10} | {'BORROWER':<14} | {'PAIR':<12}"
    print(f"  {DIM(head)}")
    print(f"  {DIM('-'*64)}")

    for liq in sorted(liquidations, key=lambda x: x["block"], reverse=True):
        b_sym = token_map.get(liq["borrower"].lower(), format_addr(liq["borrower"]))
        c_sym = token_map.get(liq["col"].lower(), "???")
        d_sym = token_map.get(liq["debt"].lower(), "???")
        
        b_str = format_addr(liq["borrower"])
        pair = f"{c_sym}/{d_sym}"
        p_str = liq["protocol"][:10]
        
        line = f"{liq['block']:<10} | {p_str:<10} | {b_str:<14} | {pair:<12}"
        print(f"  {line}")

    print(f"\n{B('═'*62)}")
    print(f"  Trace finished at {datetime.now().strftime('%H:%M:%S')}")
    print(f"  If your target is missing, it might use a different event signature.")
    print(f"{B('═'*62)}\n")

if __name__ == "__main__":
    main()
