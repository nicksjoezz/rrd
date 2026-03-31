import requests
import json
url = "https://arb1.arbitrum.io/rpc"
# Aave V3 Pool
pool = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"

# We want whale addresses or users with debt.
# Let's try some known large addresses or just common ones.
# Actually, let's use the scan results we saw earlier in the logs.
# 0x496b0Da20E553cC4B1879D54e57283b8C9fDfbB4 was seen with $253k debt.

whale = "0x496b0Da20E553cC4B1879D54e57283b8C9fDfbB4"

ABI = [{"name":"getUserAccountData","type":"function","inputs":[{"name":"user","type":"address"}],"outputs":[{"name":"totalCollateralBase","type":"uint256"},{"name":"totalDebtBase","type":"uint256"},{"name":"availableBorrowsBase","type":"uint256"},{"name":"currentLiquidationThreshold","type":"uint256"},{"name":"ltv","type":"uint256"},{"name":"healthFactor","type":"uint256"}],"stateMutability":"view"}]

from web3 import Web3
w3 = Web3(Web3.HTTPProvider(url))
contract = w3.eth.contract(address=Web3.to_checksum_address(pool), abi=ABI)

data = contract.functions.getUserAccountData(Web3.to_checksum_address(whale)).call()
print(f"Whale {whale}: HF = {data[5]/1e18}")
