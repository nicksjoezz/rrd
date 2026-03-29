"""
scan_test.py -- Standalone terminal tool to verify scanning logic and price discovery.
Usage:
    python scan_test.py
    python scan_test.py --protocol aave_v3
    python scan_test.py --limit 10
"""

import sys
import argparse
import logging
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from bot.utils import load_config, get_web3, logger
from bot.monitor import MultiProtocolMonitor
from bot.profitability import get_token_price_usd

def main():
    parser = argparse.ArgumentParser(description="LiqBot Scan Test Tool")
    parser.add_argument("--protocol", type=str, help="Specific protocol to scan")
    parser.add_argument("--limit", type=int, default=100, help="Limit number of borrowers to check")
    parser.add_argument("--discovery", action="store_true", help="Run full event discovery scan (slow)")
    args = parser.parse_args()

    # Set logger to INFO to see our new price logs
    logging.getLogger("liquidation_bot").setLevel(logging.INFO)

    logger.info("Starting Scan Test...")

    # Check ETH price first to verify price discovery logging
    weth = load_config()["network"]["weth"]
    logger.info("Testing ETH Price Discovery:")
    eth_price = get_token_price_usd(weth)
    logger.info(f"Final ETH Price: ${eth_price:,.2f}")

    monitor = MultiProtocolMonitor()

    if args.discovery:
        logger.info("Running borrower discovery from events (last 1000 blocks)...")
        w3 = get_web3()
        current_block = w3.eth.block_number
        from_block = current_block - 1000
        for name, m in monitor.monitors.items():
            m.load_borrowers_from_events(from_block, current_block)
    else:
        logger.info("Loading borrowers from DB cache...")
        for name, m in monitor.monitors.items():
            m.load_borrowers_from_db()

    total_checked = 0
    total_found = 0

    for name, m in monitor.monitors.items():
        if args.protocol and name != args.protocol:
            continue

        borrowers = list(m._borrowers)[:args.limit]
        logger.info(f"Scanning {len(borrowers)} borrowers on {name}...")

        # Scan users directly
        found = m.scan_users(borrowers, zombie_queue=monitor.zombie_queue)

        total_checked += len(borrowers)
        total_found += len(found)

        for pos in found:
            hf = pos['health_factor']
            status = "CRITICAL" if hf < 1.0 else "ZOMBIE" if hf < 1.05 else "WATCHING"
            logger.info(
                f"[{status}] {pos['user']} | HF: {hf:.4f} | "
                f"Debt: {pos['debt_symbol']} (${pos['total_debt_usd']:.2f}) | "
                f"Col: {pos['collateral_symbol']} (${pos['total_col_usd']:.2f})"
            )

    logger.info(f"Scan complete. Checked {total_checked} borrowers, found {total_found} at-risk positions.")

if __name__ == "__main__":
    main()
