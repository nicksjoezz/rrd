import json
from web3 import Web3

with open('config.json') as f:
    config = json.load(f)

rpc = config['network']['rpc_http']
w3 = Web3(Web3.HTTPProvider(rpc))

# Aave V3 Price Oracle on Arbitrum
# WETH: 0x82aF49447D8a07e3bd95BD0d56f35241523fBab1
# LINK: 0xf97f4df75117a78c1a5a0DBb814Af92458539FB4
LINK = "0xf97f4df75117a78c1a5a0DBb814Af92458539FB4"
ORACLE = "0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb" # Addresses Provider

ABI_PROVIDER = [{"inputs":[],"name":"getPriceOracle","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"}]
ABI_ORACLE = [{"inputs":[{"internalType":"address","name":"asset","type":"address"}],"name":"getSourceOfAsset","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"}]

try:
    provider = w3.eth.contract(address=Web3.to_checksum_address(ORACLE), abi=ABI_PROVIDER)
    oracle_addr = provider.functions.getPriceOracle().call()
    print(f"Price Oracle: {oracle_addr}")

    oracle = w3.eth.contract(address=Web3.to_checksum_address(oracle_addr), abi=ABI_ORACLE)
    source = oracle.functions.getSourceOfAsset(Web3.to_checksum_address(LINK)).call()
    print(f"Source for LINK: {source}")
except Exception as e:
    print(f"Error: {e}")
