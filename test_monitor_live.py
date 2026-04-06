import asyncio
import json
import os
import sys
from web3 import Web3

# Add current dir to path
sys.path.append(os.getcwd())

from bot.monitor import ArbMonitor

async def test_monitor():
    monitor = ArbMonitor()
    print("Watchlist items:", len(monitor.watchlist))
    for item in monitor.watchlist:
        print(f"Token: {item['symbol']}")
        print(f"  UniV3: {item['univ3Pool']}")
        print(f"  Camelot: {item['camelotPool']}")

    print("\nTesting Price Discovery (Multicall3)...")
    opps = monitor.check_all_prices_multicall()
    print(f"Opportunities found: {len(opps)}")
    for o in opps:
        print(f"Token: {o['symbol']} | Gap: {o['gap']:.2%} | U: ${o['u_price']:.4f} | C: ${o['c_price']:.4f}")

if __name__ == "__main__":
    asyncio.run(test_monitor())
