import asyncio
from bot.utils import get_web3, checksum

async def check_pools():
    w3 = get_web3()
    pools = [
        ("WINR", "0xc35AA1cEc34E02A8acc3E5f79c22BE364823094c"),
        ("SMOL", "0x13bCE500f21C92aED597AaB2247Cf27cA1116e30"),
        ("PNP", "0x13BC35D101B646Cf1F566f95077E67a9f5b301a3"),
        ("AARK", "0xd7e299880cB19c11176913581F8BFeF6cE50cAB6"),
        ("USDe", "0xc23f308CF1bFA7efFFB592920a619F00990F8D74")
    ]
    for name, addr in pools:
        print(f"Checking {name} at {addr}...")
        # Check for getReserves (V2) - 0x0902f1ac
        try:
            w3.eth.call({'to': checksum(addr), 'data': '0x0902f1ac'})
            print(f"  {name} is V2")
        except Exception as e:
            # Check for globalState (V3/Algebra) - 0x9f36f6d0
            try:
                w3.eth.call({'to': checksum(addr), 'data': '0x9f36f6d0'})
                print(f"  {name} is V3")
            except:
                print(f"  {name} is Unknown (Error: {e})")

if __name__ == "__main__":
    asyncio.run(check_pools())
