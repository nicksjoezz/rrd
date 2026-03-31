import json
from web3 import Web3

with open('config.json') as f:
    config = json.load(f)

rpc = config['network']['rpc_http']
w3 = Web3(Web3.HTTPProvider(rpc))

pool = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"
topic = "0xb3d084820fb1a9decffb176436bd02558d15fac9b0ddfed8c465bc7359d7dce0"

current = w3.eth.block_number
start = current - 500

print(f"Scanning {start} to {current}...")

logs = w3.eth.get_logs({
    "address": Web3.to_checksum_address(pool),
    "topics": [topic],
    "fromBlock": start,
    "toBlock": current
})

print(f"Found {len(logs)} logs")
for log in logs[:3]:
    print(f"  Log: {log['topics'][2].hex()[-40:]}")
