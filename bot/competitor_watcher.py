"""
competitor_watcher.py — On-chain competitor intelligence.

Monitors real liquidation events emitted by Aave/Radiant to understand:
  1. Who the active liquidator bots are (their wallet addresses)
  2. Which positions they're targeting (size, collateral type)
  3. How fast they react (blocks between position becoming unhealthy and liquidated)
  4. What gas they're paying (tells you their profitability threshold)
  5. Which positions they're SKIPPING (your opportunity gap)

This is publicly available on-chain data — every liquidation is an event.
Use it to calibrate your own strategy in real time.

Run standalone: python -c "from bot.competitor_watcher import report; report()"
"""

import logging
import time
from collections import defaultdict
from typing import List
from web3 import Web3

from .utils import get_web3, cfg, checksum, get_address_to_symbol, AAVE_POOL_ABI

logger = logging.getLogger("liquidation_bot.competitors")

# Aave V3 LiquidationCall event topic
LIQUIDATION_TOPIC = Web3.keccak(
    text="LiquidationCall(address,address,address,uint256,uint256,address,bool)"
).hex()

LIQUIDATION_CALL_ABI = [{
    "name": "LiquidationCall",
    "type": "event",
    "inputs": [
        {"name": "collateralAsset",     "type": "address", "indexed": True},
        {"name": "debtAsset",           "type": "address", "indexed": True},
        {"name": "user",                "type": "address", "indexed": True},
        {"name": "debtToCover",         "type": "uint256", "indexed": False},
        {"name": "liquidatedCollateralAmount", "type": "uint256", "indexed": False},
        {"name": "liquidator",          "type": "address", "indexed": False},
        {"name": "receiveAToken",       "type": "bool",    "indexed": False},
    ]
}]


def fetch_recent_liquidations(pool_addr: str, blocks: int = 5000) -> List[dict]:
    """
    Fetch recent LiquidationCall events from an Aave-compatible pool.
    Returns list of liquidation records.
    """
    w3           = get_web3()
    current      = w3.eth.block_number
    from_block   = max(0, current - blocks)
    chunk        = cfg("scanning", "event_scan_chunk")
    a2s          = get_address_to_symbol()

    pool = w3.eth.contract(
        address=checksum(pool_addr),
        abi=LIQUIDATION_CALL_ABI
    )

    results = []
    for start in range(from_block, current, chunk):
        end = min(start + chunk - 1, current)
        try:
            events = pool.events.LiquidationCall.get_logs(
                fromBlock=start, toBlock=end
            )
            for e in events:
                args = e["args"]
                col_sym  = a2s.get(args["collateralAsset"].lower(), args["collateralAsset"][:8])
                debt_sym = a2s.get(args["debtAsset"].lower(), args["debtAsset"][:8])

                # Get tx receipt to find gas paid
                try:
                    receipt = w3.eth.get_transaction_receipt(e["transactionHash"])
                    tx      = w3.eth.get_transaction(e["transactionHash"])
                    gas_price_gwei = tx["maxFeePerGas"] / 1e9 if "maxFeePerGas" in tx else tx.get("gasPrice", 0) / 1e9
                    gas_used = receipt["gasUsed"]
                except Exception:
                    gas_price_gwei = 0
                    gas_used       = 0

                results.append({
                    "block":           e["blockNumber"],
                    "tx":              e["transactionHash"].hex(),
                    "liquidator":      args["liquidator"],
                    "borrower":        args["user"],
                    "collateral":      col_sym,
                    "debt":            debt_sym,
                    "debt_covered":    args["debtToCover"],
                    "gas_price_gwei":  round(gas_price_gwei, 4),
                    "gas_used":        gas_used,
                })
        except Exception as ex:
            logger.debug(f"Event fetch error {start}-{end}: {ex}")

    return sorted(results, key=lambda x: x["block"], reverse=True)


def analyze_competitors(liquidations: List[dict]) -> dict:
    """
    Aggregate liquidation data into competitor profiles.
    """
    bots = defaultdict(lambda: {
        "liquidations":     0,
        "total_gas_used":   0,
        "avg_gas_gwei":     0.0,
        "collateral_types": defaultdict(int),
        "debt_types":       defaultdict(int),
        "gas_readings":     [],
    })

    for liq in liquidations:
        addr = liq["liquidator"]
        b    = bots[addr]
        b["liquidations"]    += 1
        b["total_gas_used"]  += liq["gas_used"]
        b["collateral_types"][liq["collateral"]] += 1
        b["debt_types"][liq["debt"]]             += 1
        if liq["gas_price_gwei"] > 0:
            b["gas_readings"].append(liq["gas_price_gwei"])

    # Compute averages
    for addr, b in bots.items():
        if b["gas_readings"]:
            b["avg_gas_gwei"] = round(sum(b["gas_readings"]) / len(b["gas_readings"]), 4)
            b["min_gas_gwei"] = round(min(b["gas_readings"]), 4)
        b.pop("gas_readings", None)
        # Convert defaultdicts to regular dicts for display
        b["collateral_types"] = dict(b["collateral_types"])
        b["debt_types"]       = dict(b["debt_types"])

    return dict(bots)


def report(blocks: int = 10000):
    """Print a full competitor analysis report."""
    protocols = cfg("protocols")
    all_liquidations = []

    for name, pcfg in protocols.items():
        if not pcfg.get("enabled"):
            continue
        pool = pcfg.get("pool", "")
        if not pool or pool.startswith("0x000"):
            continue
        logger.info(f"Fetching liquidations from {name} (last {blocks} blocks)...")
        liq = fetch_recent_liquidations(pool, blocks=blocks)
        for l in liq:
            l["protocol"] = name
        all_liquidations.extend(liq)

    if not all_liquidations:
        print("No liquidations found in the scanned range.")
        return

    bots = analyze_competitors(all_liquidations)

    print(f"\n{'═'*65}")
    print(f"  Competitor Analysis — {len(all_liquidations)} liquidations across {len(bots)} bots")
    print(f"{'═'*65}")

    sorted_bots = sorted(bots.items(), key=lambda x: x[1]["liquidations"], reverse=True)

    for rank, (addr, data) in enumerate(sorted_bots[:10], 1):
        top_col  = max(data["collateral_types"], key=data["collateral_types"].get, default="?")
        top_debt = max(data["debt_types"],       key=data["debt_types"].get,       default="?")
        print(f"\n  #{rank} {addr[:14]}...")
        print(f"     Liquidations:    {data['liquidations']}")
        print(f"     Avg gas price:   {data['avg_gas_gwei']} gwei")
        print(f"     Min gas seen:    {data.get('min_gas_gwei', '?')} gwei")
        print(f"     Fav collateral:  {top_col}")
        print(f"     Fav debt token:  {top_debt}")

    # Summary stats
    avg_gas = sum(b["avg_gas_gwei"] for b in bots.values() if b["avg_gas_gwei"] > 0)
    avg_gas /= max(len([b for b in bots.values() if b["avg_gas_gwei"] > 0]), 1)

    print(f"\n{'─'*65}")
    print(f"  Market avg gas: {avg_gas:.4f} gwei")
    print(f"  Your config max: {cfg('gas','max_fee_per_gas_gwei')} gwei")

    if cfg("gas", "max_fee_per_gas_gwei") >= avg_gas:
        print(f"  ✅ Your gas cap is competitive")
    else:
        print(f"  ⚠️  Your gas cap may be too low — consider raising it")

    # Identify skipped positions (small collateral types)
    all_col = defaultdict(int)
    for b in bots.values():
        for sym, count in b["collateral_types"].items():
            all_col[sym] += count

    print(f"\n  Collateral distribution (all bots):")
    for sym, count in sorted(all_col.items(), key=lambda x: -x[1]):
        pct = count / len(all_liquidations) * 100
        print(f"    {sym:<10} {count:>5} ({pct:.1f}%)")

    print(f"\n  💡 Underserved collateral types (your edge):")
    underserved = [sym for sym, cnt in all_col.items()
                   if cnt < len(all_liquidations) * 0.03]  # less than 3% of liquidations
    if underserved:
        for sym in underserved:
            tokens = cfg("tokens")
            bonus  = tokens.get(sym, {}).get("liquidation_bonus", "?")
            if isinstance(bonus, float):
                print(f"    {sym:<10} bonus={bonus*100:.0f}%  ← low competition")
    else:
        print("    (all collateral types are well-covered by existing bots)")

    print(f"\n{'═'*65}\n")
    return bots
