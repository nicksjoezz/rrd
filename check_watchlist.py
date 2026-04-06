import asyncio
import json
from bot.utils import get_web3, checksum, ROOT_DIR

WATCHLIST_PATH = ROOT_DIR / "logs" / "watchlist.json"

async def check_watchlist_pools():
    w3 = get_web3()
    if not WATCHLIST_PATH.exists():
        print("No watchlist found")
        return

    with open(WATCHLIST_PATH, "r") as f:
        watchlist = json.load(f)

    for item in watchlist:
        symbol = item['symbol']
        c_addr = checksum(item['camelotPool'])
        u_addr = checksum(item['univ3Pool'])

        print(f"Checking {symbol}:")
        # Check Camelot
        try:
            w3.eth.call({'to': c_addr, 'data': '0x0902f1ac'}) # getReserves
            print(f"  Camelot {c_addr} is V2")
        except:
            try:
                w3.eth.call({'to': c_addr, 'data': '0x3850c7bd'}) # globalState (Algebra/CamelotV3)
                print(f"  Camelot {c_addr} is V3 (Algebra)")
            except:
                print(f"  Camelot {c_addr} is Unknown")

        # Check Uni
        try:
            w3.eth.call({'to': u_addr, 'data': '0x3850c7bd'}) # slot0/globalState
            print(f"  UniV3 {u_addr} is V3")
        except:
            print(f"  UniV3 {u_addr} is Unknown")

if __name__ == "__main__":
    asyncio.run(check_watchlist_pools())
