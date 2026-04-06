import asyncio
from bot.arb_monitor import ArbMonitor
from bot.utils import logger

async def test_monitor():
    monitor = ArbMonitor()
    print(f"Watchlist size: {len(monitor.watchlist)}")
    for item in monitor.watchlist:
        print(f" - {item['symbol']}: Camelot={item['camelotPool']}, UniV3={item['univ3Pool']}")

    print("\nChecking for opportunities...")
    opps = monitor.check_all_prices_multicall()
    print(f"Found {len(opps)} opportunities.")
    for opp in opps:
        print(f" - {opp['symbol']}: Gap={opp['gap']:.2%}, U={opp['u_price']:.4f}, C={opp['c_price']:.4f}")

if __name__ == "__main__":
    asyncio.run(test_monitor())
