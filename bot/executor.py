import logging
import time
from typing import Optional
from web3 import Web3
from .utils import (
    get_web3, get_account, cfg, checksum,
    logger, notify
)
from .database import record_execution
from .arb_math import calculate_optimal_input

ARB_CONTRACT_ABI = [
  {"name":"executeArb","type":"function","stateMutability":"nonpayable",
   "inputs":[
     {"name":"flashPool","type":"address"},
     {"name":"tokenX","type":"address"},
     {"name":"tokenUSDC","type":"address"},
     {"name":"amountUSDC","type":"uint256"},
     {"name":"uniV3Fee","type":"uint24"},
     {"name":"minProfit","type":"uint256"},
     {"name":"isCamelotV3","type":"bool"}
   ],"outputs":[]},
  {"name":"withdraw","type":"function","stateMutability":"nonpayable",
   "inputs":[{"name":"token","type":"address"}],"outputs":[]},
  {"name":"owner","type":"function","stateMutability":"view",
   "inputs":[],"outputs":[{"type":"address"}]}
]

def get_mode() -> str:
    """Returns 'simulate' or 'live'. Reads live from config.json."""
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

        if self._account:
            acc_str = f"{self._account.address[:10]}..."
        else:
            acc_str = "NOT_SET"
            logger.warning("private_key not set -- live execution and simulation disabled")

        logger.info(
            f"ArbExecutor initialized | Contract: {contract_addr[:10] if self._contract else 'None'} | "
            f"Wallet: {acc_str}"
        )

    def _build_tx(self, opportunity: dict, amount_usdc: int, min_profit: int, is_c_v3: bool) -> dict:
        """Build the arbitrage transaction."""
        flash_pool = opportunity["univ3Pool"]
        token_x = opportunity["token"]
        token_usdc = cfg("tokens", "USDC", "address")
        uni_v3_fee = 500 # Assume 0.05% for now

        nonce = self._w3.eth.get_transaction_count(self._account.address)

        return self._contract.functions.executeArb(
            checksum(flash_pool),
            checksum(token_x),
            checksum(token_usdc),
            amount_usdc,
            uni_v3_fee,
            min_profit,
            is_c_v3
        ).build_transaction({
            "from":    self._account.address,
            "nonce":   nonce,
            "chainId": cfg("network", "chain_id"),
            "gas":     cfg("gas", "gas_limit"),
            "maxFeePerGas": self._w3.to_wei(cfg("gas", "max_fee_per_gas_gwei"), "gwei"),
            "maxPriorityFeePerGas": self._w3.to_wei(cfg("gas", "max_priority_fee_gwei"), "gwei"),
        })

    async def execute(self, opportunity: dict):
        """Execute or simulate the arbitrage transaction."""
        mode = get_mode()
        token_usdc_addr = cfg("tokens", "USDC", "address")
        token_usdc_decimals = cfg("tokens", "USDC", "decimals")

        # Calculate optimal amount based on liquidity and price gap
        u_liq = opportunity.get("u_liq", 1000)
        c_liq = opportunity.get("c_liq", 1000)
        p_u = opportunity["u_price"]
        p_c = opportunity["c_price"]

        amount_usd = calculate_optimal_input(u_liq, c_liq, p_u, p_c)
        amount_usdc_wei = int(amount_usd * (10 ** token_usdc_decimals))
        min_profit_usd = cfg("strategy", "min_profit_usd")
        min_profit_wei = int(min_profit_usd * (10 ** token_usdc_decimals))

        is_c_v3 = opportunity.get("isCamelotV3", False)
        if not self._contract or not self._account:
            logger.info(f"[{mode.upper()}] Arbitrage simulation (Dry Run) | Token: {opportunity['symbol']} | CamV3: {is_c_v3}")
            return None

        tx = self._build_tx(opportunity, amount_usdc_wei, min_profit_wei, is_c_v3)

        try:
            if mode == "simulate":
                self._w3.eth.call(tx)
                logger.info(f"[SIMULATE] [SUCCESS] Arbitrage simulation passed for {opportunity['symbol']}!")

                record_execution({
                    "tx_hash": f"sim-{int(time.time())}-{opportunity['symbol']}",
                    "token": opportunity["token"],
                    "symbol": opportunity["symbol"],
                    "univ3Pool": opportunity["univ3Pool"],
                    "camelotPool": opportunity["camelotPool"],
                    "estimated_profit": min_profit_usd,
                    "timestamp": int(time.time())
                })
                return "sim-success"
            else:
                signed = self._account.sign_transaction(tx)
                tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
                logger.info(f"[LIVE] Arbitrage TX Sent: {tx_hash.hex()}")
                receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
                if receipt.status == 1:
                    logger.info(f"[LIVE] SUCCESS! Arbitrage completed. TX: {tx_hash.hex()}")
                    record_execution({
                        "tx_hash": tx_hash.hex(),
                        "token": opportunity["token"],
                        "symbol": opportunity["symbol"],
                        "univ3Pool": opportunity["univ3Pool"],
                        "camelotPool": opportunity["camelotPool"],
                        "estimated_profit": min_profit_usd,
                        "timestamp": int(time.time())
                    })
                    notify(f"Arbitrage SUCCESS! Token: {opportunity['symbol']}\nTX: {tx_hash.hex()}")
                    return tx_hash.hex()
                else:
                    logger.error(f"[LIVE] FAILED! Arbitrage reverted. TX: {tx_hash.hex()}")
                    return None
        except Exception as e:
            logger.info(f"[{mode.upper()}] [REVERTED] Arbitrage failed: {e}")
            return None
