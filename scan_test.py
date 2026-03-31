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
    parser.add_argument("--days", type=float, default=7.0, help="Number of days to scan for discovery")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.getLogger("liquidation_bot").setLevel(log_level)

    logger.info(f"Starting Deep Scan Audit ({args.days} days)...")

    # Check ETH price first
    weth = load_config()["network"]["weth"]
    eth_price = get_token_price_usd(weth)
    logger.info(f"Current ETH Price: ${eth_price:,.2f}")

    monitor = MultiProtocolMonitor()

    if args.discovery:
        w3 = get_web3()
        current_block = w3.eth.block_number
        # Arbitrum: ~0.25s per block -> 4 blocks/sec.
        # 7 days = 7 * 24 * 60 * 60 * 4 = 2,419,200 blocks
        blocks_to_scan = int(args.days * 24 * 3600 * 4)
        from_block = max(0, current_block - blocks_to_scan)

        logger.info(f"Running borrower discovery from events: {from_block:,} -> {current_block:,} ({blocks_to_scan:,} blocks)")

        for name, m in monitor.monitors.items():
            if args.protocol and name != args.protocol: continue
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
            est_hf = pos.get('estimated_hf')
            status = "CRITICAL" if hf < 1.0 or (est_hf and est_hf < 1.0) else "ZOMBIE" if hf < 1.05 or (est_hf and est_hf < 1.05) else "WATCHING"

            hf_str = f"{hf:.4f}"
            if est_hf: hf_str += f" (Est: {est_hf:.4f})"

            logger.info(
                f"[{status}] {pos['user']} | HF: {hf_str} | "
                f"Debt: {pos['debt_symbol']} (${pos['total_debt_usd']:.2f}) | "
                f"Col: {pos['collateral_symbol']} (${pos['total_col_usd']:.2f})"
            )

    logger.info(f"Scan complete. Checked {total_checked} borrowers, found {total_found} at-risk positions.")

if __name__ == "__main__":
    main()
