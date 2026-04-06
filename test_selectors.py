from bot.utils import get_web3, checksum
import asyncio

async def test_selectors():
    w3 = get_web3()
    # GRAIL V3 Pool
    target = checksum("0x8cc8093218bCaC8B1896A1EED4D925F6F6aB289F")

    selectors = {
        "slot0": "0x3850c7bd", # UniV3 / Algebra globalState
        "getReserves": "0x0902f1ac",
        "globalState": "0x3850c7bd",
        "state": "0x1ad14a67"
    }

    for name, selector in selectors.items():
        try:
            res = w3.eth.call({"to": target, "data": selector})
            print(f"{name} ({selector}): SUCCESS")
        except Exception as e:
            print(f"{name} ({selector}): FAILED - {e}")

if __name__ == "__main__":
    asyncio.run(test_selectors())
