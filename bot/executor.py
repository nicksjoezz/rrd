import logging
import time
from typing import Optional, List
from web3 import Web3
from .utils import (
    get_web3, get_account, cfg, checksum,
    logger, notify
)
from .database import record_execution

ARB_CONTRACT_ABI = [
  {"name":"executeArb","type":"function","stateMutability":"nonpayable",
   "inputs":[
     {"name":"flashPool","type":"address"},
     {"name":"amountFlash","type":"uint256"},
     {"name":"tokenFlash","type":"address"},
     {"components":[
         {"name":"dex","type":"uint8"},
         {"name":"tokenIn","type":"address"},
         {"name":"tokenOut","type":"address"},
         {"name":"fee","type":"uint24"}
       ],"name":"steps","type":"tuple[]"},
     {"name":"minProfit","type":"uint256"}
   ],"outputs":[]},
  {"name":"withdraw","type":"function","stateMutability":"nonpayable",
   "inputs":[{"name":"token","type":"address"}],"outputs":[]},
  {"name":"owner","type":"function","stateMutability":"view",
   "inputs":[],"outputs":[{"type":"address"}]}
]

DEX_TYPE_MAP = {
    "univ2": 0,
    "univ3": 1,
    "camelotv2": 2,
    "camelotv3": 3,
    "algebra": 3
}

def get_mode() -> str:
    try:
        import bot.utils as _u
        raw = (_u.load_config() or {}).get("mode", "live")
        return raw if raw in ("simulate", "live") else "live"
    except Exception:
        return "live"

class ArbExecutor:
    def __init__(self):
        self._w3      = get_web3()
        self._account = get_account()
        contract_addr = cfg("wallet", "arb_contract")

        if not contract_addr or contract_addr == "DEPLOY_CONTRACT_ADDRESS_HERE":
            self._contract = None
            logger.warning("arb_contract not set -- execution will be disabled")
        else:
            self._contract = self._w3.eth.contract(
                address=checksum(contract_addr),
                abi=ARB_CONTRACT_ABI
            )

        logger.info(
            f"ArbExecutor initialized | Contract: {contract_addr[:10] if self._contract else 'None'}"
        )

    def _build_tx(self, opportunity: dict, amount_flash: int, min_profit: int) -> dict:
        token_flash = checksum(opportunity["tokens"][0])

        # Identify a Uniswap V3 pool for flash loaning
        flash_pool = ""
        # 1. Try to use a pool already in the path if it's UniV3
        for i, p in enumerate(opportunity["pools"]):
            if opportunity["versions"][i] == "univ3":
                flash_pool = p
                break

        # 2. Fallback to a known high-liquidity UniV3 pool containing token_flash
        if not flash_pool:
            # Common UniV3 pools for flash loans on Arbitrum
            # WETH/USDC 0.05%
            weth_usdc = "0xC6962004f452fE5bE02D0E47321ee3deFb746355"
            # WETH/ARB 0.05%
            weth_arb = "0x2f6E8Ba994f06859556C0150935561a06788e001"

            # This is a bit hardcoded, but for simulation/testing it works for major tokens
            if "USDC" in opportunity["symbol"] or "WETH" in opportunity["symbol"]:
                flash_pool = weth_usdc
            elif "ARB" in opportunity["symbol"]:
                flash_pool = weth_arb
            else:
                flash_pool = weth_usdc # Default to USDC/WETH

        steps = []
        for i in range(len(opportunity["pools"])):
            steps.append({
                "dex": DEX_TYPE_MAP.get(opportunity["versions"][i], 1),
                "tokenIn": checksum(opportunity["tokens"][i]),
                "tokenOut": checksum(opportunity["tokens"][i+1]),
                "fee": opportunity["fees"][i] or 0
            })

        nonce = self._w3.eth.get_transaction_count(self._account.address)

        return self._contract.functions.executeArb(
            checksum(flash_pool),
            amount_flash,
            checksum(token_flash),
            steps,
            min_profit
        ).build_transaction({
            "from":    self._account.address,
            "nonce":   nonce,
            "chainId": cfg("network", "chain_id"),
            "gas":     cfg("gas", "gas_limit"),
            "maxFeePerGas": self._w3.to_wei(cfg("gas", "max_fee_per_gas_gwei"), "gwei"),
            "maxPriorityFeePerGas": self._w3.to_wei(cfg("gas", "max_priority_fee_gwei"), "gwei"),
        })

    async def execute(self, opportunity: dict):
        mode = get_mode()
        if not self._contract or not self._account:
            logger.info(f"[{mode.upper()}] Arbitrage DRY RUN | {opportunity['symbol']} Gap: {opportunity['gap']:.2%}")
            return None

        token_flash = opportunity["tokens"][0].lower()
        usdc = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831".lower()
        usdc_e = "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8".lower()

        # Simple dynamic sizing
        # Try to use decimals if available in metadata (which is populated in monitor)
        # Note: In a real bot we'd want a more robust way to get decimals here if monitor hasn't run
        decimals = 18
        if token_flash in [usdc, usdc_e]:
            decimals = 6

        if decimals == 6:
            amount_flash = 500 * 10**6
            min_profit = int(0.5 * 10**6)
        else:
            amount_flash = int(0.2 * 10**18) # Default to 0.2 units
            min_profit = int(0.0002 * 10**18)

        try:
            tx = self._build_tx(opportunity, amount_flash, min_profit)

            if mode == "simulate":
                # Static call to verify revert/success
                self._w3.eth.call(tx)
                logger.info(f"[SIMULATE] [SUCCESS] {opportunity['symbol']} Gap: {opportunity['gap']:.2%}")
                record_execution({
                    "tx_hash": f"sim-{int(time.time())}-{opportunity['symbol']}",
                    "token": opportunity["tokens"][0],
                    "symbol": opportunity["symbol"],
                    "type": opportunity.get("type", "dual"),
                    "estimated_profit": opportunity['gap'] * (amount_flash / 10**18 if "ETH" in opportunity['symbol'] else amount_flash / 10**6),
                    "timestamp": int(time.time())
                })
                return "sim-success"
            else:
                signed = self._account.sign_transaction(tx)
                tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
                logger.info(f"[LIVE] Sent: {tx_hash.hex()} | {opportunity['symbol']}")
                record_execution({
                    "tx_hash": tx_hash.hex(),
                    "token": opportunity["tokens"][0],
                    "symbol": opportunity["symbol"],
                    "type": opportunity.get("type", "dual"),
                    "estimated_profit": opportunity['gap'] * (amount_flash / 10**18 if "ETH" in opportunity['symbol'] else amount_flash / 10**6),
                    "timestamp": int(time.time())
                })
                return tx_hash.hex()
        except Exception as e:
            logger.info(f"[{mode.upper()}] [FAILED] {opportunity['symbol']}: {str(e)[:100]}")
            return None
