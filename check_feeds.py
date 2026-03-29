import json
from web3 import Web3

with open('config.json') as f:
    config = json.load(f)

rpc = config['network']['rpc_http']
w3 = Web3(Web3.HTTPProvider(rpc))

feeds = config['oracle']['chainlink_feeds']
ABI = [{"inputs":[],"name":"latestRoundData","outputs":[{"internalType":"uint80","name":"roundId","type":"uint80"},{"internalType":"int256","name":"answer","type":"int256"},{"internalType":"uint256","name":"startedAt","type":"uint256"},{"internalType":"uint256","name":"updatedAt","type":"uint256"},{"internalType":"uint80","name":"answeredInRound","type":"uint80"}],"stateMutability":"view","type":"function"}]

for name, addr in feeds.items():
    print(f"Checking {name} at {addr}...")
    try:
        contract = w3.eth.contract(address=Web3.to_checksum_address(addr), abi=ABI)
        data = contract.functions.latestRoundData().call()
        print(f"  OK: Price = {data[1]/1e8}")
    except Exception as e:
        print(f"  FAIL: {e}")
