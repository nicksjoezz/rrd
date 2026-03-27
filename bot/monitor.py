"""
monitor.py -- Scans multiple lending protocols for liquidatable positions.

Protocols supported (configured in config.json):
  - Aave V3 (Arbitrum)
  - Radiant Capital (Aave-fork)
  - Easily extendable to any Aave-compatible protocol

Edge advantages:
  1. Multi-protocol — most bots only watch Aave
  2. No minimum size filter — small positions on Arbitrum are profitable
  3. Long-tail asset detection (ARB, GMX, LINK with 10–15% bonus)
  4. Zombie queue integration for HF 0.95-1.05 pre-queuing
"""

import logging
import time
import asyncio
from typing import List, Optional
from web3 import Web3

from .utils import (
    get_web3, get_public_web3, get_alchemy_web3,
    cfg, get_token_map, checksum,
    wei_to_usd_base, health_factor_float,
    AAVE_POOL_ABI, DATA_PROVIDER_ABI, COMET_ABI,
    SILO_ABI, SILO_FACTORY_ABI, MORPHO_BLUE_ABI,
    logger, notify,
    MULTICALL3_ADDR, MULTICALL3_ABI,
    call_with_retry
)
from .swap_router import get_best_swap
import urllib.request as _urllib



from .zombie_queue import ZombieQueue
from .database import (
    upsert_borrowers, get_borrowers, upsert_position,
    get_last_scan_block, set_last_scan_block, delete_stale_positions,
    delete_position
)
from .velocity import VelocityTracker

logger = logging.getLogger("liquidation_bot.monitor")


class MorphoBlueMonitor:
    """
    Monitors Morpho Blue markets.
    Single contract for all markets.
    """

    def __init__(self, name: str, pool_addr: str):
        self.name = name
        self.pool_addr = pool_addr
        self._borrowers: set = set()

        w3 = get_web3()
        self.pool = w3.eth.contract(address=checksum(pool_addr), abi=MORPHO_BLUE_ABI)

    def load_borrowers_from_db(self):
        cached = get_borrowers(self.name)
        self._borrowers.update(cached)

    def load_borrowers_from_events(self, from_block: int, to_block: int, on_batch_found=None):
        """
        Morpho Supply (topic0: 0x4f128c...90) or Borrow.
        We scan Borrow events.
        """
        w3 = get_web3()
        # Borrow event: Borrow(bytes32 indexed id, address caller, address indexed onBehalfOf,
        #   address receiver, uint256 assets, uint256 shares)
        BORROW_TOPIC = "0x013a3e29f3796d833454b5093e0315183495d015c92c89280145c360098df156"
        new_borrowers = set()
        chunk = cfg("scanning", "event_scan_chunk")

        for start in range(from_block, to_block, chunk):
            end = min(start + chunk - 1, to_block)
            try:
                logs = w3.eth.get_logs({
                    "address": checksum(self.pool_addr),
                    "topics": [BORROW_TOPIC],
                    "fromBlock": start,
                    "toBlock": end,
                })
                batch_found = []
                for log in logs:
                    topics = log.get("topics", [])
                    if len(topics) >= 3:
                        addr = "0x" + topics[2].hex()[-40:]
                        caddr = w3.to_checksum_address(addr)
                        if caddr not in self._borrowers and caddr not in new_borrowers:
                            batch_found.append(caddr)
                            new_borrowers.add(caddr)

                if on_batch_found and batch_found:
                    on_batch_found(self.name, batch_found)
            except Exception:
                continue

        self._borrowers.update(new_borrowers)
        set_last_scan_block(self.name, to_block)
        if new_borrowers:
            upsert_borrowers(list(new_borrowers), self.name)

    def check_position(self, user: str, market_id: bytes) -> Optional[dict]:
        """
        Morpho health: LTV = borrowValue / collateralValue.
        Liquidatable if LTV > LLTV.
        """
        w3 = get_web3()
        try:
            pos = call_with_retry(self.pool.functions.position, market_id, checksum(user))
            # pos = (supplyShares, borrowShares, collateral)
            collateral = pos[2]
            if collateral == 0: return None

            # Simplified HF for the dashboard.
            # In production, we'd fetch the oracle price and calculate exact LTV.
            # Here we provide a "simulated" check to ensure the dashboard works.
            hf = 1.02 # Placeholder until price feed integration

            return {
                "protocol":          self.name,
                "user":              user,
                "collateral_token":  "0x0000000000000000000000000000000000000000",
                "collateral_symbol": "MORPHO",
                "collateral_bonus":  0.05,
                "debt_token":        "0x0000000000000000000000000000000000000000",
                "debt_symbol":       "UNKNOWN",
                "debt_to_cover":     pos[1],
                "health_factor":     hf,
                "total_debt_usd":    0,
                "total_col_usd":     0,
                "pool_address":      self.pool_addr,
                "swap_params":       "",
            }
        except Exception:
            return None

    def scan_all(self, zombie_queue: Optional[ZombieQueue] = None) -> List[dict]:
        return self.scan_users(list(self._borrowers), zombie_queue=zombie_queue)

    def scan_users(self, users: List[str], zombie_queue: Optional[ZombieQueue] = None) -> List[dict]:
        liquidatable = []
        # In production, we'd map users to their marketIds.
        # For this implementation, we use a placeholder marketId.
        market_id = b"\x00" * 32
        for user in users:
            pos = self.check_position(user, market_id)
            if pos:
                upsert_position(pos)
                if pos["health_factor"] <= 1.0:
                    liquidatable.append(pos)
                if zombie_queue:
                    zombie_queue.update(self.name, user, pos)
            else:
                delete_position(user, self.name)
        return liquidatable

    def get_borrower_count(self) -> int:
        return len(self._borrowers)


class SiloV2Monitor:
    """
    Monitors Silo Finance V2 markets.
    Each Silo is isolated. We enumerate all silos from the factory.
    """

    def __init__(self, name: str, factory_addr: str):
        self.name = name
        self.factory_addr = factory_addr
        self._silo_addresses = []
        self._borrowers: set = set()

        w3 = get_web3()
        self.factory = w3.eth.contract(address=checksum(factory_addr), abi=SILO_FACTORY_ABI)

    def load_borrowers_from_db(self):
        cached = get_borrowers(self.name)
        self._borrowers.update(cached)
        if cached:
            logger.info(f"[{self.name}] Loaded {len(cached):,} cached borrowers from DB")

    def load_borrowers_from_events(self, from_block: int, to_block: int, on_batch_found=None):
        """
        Scan Borrow events for each Silo.
        Silo V2 Borrow topic: 0x312a5e5e1079f5dda4e95dbbd0b908b291fd5b992ef22073643ab691572c5b52
        """
        w3 = get_web3()
        BORROW_TOPIC = "0x312a5e5e1079f5dda4e95dbbd0b908b291fd5b992ef22073643ab691572c5b52"

        if not self._silo_addresses:
            try:
                self._silo_addresses = call_with_retry(self.factory.functions.getSilos)
                logger.info(f"[{self.name}] Found {len(self._silo_addresses)} silos from factory")
            except Exception as e:
                logger.error(f"[{self.name}] Failed to get silos: {e}")
                return

        new_borrowers = set()
        chunk = cfg("scanning", "event_scan_chunk")

        # In a real bot, we'd distribute this over multiple cycles or use a subgraph
        # For this task, we scan the top silos
        for silo_addr in self._silo_addresses[:20]: # Only top 20 silos for performance
            for start in range(from_block, to_block, chunk):
                end = min(start + chunk - 1, to_block)
                try:
                    logs = w3.eth.get_logs({
                        "address": checksum(silo_addr),
                        "topics": [BORROW_TOPIC],
                        "fromBlock": start,
                        "toBlock": end,
                    })
                    batch_found = []
                    for log in logs:
                        topics = log.get("topics", [])
                        if len(topics) >= 3:
                            addr = "0x" + topics[2].hex()[-40:]
                            caddr = w3.to_checksum_address(addr)
                            if caddr not in self._borrowers and caddr not in new_borrowers:
                                batch_found.append(caddr)
                                new_borrowers.add(caddr)

                    if on_batch_found and batch_found:
                        on_batch_found(self.name, batch_found)
                except Exception:
                    continue

        self._borrowers.update(new_borrowers)
        set_last_scan_block(self.name, to_block)
        if new_borrowers:
            upsert_borrowers(list(new_borrowers), self.name)

    def check_position(self, user: str) -> Optional[dict]:
        """
        Silo health is (ltv, lt). If ltv > lt, it is liquidatable.
        Since we don't know which silo the user is in without a scan,
        we check the silos they were found in.
        For simplicity in this implementation, we assume we check the silo's getUserHealth.
        """
        w3 = get_web3()
        # In a production bot, we'd track which user belongs to which silo(s).
        # Here we attempt to find the silo by checking health on known active silos.
        for silo_addr in self._silo_addresses[:20]:
            try:
                silo = w3.eth.contract(address=checksum(silo_addr), abi=SILO_ABI)
                ltv, lt = silo.functions.getUserHealth(checksum(user)).call()

                if lt == 0: continue # User has no position here

                # Health Factor = LT / LTV (simplified)
                hf = lt / ltv if ltv > 0 else 2.0

                if hf > 1.15: continue

                return {
                    "protocol":          self.name,
                    "user":              user,
                    "collateral_token":  silo_addr, # Placeholder
                    "collateral_symbol": "SILO",
                    "collateral_bonus":  0.10,
                    "debt_token":        "0x0000000000000000000000000000000000000000",
                    "debt_symbol":       "UNKNOWN",
                    "debt_to_cover":     0,
                    "health_factor":     hf,
                    "total_debt_usd":    0, # Requires complex reserve data
                    "total_col_usd":     0,
                    "pool_address":      silo_addr,
                    "swap_params":       "",
                }
            except Exception:
                continue
        return None

    def scan_all(self, zombie_queue: Optional[ZombieQueue] = None) -> List[dict]:
        return self.scan_users(list(self._borrowers), zombie_queue=zombie_queue)

    def scan_users(self, users: List[str], zombie_queue: Optional[ZombieQueue] = None) -> List[dict]:
        liquidatable = []
        for user in users:
            pos = self.check_position(user)
            if pos:
                upsert_position(pos)
                if pos["health_factor"] <= 1.0:
                    liquidatable.append(pos)
                if zombie_queue:
                    zombie_queue.update(self.name, user, pos)
            else:
                delete_position(user, self.name)
        return liquidatable

    def get_borrower_count(self) -> int:
        return len(self._borrowers)


class CompoundIIIMonitor:
    """
    Monitors a Compound III (Comet) market.
    Compound III uses a simplified model where isLiquidatable() returns a boolean.
    """

    def __init__(self, name: str, pool_addr: str):
        self.name = name
        self.pool_addr = pool_addr
        self._borrowers: set = set()

        w3 = get_web3()
        self.pool = w3.eth.contract(address=checksum(pool_addr), abi=COMET_ABI)
        self.base_token = self.pool.functions.baseToken().call()

    def load_borrowers_from_db(self):
        cached = get_borrowers(self.name)
        self._borrowers.update(cached)
        if cached:
            logger.info(f"[{self.name}] Loaded {len(cached):,} cached borrowers from DB")

    def load_borrowers_from_events(self, from_block: int, to_block: int, on_batch_found=None):
        """
        Compound III Supply event (topic0: 0xd6d480d5b3068db003533b170d67561494d72e3bf9fa40a266471351ebba9e16)
        """
        w3 = get_web3()
        chunk = cfg("scanning", "event_scan_chunk")
        SUPPLY_TOPIC = "0xd6d480d5b3068db003533b170d67561494d72e3bf9fa40a266471351ebba9e16"
        new_borrowers = set()

        total_reqs = (to_block - from_block) // chunk + 1
        for i, start in enumerate(range(from_block, to_block, chunk), 1):
            end = min(start + chunk - 1, to_block)
            try:
                logs = w3.eth.get_logs({
                    "address": checksum(self.pool_addr),
                    "topics": [SUPPLY_TOPIC],
                    "fromBlock": start,
                    "toBlock": end,
                })
                batch_found = []
                for log in logs:
                    topics = log.get("topics", [])
                    if len(topics) >= 3:
                        # topics[2] is dst (the user who supplied or received supply)
                        addr = "0x" + topics[2].hex()[-40:]
                        caddr = w3.to_checksum_address(addr)
                        if caddr not in self._borrowers and caddr not in new_borrowers:
                            batch_found.append(caddr)
                            new_borrowers.add(caddr)

                if on_batch_found and batch_found:
                    on_batch_found(self.name, batch_found)
            except Exception:
                continue

        self._borrowers.update(new_borrowers)
        set_last_scan_block(self.name, to_block)
        if new_borrowers:
            upsert_borrowers(list(new_borrowers), self.name)

    def check_position(self, user: str, is_liquidatable: Optional[bool] = None) -> Optional[dict]:
        try:
            if is_liquidatable is None:
                is_liquidatable = call_with_retry(self.pool.functions.isLiquidatable, checksum(user))

            # Get borrow balance
            debt_raw = call_with_retry(self.pool.functions.borrowBalanceOf, checksum(user))
            if debt_raw == 0:
                return None

            # Comet base tokens are typically 6 decimals (USDC)
            # For the dashboard, we'll estimate a "health factor"
            # If is_liquidatable is True, HF = 0.99, else 1.01 (simplified)
            hf = 0.95 if is_liquidatable else 1.1

            # TODO: Add full collateral value calculation for accurate HF and debt_usd
            # For now, we use a placeholder debt_usd based on 6 decimals
            debt_usd = debt_raw / 1e6

            if not is_liquidatable and hf > 1.15:
                return None

            return {
                "protocol":          self.name,
                "user":              user,
                "collateral_token":  "0x0000000000000000000000000000000000000000", # Multi-collateral in Comet
                "collateral_symbol": "COMET",
                "collateral_bonus":  0.07, # Compound III typically has ~7% liquidation penalty
                "debt_token":        self.base_token,
                "debt_symbol":       "USDC",
                "debt_to_cover":     debt_raw,
                "health_factor":     hf,
                "total_debt_usd":    debt_usd,
                "total_col_usd":     debt_usd * 1.1, # Dummy col value
                "pool_address":      self.pool_addr,
                "swap_params":       "", # Compound uses absorb() then buyCollateral()
            }
        except Exception:
            return None

    def scan_all(self, zombie_queue: Optional[ZombieQueue] = None) -> List[dict]:
        return self.scan_users(list(self._borrowers), zombie_queue=zombie_queue)

    def scan_users(self, users: List[str], zombie_queue: Optional[ZombieQueue] = None) -> List[dict]:
        liquidatable = []
        for user in users:
            pos = self.check_position(user)
            if pos:
                upsert_position(pos)
                if pos["health_factor"] <= 1.0:
                    liquidatable.append(pos)
                if zombie_queue:
                    zombie_queue.update(self.name, user, pos)
            else:
                delete_position(user, self.name)
        return liquidatable

    def get_borrower_count(self) -> int:
        return len(self._borrowers)


class ProtocolMonitor:
    """
    Monitors a single Aave-compatible lending protocol.
    Reusable for Aave, Radiant, Silo, etc.
    """

    def __init__(self, name: str, pool_addr: str, data_provider_addr: str, borrow_topic: str):
        self.name               = name
        self.pool_addr          = pool_addr
        self.data_provider_addr  = data_provider_addr
        self.borrow_topic        = borrow_topic
        self._borrowers: set   = set()
        self._last_scan_block  = 0

        w3 = get_web3()
        self.pool = w3.eth.contract(
            address=checksum(pool_addr),
            abi=AAVE_POOL_ABI
        )
        self.data_provider = w3.eth.contract(
            address=checksum(data_provider_addr),
            abi=DATA_PROVIDER_ABI
        ) if data_provider_addr else None

    def load_borrowers_from_db(self):
        """Load previously scanned borrowers from SQLite (fast restart)."""
        cached = get_borrowers(self.name)
        self._borrowers.update(cached)
        if cached:
            logger.info(f"[{self.name}] Loaded {len(cached):,} cached borrowers from DB")

    def load_borrowers_from_events(self, from_block: int, to_block: int, on_batch_found=None):
        """
        Scan Borrow event logs using raw eth_getLogs — NOT the web3 event helper.

        Root cause fix: Aave V3 Borrow event has interestRateMode as an internal
        enum type. web3.py's event helper fails to decode it ('anonymous' error).
        Raw eth_getLogs bypasses all ABI decoding — onBehalfOf lives in topics[2]
        as a standard 32-byte indexed address, which we read directly.
        """
        w3    = get_web3()
        chunk = cfg("scanning", "event_scan_chunk")
        # Use protocol-specific topic0
        new_borrowers = set()

        total_reqs = (to_block - from_block) // chunk + 1
        logger.info(f"[{self.name}] Discovery scan: {from_block:,} -> {to_block:,} ({total_reqs} chunks)")

        for i, start in enumerate(range(from_block, to_block, chunk), 1):
            end = min(start + chunk - 1, to_block)
            try:
                if i % 50 == 0 or i == 1 or i == total_reqs:
                    pct = (i / total_reqs) * 100
                    logger.info(f"[{self.name}] Progress: {pct:.1f}%  |  Borrowers found: {len(new_borrowers)}")

                logs = w3.eth.get_logs({
                    "address":   w3.to_checksum_address(self.pool_addr),
                    "topics":    [self.borrow_topic],
                    "fromBlock": start,
                    "toBlock":   end,
                })
                batch_found = []
                for log in logs:
                    topics = log.get("topics", [])
                    if len(topics) >= 3:
                        raw = topics[2]
                        # Extract address from topic (last 20 bytes / 40 chars)
                        addr_hex = raw.hex() if isinstance(raw, bytes) else str(raw)
                        addr     = "0x" + addr_hex[-40:]
                        try:
                            caddr = w3.to_checksum_address(addr)
                            if caddr not in self._borrowers and caddr not in new_borrowers:
                                batch_found.append(caddr)
                                new_borrowers.add(caddr)
                        except Exception:
                            pass
                
                # Streaming verification: if we found new borrowers, trigger callback
                if on_batch_found and batch_found:
                    on_batch_found(self.name, batch_found)

            except Exception as ex:
                logger.debug(f"[{self.name}] Log chunk {start}-{end}: {ex}")
                time.sleep(0.5)

        added = len(new_borrowers - self._borrowers)
        self._borrowers.update(new_borrowers)
        
        # Always update the scan state so we don't re-scan the same range
        set_last_scan_block(self.name, to_block)
        
        if new_borrowers:
            upsert_borrowers(list(new_borrowers), self.name)
            
        if added:
            logger.info(f"[{self.name}] +{added} new borrowers (total: {len(self._borrowers):,})")

    def check_position(self, user: str, account_data: Optional[tuple] = None) -> Optional[dict]:
        """
        Check a single user's health factor. Returns position dict if liquidatable
        or approaching liquidation. Returns None if healthy.
        If account_data is provided (from multicall), it bils around it.
        """
        w3 = get_web3()
        try:
            if account_data:
                data = account_data
            else:
                data = call_with_retry(self.pool.functions.getUserAccountData, checksum(user))

            total_col  = data[0]
            total_debt = data[1]
            hf_raw     = data[5]

            if total_debt == 0:
                return None

            hf         = health_factor_float(hf_raw)
            col_usd    = wei_to_usd_base(total_col)
            debt_usd   = wei_to_usd_base(total_debt)

            min_debt = cfg("strategy", "min_debt_usd")
            max_debt = cfg("strategy", "max_debt_usd")

            if debt_usd < min_debt or debt_usd > max_debt:
                return None

            # Track positions up to 1.15 HF to keep dashboard updated during recovery
            if hf > 1.15:
                return None

            # Find best collateral and debt token
            tokens       = self._get_best_tokens(user)
            if not tokens:
                return None

            try:
                col_token, col_symbol, col_bonus, debt_token, debt_symbol, debt_raw = tokens
            except ValueError:
                logger.error(f"[{self.name}] Failed to unpack tokens for {user[:10]}: {tokens}")
                return None

            # Close factor: 100% if HF < 0.95 OR position < $2k, else 50%
            close_factor = cfg("strategy", "close_factor")
            if hf < 0.95 or debt_usd < 2000:
                close_factor = 1.0

            debt_to_cover = int(debt_raw * close_factor)

            # Pre-calculate best swap route for immediate fire
            _, _, swap_params = get_best_swap(col_token, debt_token, debt_to_cover)

            return {
                "protocol":          self.name,
                "user":              user,
                "collateral_token":  col_token,
                "collateral_symbol": col_symbol,
                "collateral_bonus":  col_bonus,
                "debt_token":        debt_token,
                "debt_symbol":       debt_symbol,
                "debt_to_cover":     debt_to_cover,
                "health_factor":     hf,
                "total_debt_usd":    debt_usd,
                "total_col_usd":     col_usd,
                "pool_address":      self.pool_addr,
                "swap_params":       swap_params,
            }

        except Exception as e:
            logger.warning(f"[{self.name}] check_position error for {user[:10]}: {e}")
            return None

    def _get_best_tokens(self, user: str):
        """
        Find the best collateral and debt tokens using Multicall.
        """
        if not self.data_provider:
            return None

        w3 = get_web3()
        mc = w3.eth.contract(address=MULTICALL3_ADDR, abi=MULTICALL3_ABI)

        token_items = list(cfg("tokens").items())
        calls = []
        for sym, info in token_items:
            call_data = self.data_provider.encodeABI("getUserReserveData", [checksum(info["address"]), checksum(user)])
            calls.append({"target": self.data_provider.address, "callData": call_data})

        try:
            _, return_data = mc.functions.aggregate(calls).call()

            best_col_score = 0
            best_col = None
            best_debt_score = 0
            best_debt = None
            best_debt_raw = 0

            for i, raw_res in enumerate(return_data):
                sym, info = token_items[i]
                # getUserReserveData return types: (uint256, uint256, uint256, uint256, uint256, uint256, uint256, uint40, bool)
                rd = w3.codec.decode(["uint256", "uint256", "uint256", "uint256", "uint256", "uint256", "uint256", "uint40", "bool"], raw_res)

                a_bal = rd[0]
                v_debt = rd[2]
                decimals = info["decimals"]
                bonus = info["liquidation_bonus"]

                if a_bal > 0:
                    usd = a_bal / (10 ** decimals)
                    score = usd * (1 + bonus) if cfg("strategy", "prioritize_high_bonus") else usd
                    if score > best_col_score:
                        best_col_score = score
                        best_col = (info["address"], sym, bonus)

                if v_debt > 0:
                    score = v_debt / (10 ** decimals)
                    if score > best_debt_score:
                        best_debt_score = score
                        best_debt = (info["address"], sym)
                        best_debt_raw = v_debt

            if not best_col or not best_debt:
                return None

            return (best_col[0], best_col[1], best_col[2], best_debt[0], best_debt[1], best_debt_raw)

        except Exception as e:
            logger.debug(f"Multicall reserve data error for {user[:8]}: {e}")
            return None

    def scan_all(self, zombie_queue: Optional[ZombieQueue] = None) -> List[dict]:
        """Scan all known borrowers."""
        return self.scan_users(list(self._borrowers), zombie_queue=zombie_queue)

    def scan_users(self, users: List[str], zombie_queue: Optional[ZombieQueue] = None) -> List[dict]:
        """
        Scan a specific list of borrowers using Multicall3.
        Returns list of liquidatable positions.
        """
        liquidatable = []
        if not users: return []
        
        batch_size = cfg("scanning", "batch_size") or 500
        zombie_entry = cfg("strategy", "zombie_queue", "entry_hf")

        for i in range(0, len(users), batch_size):
            # Rotate Web3 for each batch
            w3 = get_web3()
            mc = w3.eth.contract(address=MULTICALL3_ADDR, abi=MULTICALL3_ABI)

            chunk = users[i : i + batch_size]
            calls = []
            
            # Step 1: Batch getUserAccountData
            for user in chunk:
                call_data = self.pool.encodeABI("getUserAccountData", [checksum(user)])
                calls.append({"target": self.pool.address, "callData": call_data})
            
            try:
                # Use call_with_retry but since mc is bound to a specific w3,
                # we might need to be careful. However, mc.functions.aggregate(...).call()
                # uses the provider in mc.w3.
                _, return_data = mc.functions.aggregate(calls).call()
                
                # Step 2: Process results
                for j, raw_res in enumerate(return_data):
                    user = chunk[j]
                    dec = w3.codec.decode(["uint256", "uint256", "uint256", "uint256", "uint256", "uint256"], raw_res)
                    
                    if dec[1] == 0:
                        # Optimization: user has 0 debt, definitely not liquidatable.
                        # Cleanup any stale state.
                        delete_position(user, self.name)
                        if zombie_queue:
                            zombie_queue.update(self.name, user, {"total_debt_usd": 0})
                        continue
                    
                    hf = health_factor_float(dec[5])
                    
                    try:
                        pos = self.check_position(user, account_data=dec)
                        if pos:
                            # Record all at-risk positions in database
                            upsert_position(pos)

                            if zombie_queue:
                                result = zombie_queue.update(self.name, user, pos)
                                if result == "fire":
                                    liquidatable.append(pos)
                            elif hf <= 1.0:
                                liquidatable.append(pos)
                        else:
                            # If pos is None, it's either healthy or debt-free
                            # Remove from database as it's no longer at risk
                            delete_position(user, self.name)

                            # We should still notify zombie_queue so it can remove recovered positions
                            if zombie_queue:
                                zombie_queue.update(self.name, user, {"health_factor": hf})
                    except Exception as e:
                        logger.error(f"[{self.name}] Error checking position for {user[:10]}: {e}")
                        # On error, we do NOT update the zombie queue to avoid corrupting data with $0 defaults
                            
            except Exception as e:
                logger.error(f"[{self.name}] Multicall batch error: {e}")
                for user in chunk:
                    pos = self.check_position(user)
                    if pos: liquidatable.append(pos)

        return liquidatable

    def get_borrower_count(self) -> int:
        return len(self._borrowers)


class MultiProtocolMonitor:
    """
    Manages multiple ProtocolMonitor instances.
    Unified interface for scanning all protocols at once.
    """

    def __init__(self):
        self.monitors: dict = {}
        self.zombie_queue = ZombieQueue(
            entry_hf=cfg("strategy", "zombie_queue", "entry_hf"),
            fire_hf=cfg("strategy",  "zombie_queue", "fire_hf")
        )
        self.velocity = VelocityTracker()
        self._init_protocols()

    def _init_protocols(self):
        protocols = cfg("protocols")
        
        # Signatures
        AAVE_V3_BORROW = "0xb3d084820fb1a9decffb176436bd02558d15fac9b0ddfed8c465bc7359d7dce0"

        for name, pcfg in protocols.items():
            if not pcfg.get("enabled", False):
                continue

            ptype = pcfg.get("type", "aave_v3")
            pool_addr = pcfg.get("pool", "")
            dp_addr   = pcfg.get("data_provider", "")
            factory_addr = pcfg.get("factory", "")

            # Validation based on type
            if ptype == "silo_v2":
                if not factory_addr or factory_addr.startswith("0x000"):
                    logger.warning(f"Protocol {name} (silo_v2) has no valid factory address -- skipping")
                    continue
            else:
                if not pool_addr or pool_addr.startswith("0x000"):
                    logger.warning(f"Protocol {name} ({ptype}) has no valid pool address -- skipping")
                    continue
            
            # Default to V3 topic
            topic = AAVE_V3_BORROW

            if ptype == "compound_iii":
                self.monitors[name] = CompoundIIIMonitor(
                    name=name,
                    pool_addr=pool_addr
                )
            elif ptype == "silo_v2":
                self.monitors[name] = SiloV2Monitor(
                    name=name,
                    factory_addr=pcfg.get("factory", "")
                )
            elif ptype == "morpho_blue":
                self.monitors[name] = MorphoBlueMonitor(
                    name=name,
                    pool_addr=pool_addr
                )
            else:
                self.monitors[name] = ProtocolMonitor(
                    name=name,
                    pool_addr=pool_addr,
                    data_provider_addr=dp_addr,
                    borrow_topic=topic
                )
            logger.info(f"Initialized protocol monitor: {name} (topic: {topic[:10]}...)")

    async def _load_monitor_events(self, name, monitor, from_block, to_block):
        # Streaming callback: verifies borrowers as they are found
        def _streaming_callback(proto_name, users):
            m = self.monitors.get(proto_name)
            if not m: return

            found = m.scan_users(users, zombie_queue=self.zombie_queue)
            if found:
                logger.info(f"[{proto_name}] Streaming verification: {len(found)} at-risk positions found!")
                for p in found:
                    if p.get("health_factor", 2.0) <= 1.0:
                        logger.warning(f"[STREAMS] LIQUIDATABLE: {p['user']} HF={p['health_factor']:.4f}")

        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                monitor.load_borrowers_from_events,
                from_block, to_block, _streaming_callback
            )
        except Exception as e:
            logger.error(f"[{name}] Event load error: {e}")

    def load_all_borrowers(self):
        """
        Load borrowers using DB cache + incremental event scan concurrently.
        """
        w3            = get_web3()
        current_block = w3.eth.block_number
        ARBITRUM_50_DAYS = 17_280_000

        tasks = []
        for name, monitor in self.monitors.items():
            monitor.load_borrowers_from_db()
            last_block = get_last_scan_block(name)

            if last_block == 0:
                from_block = max(0, current_block - ARBITRUM_50_DAYS)
                logger.info(f"[{name}] First run -- scanning last 50 days")
            else:
                from_block = last_block + 1

            if from_block < current_block:
                tasks.append(self._load_monitor_events(name, monitor, from_block, current_block))

        # Load zombies
        zombies = self.zombie_queue.get_watching()
        for z in zombies:
            proto = z.get("protocol")
            user  = z.get("user")
            if proto and user and proto in self.monitors:
                self.monitors[proto]._borrowers.add(user.lower())

        if tasks:
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(asyncio.gather(*tasks))
                loop.close()
            except Exception as e:
                logger.error(f"Async discovery failed: {e}")

    def refresh_borrowers(self):
        """Incremental concurrent update."""
        w3 = get_web3()
        current_block = w3.eth.block_number

        tasks = []
        for name, monitor in self.monitors.items():
            last_block = get_last_scan_block(name)
            from_block = (last_block + 1) if last_block > 0 else (current_block - 1000)
            
            if from_block < current_block:
                tasks.append(self._load_monitor_events(name, monitor, from_block, current_block))

        if tasks:
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(asyncio.gather(*tasks))
                loop.close()
            except Exception as e:
                logger.error(f"Async refresh failed: {e}")

    async def _scan_monitor(self, name, monitor):
        try:
            # Run the synchronous scan_all in a thread pool to avoid blocking the event loop
            loop = asyncio.get_event_loop()
            positions = await loop.run_in_executor(None, monitor.scan_all, self.zombie_queue)
            logger.debug(f"[{name}] Found {len(positions)} liquidatable positions")
            return positions
        except Exception as e:
            logger.error(f"[{name}] Scan error: {e}")
            return []

    def scan_all_protocols(self) -> List[dict]:
        """
        Scan all enabled protocols concurrently. Returns list of liquidatable positions
        across all protocols, sorted by profit potential.
        Also records HF velocity for fast-falling detection.
        """
        # Run async scanning
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            tasks = [self._scan_monitor(name, monitor) for name, monitor in self.monitors.items()]
            results = loop.run_until_complete(asyncio.gather(*tasks))
            loop.close()
            all_positions = [pos for batch in results for pos in batch]
        except Exception as e:
            logger.error(f"Async scan failed: {e}")
            # Fallback to sync
            all_positions = []
            for name, monitor in self.monitors.items():
                try:
                    positions = monitor.scan_all(zombie_queue=self.zombie_queue)
                    all_positions.extend(positions)
                except Exception:
                    pass

        # Record velocity for all scanned positions (including non-liquidatable)
        for pos in all_positions:
            self.velocity.record(pos["protocol"], pos["user"], pos["health_factor"])

        # Also flush any zombie queue items that are now ready
        ready = self.zombie_queue.get_ready()
        for pos in ready:
            if pos not in all_positions:
                all_positions.append(pos)

        # Evict stale entries
        self.zombie_queue.evict_old()
        self.velocity.cleanup()

        # Prune positions table in SQLite (e.g. users who repaid and are no longer scanned)
        try:
            delete_stale_positions(max_age_seconds=86400)
        except Exception:
            pass

        return all_positions

    async def _scan_zombie_batch(self, proto_name, users):
        monitor = self.monitors.get(proto_name)
        if not monitor: return []
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, monitor.scan_users, users, self.zombie_queue)
        except Exception as e:
            logger.error(f"[{proto_name}] Zombie scan error: {e}")
            return []

    def scan_zombies(self) -> List[dict]:
        """
        High-priority concurrent scan specifically for wallets in the zombie queue.
        Ensures their HF is always up-to-date in the dashboard and database.
        Returns list of newly ready liquidations.
        """
        zombies = self.zombie_queue.get_watching()
        if not zombies: return []

        # Group by protocol
        by_proto = {}
        for z in zombies:
            p = z.get("protocol")
            if p not in by_proto: by_proto[p] = []
            by_proto[p].append(z.get("user"))

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            tasks = [self._scan_zombie_batch(pn, u) for pn, u in by_proto.items()]
            results = loop.run_until_complete(asyncio.gather(*tasks))
            loop.close()
            return [pos for batch in results for pos in batch]
        except Exception as e:
            logger.error(f"Async zombie scan failed: {e}")
            # Fallback
            all_ready = []
            for proto_name, users in by_proto.items():
                monitor = self.monitors.get(proto_name)
                if not monitor: continue
                try:
                    ready = monitor.scan_users(users, zombie_queue=self.zombie_queue)
                    all_ready.extend(ready)
                except Exception:
                    pass
            return all_ready

    def get_stats(self) -> dict:
        total = sum(m.get_borrower_count() for m in self.monitors.values())
        return {
            "protocols":       list(self.monitors.keys()),
            "total_borrowers": total,
            "zombie_watching": self.zombie_queue.size(),
        }

    def reload_protocols(self):
        """
        Re-initializes the protocol list from the latest config.
        Allows for dynamic enabling/disabling of protocols without restarting the bot.
        """
        logger.info("Reloading protocol configurations...")
        new_protocols = cfg("protocols")

        # 1. Disable protocols that are no longer in config or have been disabled
        to_remove = []
        for name in self.monitors:
            if name not in new_protocols or not new_protocols[name].get("enabled", False):
                to_remove.append(name)

        for name in to_remove:
            logger.info(f"Disabling protocol: {name}")
            del self.monitors[name]

        # 2. Enable/Add new protocols
        AAVE_V3_BORROW = "0xb3d084820fb1a9decffb176436bd02558d15fac9b0ddfed8c465bc7359d7dce0"

        for name, pcfg in new_protocols.items():
            if not pcfg.get("enabled", False) or name in self.monitors:
                continue

            ptype = pcfg.get("type", "aave_v3")
            pool_addr = pcfg.get("pool", "")
            dp_addr   = pcfg.get("data_provider", "")
            factory_addr = pcfg.get("factory", "")

            # Validation based on type
            if ptype == "silo_v2":
                if not factory_addr or factory_addr.startswith("0x000"):
                    logger.warning(f"Protocol {name} (silo_v2) has no valid factory address -- skipping")
                    continue
            else:
                if not pool_addr or pool_addr.startswith("0x000"):
                    logger.warning(f"Protocol {name} ({ptype}) has no valid pool address -- skipping")
                    continue

            # Default to V3 topic
            topic = AAVE_V3_BORROW

            if ptype == "compound_iii":
                self.monitors[name] = CompoundIIIMonitor(name=name, pool_addr=pool_addr)
            elif ptype == "silo_v2":
                self.monitors[name] = SiloV2Monitor(name=name, factory_addr=factory_addr)
            elif ptype == "morpho_blue":
                self.monitors[name] = MorphoBlueMonitor(name=name, pool_addr=pool_addr)
            else:
                self.monitors[name] = ProtocolMonitor(
                    name=name, pool_addr=pool_addr,
                    data_provider_addr=dp_addr, borrow_topic=topic
                )

            logger.info(f"Dynamically initialized protocol monitor: {name}")
            # Load cached borrowers for the newly added protocol
            self.monitors[name].load_borrowers_from_db()
