"""
liquidator.py — Flash loan liquidation execution.
"""

import logging
import time
from typing import Optional
from web3 import Web3

from .utils import (
    get_web3, get_account, cfg, checksum, get_swap_fee,
    LIQUIDATOR_CONTRACT_ABI, notify, get_token_map
)
from .profitability import estimate_profit_usd
from .database import record_liquidation
from .gas_manager import get_gas_params, is_gas_spike

logger = logging.getLogger("liquidation_bot.liquidator")

LIQUIDATION_TOPIC = Web3.keccak(
    text="LiquidationCall(address,address,address,uint256,uint256,address,bool)"
).hex()

PROTOCOL_IDS = {
    "aave_v3": 0,
    "radiant": 0,
    "compound": 1,
    "silo": 2,
    "morpho": 3
}

def get_mode() -> str:
    try:
        import bot.utils as _u
        return (_u.load_config() or {}).get("mode", "simulate")
    except Exception:
        return "simulate"

class LiquidationExecutor:
    def __init__(self):
        self._w3      = get_web3()
        self._account = get_account()
        addr = cfg("wallet", "liquidator_contract")
        self._contract = self._w3.eth.contract(address=checksum(addr), abi=LIQUIDATOR_CONTRACT_ABI) if addr and addr != "DEPLOY_CONTRACT_ADDRESS_HERE" else None

    def _build_tx(self, position: dict, gas_params: dict) -> dict:
        col_token = position["collateral_token"]
        debt_token = position["debt_token"]
        debt_amount = position["debt_to_cover"]
        pool_address = position["pool_address"]
        profit_info = position.get("profit_info", {})
        nonce = self._w3.eth.get_transaction_count(self._account.address)

        proto_id = PROTOCOL_IDS.get(position.get("protocol", "").lower(), 0)
        swap_params = position.get("swap_params", {})
        est_profit_usd = profit_info.get("estimated_profit_usd", 0)
        debt_price = position.get("debt_price", 1.0)
        min_profit_wei = 0
        if est_profit_usd > 0:
            target = est_profit_usd * 0.5
            token_map = get_token_map()
            debt_info = token_map.get(debt_token.lower(), {"decimals": 18})
            min_profit_wei = int((target / debt_price) * (10 ** debt_info["decimals"]))

        path = swap_params.get("path", b"")
        if isinstance(path, str):
            if path.startswith("0x"): path = bytes.fromhex(path[2:])
            elif path.startswith("hex:"): path = bytes.fromhex(path[4:])
            else:
                try: path = bytes.fromhex(path)
                except: pass

        if swap_params.get("route") == "multi" and path:
            return self._contract.functions.executeLiquidationMultiHop(
                checksum(debt_token), checksum(col_token), checksum(position["user"]),
                debt_amount, checksum(pool_address), path, min_profit_wei, proto_id
            ).build_transaction({
                "from": self._account.address, "nonce": nonce,
                "chainId": cfg("network", "chain_id"), **gas_params,
            })
        else:
            fee = get_swap_fee(col_token, debt_token)
            return self._contract.functions.executeLiquidation(
                checksum(debt_token), checksum(col_token), checksum(position["user"]),
                debt_amount, checksum(pool_address), fee, min_profit_wei, proto_id
            ).build_transaction({
                "from": self._account.address, "nonce": nonce,
                "chainId": cfg("network", "chain_id"), **gas_params,
            })

    def _check_rival_liquidation(self, user: str, blocks: int = 50) -> Optional[dict]:
        try:
            current = self._w3.eth.block_number
            logs = self._w3.eth.get_logs({
                "fromBlock": max(0, current - blocks),
                "toBlock":   "latest",
                "topics": [LIQUIDATION_TOPIC, None, None, "0x" + user.lower()[2:].zfill(64)]
            })
            if logs: return {"block": logs[0]["blockNumber"], "tx_hash": logs[0]["transactionHash"].hex()}
        except: pass
        return None

    def execute(self, position: dict) -> Optional[str]:
        mode = get_mode(); protocol = position.get("protocol", "unknown"); user = position["user"]
        profit_info = estimate_profit_usd(position)
        if not profit_info["profitable"]: return None
        if mode == "live" and is_gas_spike(): return None

        logger.info(f"[{protocol}] [{mode.upper()}] {user[:10]}... HF={position.get('health_factor',0):.4f} Profit=${profit_info['estimated_profit_usd']:.2f}")

        try:
            if not self._account or not self._contract: return None
            gas_params = get_gas_params(profit_info["estimated_profit_usd"])
            tx = self._build_tx(position, gas_params)

            if mode == "simulate":
                try:
                    self._w3.eth.call({"from": self._account.address, "to": self._contract.address, "data": tx["data"], "gas": tx["gas"]})
                    rival = self._check_rival_liquidation(user)
                    if rival: logger.warning(f"[{protocol}] SIMULATE [LOST] Rival bot beat us at block {rival['block']}")
                    else: logger.info(f"[{protocol}] SIMULATE SUCCESS | {user[:8]}")
                    return f"sim-{int(time.time())}-{user[:6]}"
                except Exception as e: logger.info(f"[{protocol}] SIMULATE FAIL: {str(e)[:100]}"); return None
            else:
                signed = self._account.sign_transaction(tx)
                tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
                receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)
                if receipt.status == 1:
                    logger.info(f"[{protocol}] [OK] SUCCESS | TX: {tx_hash.hex()}")
                    record_liquidation(tx_hash.hex(), protocol, user, position["collateral_token"], position["debt_token"], profit_info["debt_to_cover_usd"], profit_info["estimated_profit_usd"], receipt.gasUsed, receipt.blockNumber)
                    return tx_hash.hex()
                return None
        except Exception as e: logger.error(f"[{protocol}] Execution error: {e}"); return None

    def execute_batch(self, positions: list) -> list:
        results = []
        for pos in positions:
            tx = self.execute(pos);
            if tx: results.append(tx)
            time.sleep(1)
        return results

    def withdraw_profits(self, token_address: str) -> Optional[str]:
        if not self._account or not self._contract: return None
        try:
            gas_cfg = cfg("gas"); nonce = self._w3.eth.get_transaction_count(self._account.address)
            tx = self._contract.functions.withdraw(checksum(token_address)).build_transaction({
                "from": self._account.address, "nonce": nonce, "gas": 120_000,
                "maxFeePerGas": self._w3.to_wei(gas_cfg["max_fee_per_gas_gwei"], "gwei"),
                "maxPriorityFeePerGas": self._w3.to_wei(gas_cfg["max_priority_fee_gwei"], "gwei"),
                "chainId": cfg("network", "chain_id"),
            })
            signed = self._account.sign_transaction(tx); tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            if receipt.status == 1: logger.info(f"Withdrew profits for {token_address}"); return tx_hash.hex()
        except Exception as e: logger.error(f"Withdraw error: {e}")
        return None
