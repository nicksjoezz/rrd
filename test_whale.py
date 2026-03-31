import sys
import logging
from pathlib import Path
from web3 import Web3

# Add project root to path
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from bot.utils import load_config, get_web3, logger, checksum
from bot.monitor import MultiProtocolMonitor

def main():
    # Setup logging to see our price logs
    logging.getLogger("liquidation_bot").setLevel(logging.INFO)

    whale = "0x496b0Da20E553cC4B1879D54e57283b8C9fDfbB4"

    monitor = MultiProtocolMonitor()
    aave = monitor.monitors["aave_v3"]

    print(f"\n--- Checking Whale {whale} ---")
    pos = aave.check_position(whale)

    if pos:
        print("\nPosition found:")
        print(f"  Protocol: {pos['protocol']}")
        print(f"  HF: {pos['health_factor']:.6f}")
        if pos.get('estimated_hf'):
            print(f"  Est HF: {pos['estimated_hf']:.6f}")
        print(f"  Debt: ${pos['total_debt_usd']:,.2f} ({pos['debt_symbol']})")
        print(f"  Collateral: ${pos['total_col_usd']:,.2f} ({pos['collateral_symbol']})")

        # Check profitability
        from bot.profitability import estimate_profit_usd
        profit = estimate_profit_usd(pos, force_fresh=True)
        print("\nProfitability Audit:")
        for k, v in profit.items():
            print(f"  {k}: {v}")
    else:
        print("Whale position not found or too healthy (> 1.15 HF)")

if __name__ == "__main__":
    main()
