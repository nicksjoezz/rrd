"""
liquidator.py — Flash loan liquidation execution.

Respects config.json → mode:
  "simulate"  — runs eth_call to validate the tx, logs the result, but
                NEVER submits to the network. Zero risk, zero gas.
  "live"      — submits real transactions via Balancer flash loan.

Switch mode any time via the Settings page (no restart needed — the bot
re-reads config.json before every execution attempt).
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


# Aave V3 LiquidationCall event topic (for rival checks)
LIQUIDATION_TOPIC = Web3.keccak(
    text="LiquidationCall(address,address,address,uint256,uint256,address,bool)"
).hex()

def _current_mode() -> str:
    """Read mode fresh from config each call so Settings changes take effect instantly."""
    return cfg("mode", "live") if isinstance(cfg.__defaults__, tuple) else "live"


def get_mode() -> str:
    """Returns 'simulate' or 'live'. Reads live from config.json."""
    try:
        import bot.utils as _u
        raw = (_u.load_config() or {}).get("mode", "live")
        return raw if raw in ("simulate", "live") else "live"
    except Exception:
        return "live"


class LiquidationExecutor:
    def __init__(self):
        self._w3      = get_web3()
        self._account = get_account()
        contract_addr = cfg("wallet", "liquidator_contract")

        if not contract_addr or contract_addr == "DEPLOY_CONTRACT_ADDRESS_HERE":
            self._contract = None
            logger.warning("liquidator_contract not set -- execution will be disabled")
        else:
            self._contract = self._w3.eth.contract(
                address=checksum(contract_addr),
                abi=LIQUIDATOR_CONTRACT_ABI
            )

        if self._account:
            acc_str = f"{self._account.address[:10]}..."
        else:
            acc_str = "NOT_SET"
            logger.warning("private_key not set -- live execution and simulation disabled")

        logger.info(
            f"Executor initialized | Contract: {contract_addr[:10] if self._contract else 'None'} | "
            f"Wallet: {acc_str}"
        )

    def _build_tx(self, position: dict, gas_params: dict) -> dict:
        """Build the liquidation transaction dict (used for both simulate and live)."""
        col_token    = position["collateral_token"]
        debt_token   = position["debt_token"]
        debt_amount  = position["debt_to_cover"]
        pool_address = position["pool_address"]
        swap_fee     = get_swap_fee(col_token, debt_token)
        profit_info  = position.get("profit_info", {})
        nonce        = self._w3.eth.get_transaction_count(self._account.address)

        # Check for multi-hop route from swap_router
        swap_params = position.get("swap_params", {})
        is_multi    = swap_params.get("route") == "multi"

        # 4. Profitability & Slippage protection (on-chain)
        # We'll set min_profit to 50% of our estimated net profit.
        est_profit_usd = profit_info.get("estimated_profit_usd", 0)
        debt_price     = position.get("debt_price", 1.0)
        min_profit_wei = 0
        if est_profit_usd > 0:
            target_profit_usd = est_profit_usd * 0.5
            token_map = get_token_map()
            debt_info = token_map.get(debt_token.lower(), {"decimals": 18})
            min_profit_wei = int((target_profit_usd / debt_price) * (10 ** debt_info["decimals"]))

        if is_multi:
            path = swap_params.get("path", b"")
            if isinstance(path, str) and path.startswith("0x"):
                path = bytes.fromhex(path[2:])
            elif isinstance(path, str) and path.startswith("hex:"):
                path = bytes.fromhex(path[4:])
            elif isinstance(path, str):
                try: path = bytes.fromhex(path)
                except: pass

            logger.info(
                f"[EXECUTE] MultiHop Parameters:\n"
                f"  debtToken:        {debt_token}\n"
                f"  collateralToken:  {col_token}\n"
                f"  borrower:         {position['user']}\n"
                f"  debtAmount:       {debt_amount}\n"
                f"  lendingPool:      {pool_address}\n"
                f"  swapPath:         {path.hex() if isinstance(path, bytes) else path}\n"
                f"  minProfit (wei):  {min_profit_wei}"
            )

            return self._contract.functions.executeLiquidationMultiHop(
                checksum(debt_token),
                checksum(col_token),
                checksum(position["user"]),
                debt_amount,
                checksum(pool_address),
                path,
                min_profit_wei
            ).build_transaction({
                "from":    self._account.address,
                "nonce":   nonce,
                "chainId": cfg("network", "chain_id"),
                **gas_params,
            })
        else:
            logger.info(
                f"[EXECUTE] SingleHop Parameters:\n"
                f"  debtToken:        {debt_token}\n"
                f"  collateralToken:  {col_token}\n"
                f"  borrower:         {position['user']}\n"
                f"  debtAmount:       {debt_amount}\n"
                f"  lendingPool:      {pool_address}\n"
                f"  swapFee:          {swap_fee}\n"
                f"  minProfit (wei):  {min_profit_wei}"
            )
            return self._contract.functions.executeLiquidation(
                checksum(debt_token),
                checksum(col_token),
                checksum(position["user"]),
                debt_amount,
                checksum(pool_address),
                swap_fee,
                min_profit_wei
            ).build_transaction({
                "from":    self._account.address,
                "nonce":   nonce,
                "chainId": cfg("network", "chain_id"),
                **gas_params,
            })

    def _check_rival_liquidation(self, user: str, blocks: int = 50) -> Optional[dict]:
        """Check if any competitor liquidated this user in the last 'blocks'."""
        try:
            current = self._w3.eth.block_number
            # Filter logs by the LiquidationCall topic and the user address (indexed param 3)
            logs = self._w3.eth.get_logs({
                "fromBlock": max(0, current - blocks),
                "toBlock":   "latest",
                "topics": [
                    LIQUIDATION_TOPIC,
                    None, None,
                    "0x" + user.lower()[2:].zfill(64)
                ]
            })
            if logs:
                log     = logs[0]
                tx_hash = log["transactionHash"]
                try:
                    receipt = self._w3.eth.get_transaction_receipt(tx_hash)
                    tx      = self._w3.eth.get_transaction(tx_hash)
                    gas_price_gwei = (tx["maxFeePerGas"] / 1e9) if "maxFeePerGas" in tx else (tx.get("gasPrice", 0) / 1e9)
                    return {
                        "block":          log["blockNumber"],
                        "tx_hash":        tx_hash.hex(),
                        "gas_price_gwei": round(gas_price_gwei, 2),
                        "gas_used":       receipt.get("gasUsed", 0)
                    }
                except:
                    return {
                        "block":   log["blockNumber"],
                        "tx_hash": tx_hash.hex()
                    }
        except Exception as e:
            logger.debug(f"Rival check error: {e}")
        return None

    def _simulate_tx(self, tx: dict, position: dict, profit_info: dict) -> Optional[str]:
        """
        Simulate mode: validate via eth_call, record result, do NOT send.
        Now includes 'Rival Check' to see if someone else beat us.
        """
        protocol = position.get("protocol", "unknown")
        user     = position["user"]
        
        # Latency check: how long since we first spotted this zombie?
        spotted_at = position.get("queued_at", time.time())
        latency    = time.time() - spotted_at

        try:
            # 1. Validation (eth_call)
            self._w3.eth.call({
                "from": self._account.address,
                "to":   self._contract.address,
                "data": tx["data"],
                "gas":  tx["gas"],
            })
            gas_est = self._w3.eth.estimate_gas({
                "from": self._account.address,
                "to":   self._contract.address,
                "data": tx["data"],
            })

            # 2. Rival check (did someone else already do it?)
            rival = self._check_rival_liquidation(user)
            
            sim_id = f"sim-{int(time.time())}-{user[:6]}"
            
            if rival:
                gas_msg = f" | Rival Gas: {rival['gas_price_gwei']} gwei" if "gas_price_gwei" in rival else ""
                logger.warning(
                    f"[{protocol}] SIMULATE [LOST] Rival bot beat us! | "
                    f"Rival TX: {rival['tx_hash'][:14]}... | Block: {rival['block']}{gas_msg} | "
                    f"Discovery Latency: {latency:.2f}s"
                )
            else:
                logger.info(
                    f"[{protocol}] SIMULATE [WIN] eth_call passed | gas~{gas_est:,} | "
                    f"Discovery Latency: {latency:.2f}s | id: {sim_id}"
                )

            record_liquidation(
                tx_hash=sim_id,
                protocol=protocol,
                borrower=user,
                col_token=position["collateral_token"],
                debt_token=position["debt_token"],
                debt_usd=profit_info["debt_to_cover_usd"],
                est_profit=profit_info["estimated_profit_usd"],
                gas_used=gas_est,
                block=self._w3.eth.block_number
            )
            return sim_id

        except Exception as e:
            err = str(e)
            reason = (
                "Position recovered (HF > 1)"  if "health"        in err.lower() else
                "Swap slippage too high"        if "insufficient"  in err.lower() else
                "Out of gas"                    if "gas"           in err.lower() else
                err[:100]
            )
            # Even if it failed now, maybe someone just beat us?
            rival = self._check_rival_liquidation(user)
            if rival:
                logger.info(f"[{protocol}] SIMULATED [LOST] Rival bot liquidated target at block {rival['block']}")
            else:
                logger.info(f"[{protocol}] SIMULATED [FAIL] | {reason} | Latency: {latency:.2f}s")
            return None

        except Exception as e:
            # Try to decode revert reason if possible
            err_msg = str(e)
            if "execution reverted" in err_msg.lower():
                logger.info(f"[{protocol}] SIMULATION REVERTED for {user[:8]}: {err_msg}")
            else:
                logger.info(f"[{protocol}] SIMULATION ERROR for {user[:8]}: {e}")
            return None

    def _live_tx(self, tx: dict, position: dict, profit_info: dict) -> Optional[str]:
        """Live mode: sign and broadcast the real transaction."""
        protocol = position.get("protocol", "unknown")
        user     = position["user"]
        try:
            signed  = self._account.sign_transaction(tx)
            # Use rawTransaction (web3.py v6 attribute)
            raw_tx  = getattr(signed, "rawTransaction", getattr(signed, "raw_transaction", None))
            tx_hash = self._w3.eth.send_raw_transaction(raw_tx)
            tx_hex  = tx_hash.hex()
            logger.info(f"[{protocol}] TX sent: {tx_hex}")

            receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)

            if receipt.status == 1:
                gas_used = receipt.gasUsed
                logger.info(
                    f"[{protocol}] [OK] SUCCESS | TX: {tx_hex[:20]}... | "
                    f"Gas: {gas_used:,} | "
                    f"Est. profit: ${profit_info['estimated_profit_usd']:.2f}"
                )
                record_liquidation(
                    tx_hash=tx_hex,
                    protocol=protocol,
                    borrower=user,
                    col_token=position["collateral_token"],
                    debt_token=position["debt_token"],
                    debt_usd=profit_info["debt_to_cover_usd"],
                    est_profit=profit_info["estimated_profit_usd"],
                    gas_used=gas_used,
                    block=receipt.blockNumber
                )
                notify(
                    f"[OK] Liquidation!\n"
                    f"Protocol: {protocol}\n"
                    f"Profit: ${profit_info['estimated_profit_usd']:.2f}\n"
                    f"TX: {tx_hex}"
                )
                return tx_hex
            else:
                logger.error(f"[{protocol}] [FAIL] REVERTED | TX: {tx_hex}")
                return None

        except Exception as e:
            logger.info(f"[{protocol}] TX error for {user[:8]}: {e}")
            if cfg("notifications", "notify_on_error"):
                notify(f"[WARN] Bot error [{protocol}]: {str(e)[:200]}")
            return None

    def execute(self, position: dict) -> Optional[str]:
        """
        Execute or simulate one liquidation depending on current mode.
        Reads mode fresh from config.json so Settings changes apply instantly.
        """
        # 1. Refresh instance data from config
        self._account = get_account()
        contract_addr = cfg("wallet", "liquidator_contract")
        if contract_addr and contract_addr != "DEPLOY_CONTRACT_ADDRESS_HERE":
            self._contract = self._w3.eth.contract(address=checksum(contract_addr), abi=LIQUIDATOR_CONTRACT_ABI)
        else:
            self._contract = None

        mode     = get_mode()
        protocol = position.get("protocol", "unknown")
        user     = position["user"]

        # ── Final On-Chain Verification ──────────────────────────────────────
        # Confirm position is still open and liquidatable immediately before fire
        try:
            pool_addr = position.get("pool_address")
            if pool_addr:
                from .utils import AAVE_POOL_ABI, health_factor_float
                pool = self._w3.eth.contract(address=checksum(pool_addr), abi=AAVE_POOL_ABI)
                data = pool.functions.getUserAccountData(checksum(user)).call()

                fresh_hf = health_factor_float(data[5])
                total_debt = data[1]

                if total_debt == 0:
                    logger.info(f"[{protocol}] Skipping {user[:8]}... Position already repaid/closed")
                    return None

                # Log for transparency but don't abort yet; let the pre-flight simulation decide
                if fresh_hf > 1.0:
                    logger.info(
                        f"[{protocol}] Fresh HF check: {user[:8]} is {fresh_hf:.4f} "
                        f"(Discovery: {position.get('health_factor',0):.4f})"
                    )

                # Update position object with latest on-chain data
                position["health_factor"] = fresh_hf
        except Exception as e:
            logger.info(f"[{protocol}] Pre-execution HF check failed for {user[:8]}: {e}")

        # Profitability check (runs in both modes)
        # Use force_fresh=True for final pre-execution check to ensure "at the moment" accuracy
        profit_info = estimate_profit_usd(position, force_fresh=True)
        if not profit_info["profitable"]:
            logger.info(
                f"[{protocol}] Skip {user[:8]}... -- "
                f"{profit_info.get('reason', 'unprofitable')}"
            )
            return None

        # Gas spike guard (live mode only — in simulate mode gas doesn't matter)
        if mode == "live" and is_gas_spike():
            logger.warning(f"[{protocol}] Gas spike -- skipping {user[:8]}...")
            return None

        logger.info(
            f"[{protocol}] [{mode.upper()}] {user[:10]}... | "
            f"HF={position.get('health_factor', 0):.4f} | "
            f"Debt=${profit_info['debt_to_cover_usd']:.0f} | "
            f"Est.profit=${profit_info['estimated_profit_usd']:.2f} | "
            f"{position.get('collateral_symbol','?')} "
            f"({position.get('collateral_bonus', 0)*100:.1f}% bonus)"
        )

        try:
            # Pre-flight check: simulate the actual transaction in BOTH modes
            # This catches any complex reverts (e.g., protocol internal state)
            # before we proceed. In LIVE mode, it saves gas.
            try:
                gas_params = get_gas_params(profit_info["estimated_profit_usd"])
                tx         = self._build_tx(position, gas_params)
                self._w3.eth.call({
                    "from": self._account.address,
                    "to":   self._contract.address,
                    "data": tx["data"],
                })
            except Exception as e:
                logger.info(f"[{protocol}] Simulation/Pre-flight failed for {user[:8]}: {e}")
                return None

            if mode == "simulate":
                # In simulate mode, we can proceed even without a wallet/contract
                # by doing a "Profit-only" simulation.
                if not self._account or not self._contract:
                    sim_id = f"sim-profit-{int(time.time())}-{user[:6]}"
                    logger.info(f"[{protocol}] SIMULATED [PROFIT-ONLY] (No contract/wallet) | id: {sim_id}")
                    record_liquidation(
                        tx_hash=sim_id,
                        protocol=protocol,
                        borrower=user,
                        col_token=position["collateral_token"],
                        debt_token=position["debt_token"],
                        debt_usd=profit_info["debt_to_cover_usd"],
                        est_profit=profit_info["estimated_profit_usd"],
                        gas_used=0,
                        block=self._w3.eth.block_number
                    )
                    return sim_id

                # If we HAVE a contract, do the full eth_call simulation
                gas_params = get_gas_params(profit_info["estimated_profit_usd"])
                tx         = self._build_tx(position, gas_params)
                return self._simulate_tx(tx, position, profit_info)

            else: # LIVE mode
                if not self._account or not self._contract:
                    logger.warning(f"[{protocol}] Skipping LIVE -- missing private_key or liquidator_contract")
                    return None

                gas_params = get_gas_params(profit_info["estimated_profit_usd"])
                tx         = self._build_tx(position, gas_params)
                return self._live_tx(tx, position, profit_info)

        except Exception as e:
            logger.error(f"[{protocol}] Execute error for {user[:8]}: {e}")
            return None

    def execute_batch(self, positions: list) -> list:
        """Execute a list of positions. Returns successful tx hashes / sim ids."""
        results = []
        for pos in positions:
            tx = self.execute(pos)
            if tx:
                results.append(tx)
            time.sleep(1)
        return results

    def withdraw_profits(self, token_address: str) -> Optional[str]:
        """Withdraw accumulated profits from the deployed contract (live only)."""
        if get_mode() == "simulate":
            logger.info("Withdraw skipped -- bot is in SIMULATE mode")
            return None
        
        if not self._account or not self._contract:
            logger.warning("Withdraw failed -- missing private_key or liquidator_contract")
            return None

        try:
            gas_cfg = cfg("gas")
            nonce   = self._w3.eth.get_transaction_count(self._account.address)
            tx = self._contract.functions.withdraw(
                checksum(token_address)
            ).build_transaction({
                "from":                 self._account.address,
                "nonce":                nonce,
                "gas":                  120_000,
                "maxFeePerGas":         self._w3.to_wei(gas_cfg["max_fee_per_gas_gwei"], "gwei"),
                "maxPriorityFeePerGas": self._w3.to_wei(gas_cfg["max_priority_fee_gwei"], "gwei"),
                "chainId":              cfg("network", "chain_id"),
            })
            signed  = self._account.sign_transaction(tx)
            tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            if receipt.status == 1:
                logger.info(f"Withdrew profits for {token_address[:10]}...")
                return tx_hash.hex()
        except Exception as e:
            logger.error(f"Withdraw error: {e}")
        return None
