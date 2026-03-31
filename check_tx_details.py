from web3 import Web3
import requests

RPC_URL = "https://arb1.arbitrum.io/rpc"
TX_HASH = "0x39e3ba94bf8731933e7ab07cfe92de8338e19524701ae972c92204b1f58301df"

def main():
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    tx = w3.eth.get_transaction(TX_HASH)
    print(f"TX: {TX_HASH}")
    print(f"From: {tx['from']}")
    print(f"To: {tx['to']}")

if __name__ == "__main__":
    main()
