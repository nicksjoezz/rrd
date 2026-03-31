import json
import time
import requests
from web3 import Web3
from concurrent.futures import ThreadPoolExecutor

RPC_URL = "https://arb1.arbitrum.io/rpc"
POOL_ADDR = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"
LIQ_TOPIC = "0xe413a321e8681d831f4dbccbca790d2952b56f977908e45be37335533e005286"

# Common token addresses on Arbitrum
TOKENS = {
    "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1": "WETH",
    "0xaf88d065e77c8cC2239327C5EDb3A432268e5831": "USDC",
    "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8": "USDC.e",
    "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9": "USDT",
    "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f": "WBTC",
    "0x912CE59144191C1204E64559FE8253a0e49E6548": "ARB",
    "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1": "DAI",
    "0xf97f4df75117a78c1A5a0DBb814Af92458539FB4": "LINK",
    "0x5979D7b546E38E414F7E9822514be443A4800529": "wstETH"
}

FLASH_TOPICS = {
    "0x0ec96ca0bc59f3c5f510860533f868955f190696956276b052069ec652b12368": "Balancer",
    "0xef2ed95ab352496738917e7edc4600e12e1a3b5c4046d03f0b2f6385a5a6a68f": "Aave V3",
    "0xb9109003509176378e9f5636087b3261623f9547d7c67423377ed38c1143807d": "Uniswap V3 Flash",
}

def get_token_name(addr):
    addr = Web3.to_checksum_address(addr)
    return TOKENS.get(addr, addr[:10])

def decode_liq_log(log):
    collateral = "0x" + log['topics'][1][-40:]
    debt = "0x" + log['topics'][2][-40:]
    user = "0x" + log['topics'][3][-40:]
    data = log['data'].replace('0x', '')
    debt_to_cover = int(data[0:64], 16)
    collateral_amount = int(data[64:128], 16)
    liquidator = "0x" + data[128+24:192]
    return {
        "collateral": Web3.to_checksum_address(collateral),
        "debt": Web3.to_checksum_address(debt),
        "user": Web3.to_checksum_address(user),
        "debt_to_cover": debt_to_cover,
        "collateral_amount": collateral_amount,
        "liquidator": Web3.to_checksum_address(liquidator),
        "tx_hash": log['transactionHash']
    }

def get_logs_large_chunk(start, end):
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "eth_getLogs",
        "params": [{
            "address": POOL_ADDR,
            "fromBlock": hex(start), "toBlock": hex(end),
            "topics": [LIQ_TOPIC]
        }]
    }
    try:
        r = requests.post(RPC_URL, json=payload, timeout=30)
        return r.json().get('result', [])
    except: return []

def main():
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    with open("competitors.json", "r") as f:
        comps = json.load(f)

    top_5_addrs = [c['address'].lower() for c in comps[:5]]
    print(f"Analyzing Top 5 Competitors: {top_5_addrs}")

    latest = w3.eth.block_number
    lookback = 5000000 # ~15 days
    start_block = latest - lookback

    print(f"Scanning {lookback:,} blocks...")
    chunk_size = 250000
    chunks = [(i, min(i+chunk_size-1, latest)) for i in range(start_block, latest, chunk_size)]

    all_logs = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        for res in executor.map(lambda x: get_logs_large_chunk(x[0], x[1]), chunks):
            if isinstance(res, list): all_logs.extend(res)

    print(f"Found {len(all_logs)} total liquidations. Filtering for top 5...")

    analysis = {addr: {"count": 0, "gas_spent": 0, "targets": {}, "flash_loans": {}, "architectures": {}} for addr in top_5_addrs}

    def process_log(log):
        tx_hash = log['transactionHash']
        try:
            tx = w3.eth.get_transaction(tx_hash)
            receipt = w3.eth.get_transaction_receipt(tx_hash)

            sender = tx['from'].lower()
            to = (tx['to'] or "").lower()

            match = None
            if sender in top_5_addrs: match = sender
            elif to in top_5_addrs: match = to

            if match:
                decoded = decode_liq_log(log)
                gas_used = receipt['gasUsed']
                gas_price = receipt.get('effectiveGasPrice', tx.get('gasPrice', 0))
                fee_eth = (gas_used * gas_price) / 10**18

                flash_type = "None"
                for l in receipt.get('logs', []):
                    top0 = l['topics'][0].lower()
                    if top0 in FLASH_TOPICS:
                        flash_type = FLASH_TOPICS[top0]
                        break

                # Check if to is a contract
                code = w3.eth.get_code(Web3.to_checksum_address(match))
                arch = "Contract" if code and code != b'\x00' else "EOA"

                target_pair = f"{get_token_name(decoded['collateral'])} / {get_token_name(decoded['debt'])}"
                return {"match": match, "fee": fee_eth, "target": target_pair, "flash": flash_type, "arch": arch}
        except: pass
        return None

    with ThreadPoolExecutor(max_workers=10) as executor:
        for res in executor.map(process_log, all_logs):
            if res:
                m = res['match']
                analysis[m]["count"] += 1
                analysis[m]["gas_spent"] += res['fee']
                analysis[m]["targets"][res['target']] = analysis[m]["targets"].get(res['target'], 0) + 1
                analysis[m]["flash_loans"][res['flash']] = analysis[m]["flash_loans"].get(res['flash'], 0) + 1
                analysis[m]["architectures"][res['arch']] = analysis[m]["architectures"].get(res['arch'], 0) + 1

    print("\n" + "="*60)
    print("COMPETITOR DEEP ANALYSIS (15 DAYS)")
    print("="*60)
    for addr in top_5_addrs:
        data = analysis[addr]
        if data['count'] == 0: continue
        avg_gas = data['gas_spent'] / data['count']
        print(f"\nBot: {addr}")
        print(f"  Scanned Liquidations: {data['count']}")
        print(f"  Architecture: {', '.join([f'{k} ({v})' for k,v in data['architectures'].items()])}")
        print(f"  Total Gas Spent: {data['gas_spent']:.6f} ETH")
        print(f"  Avg Gas per Liq: {avg_gas:.6f} ETH")
        print(f"  Flash Loans Used:")
        for ft, fc in data['flash_loans'].items():
            print(f"    - {ft}: {fc} times")
        print(f"  Top Targets:")
        sorted_targets = sorted(data['targets'].items(), key=lambda x: x[1], reverse=True)
        for target, count in sorted_targets[:3]:
            print(f"    - {target}: {count} times")

if __name__ == "__main__":
    main()
