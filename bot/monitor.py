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
  4. Zombie queue integration for HF 0.95-1.15 pre-queuing
"""

import logging
import time
from typing import List, Optional
from web3 import Web3

from .utils import (
    get_web3, get_public_web3, get_alchemy_web3,
    cfg, get_token_map, checksum,
    wei_to_usd_base, health_factor_float,
    AAVE_POOL_ABI, DATA_PROVIDER_ABI,
    logger, notify,
    MULTICALL3_ADDR, MULTICALL3_ABI
)
from .swap_router import get_best_swap


from .database import (
    upsert_borrowers, get_borrowers, upsert_position,
    get_last_scan_block, set_last_scan_block, remove_position,
    remove_borrower
)
from .velocity import VelocityTracker
from .zombie_queue import ZombieQueue

logger = logging.getLogger("liquidation_bot.monitor")


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

        self.reserve_configs = {}

    def _get_reserve_config(self, token_addr: str):
        addr_l = token_addr.lower()
        if addr_l in self.reserve_configs:
            return self.reserve_configs[addr_l]

        if not self.data_provider: return None

        try:
            res = self.data_provider.functions.getReserveConfigurationData(checksum(token_addr)).call()
            config = {
                "decimals": res[0],
                "ltv": res[1] / 10000,
                "threshold": res[2] / 10000,
                "bonus": (res[3] - 10000) / 10000 if res[3] > 10000 else 0,
            }
            self.reserve_configs[addr_l] = config
            return config
        except Exception as e:
            logger.debug(f"[{self.name}] Reserve config fail for {token_addr[:10]}: {e}")
            return None

    def load_borrowers_from_db(self):
        """Load previously scanned borrowers from SQLite (fast restart)."""
        cached = get_borrowers(self.name)
        self._borrowers.update(cached)
        if cached:
            logger.info(f"[{self.name}] Loaded {len(cached):,} cached borrowers from DB")

    def load_borrowers_from_events(self, from_block: int, to_block: int, on_batch_found=None):
        """
        Scan Borrow event logs using raw eth_getLogs — NOT the web3 event helper.
        """
        w3    = get_public_web3()
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

    def _get_best_tokens_and_fresh_hf(self, user: str):
        """
        Calculates HF using fresh local prices and returns the best
        collateral/debt tokens for liquidation.
        """
        if not self.data_provider: return None
        from .profitability import get_token_price_usd

        best_col_score = 0
        best_col = None
        best_debt_score = 0
        best_debt = None
        total_fresh_weighted_col = 0
        total_fresh_debt = 0

        # 1. Get user configuration to see which assets they actually have
        try:
            # Aave V3: getUserConfiguration returns a bitmask of assets
            # Each pair of bits represents [isCollateral, isBorrowing]
            config_bits = self.pool.functions.getUserConfiguration(checksum(user)).call()
            if config_bits == 0: return None
        except Exception:
            config_bits = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF # Fallback to scan all if bitmask fails

        # We need to know which index corresponds to which asset
        # Aave V3 Pool: getReservesList() returns list of addresses in order
        try:
            reserves_list = self.pool.functions.getReservesList().call()
        except Exception:
            reserves_list = []

        # Universal asset support: iterate over ALL reserves in the pool
        for i, addr in enumerate(reserves_list):
            # Check bitmask: index i corresponds to bits i*2 and i*2+1
            is_using = (config_bits >> (i * 2)) & 3
            if not is_using: continue

            addr_l = addr.lower()
            try:
                rd = self.data_provider.functions.getUserReserveData(
                    checksum(addr), checksum(user)
                ).call()

                col_bal = rd[0] # currentATokenBalance
                debt_bal = rd[2] # currentVariableDebt
                if col_bal == 0 and debt_bal == 0: continue

                price = get_token_price_usd(addr)

                config = self._get_reserve_config(addr)
                if not config: continue
                decimals = config["decimals"]

                # Try to get symbol for logging
                sym = get_token_map().get(addr_l, {}).get("symbol", addr[:10])

                if col_bal > 0:
                    if price > 0:
                        usd_val = (col_bal / 10**decimals) * price
                        total_fresh_weighted_col += usd_val * config["threshold"]
                        score = usd_val * (1 + config["bonus"]) if cfg("strategy", "prioritize_high_bonus") else usd_val
                    else:
                        # We don't have a price, but we still need to pick a collateral
                        # Score it as 1 to avoid crash but indicate presence
                        score = 1

                    if score > best_col_score:
                        best_col_score = score
                        best_col = (addr, sym, config["bonus"])

                if debt_bal > 0:
                    if price > 0:
                        usd_val = (debt_bal / 10**decimals) * price
                        total_fresh_debt += usd_val
                        score = usd_val
                    else:
                        score = 1

                    if score > best_debt_score:
                        best_debt_score = score
                        best_debt = (addr, sym, debt_bal)
            except Exception: continue

        if not best_col or not best_debt or total_fresh_debt == 0:
            # If we don't have enough data for a fresh HF, return tokens but no HF
            if best_col and best_debt:
                 return {
                    "fresh_hf": None,
                    "col_token": best_col[0], "col_symbol": best_col[1], "col_bonus": best_col[2],
                    "debt_token": best_debt[0], "debt_symbol": best_debt[1], "debt_raw": best_debt[2]
                }
            return None

        fresh_hf = total_fresh_weighted_col / total_fresh_debt
        return {
            "fresh_hf": fresh_hf,
            "col_token": best_col[0], "col_symbol": best_col[1], "col_bonus": best_col[2],
            "debt_token": best_debt[0], "debt_symbol": best_debt[1], "debt_raw": best_debt[2]
        }

    def check_position(self, user: str, account_data: Optional[tuple] = None) -> Optional[dict]:
        """
        Check a single user's health factor. Returns position dict if liquidatable
        or approaching liquidation. Returns None if healthy.
        """
        try:
            if account_data:
                data = account_data
            else:
                data = self.pool.functions.getUserAccountData(checksum(user)).call()

            total_col_base  = data[0]
            total_debt_base = data[1]
            hf_raw          = data[5]

            if total_debt_base == 0:
                # Cleanup if it was previously recorded
                remove_position(self.name, user)
                return None

            # ── Real-Time Edge ──────────────────────────────────────────
            hf_onchain = health_factor_float(hf_raw)
            hf = hf_onchain
            best_info = None

            # Only do expensive fresh check if truly near liquidation
            if hf < 1.1:
                best_info = self._get_best_tokens_and_fresh_hf(user)
                if best_info and best_info["fresh_hf"] is not None:
                    hf = best_info["fresh_hf"]

            col_usd    = wei_to_usd_base(total_col_base)
            debt_usd   = wei_to_usd_base(total_debt_base)
            min_debt = cfg("strategy", "min_debt_usd")
            max_debt = cfg("strategy", "max_debt_usd")

            if debt_usd < min_debt or debt_usd > max_debt: return None

            if hf > 1.15 and hf_onchain > 1.15: return None

            if not best_info:
                best_info = self._get_best_tokens_and_fresh_hf(user)
            if not best_info: return None

            col_token    = best_info["col_token"]
            col_symbol   = best_info["col_symbol"]
            col_bonus    = best_info["col_bonus"]
            debt_token   = best_info["debt_token"]
            debt_symbol  = best_info["debt_symbol"]
            debt_raw     = best_info["debt_raw"]

            # Close factor: 100% if HF < 0.95 OR position < $2k, else 50%
            close_factor = cfg("strategy", "close_factor")
            if hf < 0.95 or debt_usd < 2000:
                close_factor = 1.0

            debt_to_cover = int(debt_raw * close_factor)
            _, _, swap_params = get_best_swap(col_token, debt_token, debt_to_cover)

            return {
                "protocol":          self.name,
                "user":              user,
                "address":           user,
                "collateral_token":  col_token,
                "collateral_symbol": col_symbol,
                "collateral_bonus":  col_bonus,
                "debt_token":        debt_token,
                "debt_symbol":       debt_symbol,
                "debt_to_cover":     debt_to_cover,
                "health_factor":     hf,
                "health_factor_onchain": hf_onchain,
                "total_debt_usd":    debt_usd,
                "total_col_usd":     col_usd,
                "pool_address":      self.pool_addr,
                "swap_params":       swap_params,
            }
        except Exception as e:
            logger.debug(f"[{self.name}] check_position error for {user[:8]}: {e}")
            return None

    def scan_all(self, zombie_queue: Optional[ZombieQueue] = None) -> List[dict]:
        """Scan all known borrowers."""
        return self.scan_users(list(self._borrowers), zombie_queue=zombie_queue)

    def scan_users(self, users: List[str], zombie_queue: Optional[ZombieQueue] = None) -> List[dict]:
        """
        Scan a specific list of borrowers using Multicall3.
        """
        liquidatable = []
        if not users: return []
        
        w3 = get_web3()
        mc = w3.eth.contract(address=MULTICALL3_ADDR, abi=MULTICALL3_ABI)
        
        batch_size = cfg("scanning", "batch_size") or 500

        for i in range(0, len(users), batch_size):
            chunk = users[i : i + batch_size]
            calls = []
            
            for user in chunk:
                call_data = self.pool.encodeABI("getUserAccountData", [checksum(user)])
                calls.append({"target": self.pool.address, "callData": call_data})
            
            try:
                _, return_data = mc.functions.aggregate(calls).call()
                
                for j, raw_res in enumerate(return_data):
                    user = chunk[j]
                    dec = w3.codec.decode(["uint256", "uint256", "uint256", "uint256", "uint256", "uint256"], raw_res)
                    
                    if dec[1] == 0:
                        if user in self._borrowers:
                            self._borrowers.remove(user)
                            remove_borrower(self.name, user)
                        if zombie_queue:
                            zombie_queue.remove(self.name, user)
                        remove_position(self.name, user)
                        continue
                    
                    hf = health_factor_float(dec[5])
                    # Watch everything up to 1.15
                    if hf > 1.2: continue # Slight buffer
                    
                    pos = self.check_position(user, account_data=dec)
                    if pos:
                        # Persist to categorized JSON
                        upsert_position(pos)

                        if zombie_queue:
                            result = zombie_queue.update(self.name, user, pos)
                            if result == "fire":
                                liquidatable.append(pos)
                        elif hf <= 1.0 or pos.get("health_factor_onchain", 2.0) <= 1.0:
                            liquidatable.append(pos)
                            
            except Exception as e:
                logger.error(f"[{self.name}] Multicall batch error: {e}")
                for user in chunk:
                    pos = self.check_position(user)
                    if pos:
                        upsert_position(pos)
                        if pos.get("health_factor", 2.0) <= 1.0: liquidatable.append(pos)

        return liquidatable

    def get_borrower_count(self) -> int:
        return len(self._borrowers)


from .zombie_queue import get_zombie_queue

class MultiProtocolMonitor:
    """
    Manages multiple ProtocolMonitor instances.
    """

    def __init__(self, on_liquidatable=None):
        self.monitors: dict = {}
        self.zombie_queue = get_zombie_queue(
            entry_hf=cfg("strategy", "zombie_queue", "entry_hf"),
            fire_hf=cfg("strategy",  "zombie_queue", "fire_hf")
        )
        self.on_liquidatable = on_liquidatable
        self.velocity = VelocityTracker()
        self._init_protocols()

    def _init_protocols(self):
        protocols = cfg("protocols")
        AAVE_V3_BORROW = "0xb3d084820fb1a9decffb176436bd02558d15fac9b0ddfed8c465bc7359d7dce0"
        AAVE_V2_BORROW = "0xc6a898309e823ee50bac64e45ca8adba6690e99e7841c45d39871800d985639b"

        for name, pcfg in protocols.items():
            if not pcfg.get("enabled", False): continue
            pool_addr = pcfg.get("pool", "")
            dp_addr   = pcfg.get("data_provider", "")
            if not pool_addr or pool_addr.startswith("0x000"): continue
            
            topic = AAVE_V3_BORROW
            if "radiant" in name.lower(): topic = AAVE_V2_BORROW

            self.monitors[name] = ProtocolMonitor(
                name=name,
                pool_addr=pool_addr,
                data_provider_addr=dp_addr,
                borrow_topic=topic
            )
            logger.info(f"Initialized protocol monitor: {name}")

    def load_all_borrowers(self):
        w3            = get_web3()
        current_block = w3.eth.block_number

        for name, monitor in self.monitors.items():
            monitor.load_borrowers_from_db()
            last_block = get_last_scan_block(name)
            ARBITRUM_50_DAYS = 17_280_000

            if last_block == 0:
                from_block = max(0, current_block - ARBITRUM_50_DAYS)
            else:
                from_block = last_block + 1

        for name, monitor in self.monitors.items():
            last_block = get_last_scan_block(name)
            if last_block == 0:
                from_block = max(0, current_block - ARBITRUM_50_DAYS)
            else:
                from_block = last_block + 1
            
            if from_block < current_block:
                def _streaming_callback(proto_name, users):
                    m = self.monitors.get(proto_name)
                    if not m: return
                    found = m.scan_users(users, zombie_queue=self.zombie_queue)
                    if found:
                        liquidatable = [p for p in found if p.get("health_factor", 2.0) <= 1.0]
                        if liquidatable and self.on_liquidatable:
                            self.on_liquidatable(liquidatable)
                        for pos in found:
                            upsert_position(pos)

                monitor.load_borrowers_from_events(from_block, current_block, on_batch_found=_streaming_callback)

    def refresh_borrowers(self):
        try:
            w3 = get_web3()
            current_block = w3.eth.block_number
        except Exception: return

        for name, monitor in self.monitors.items():
            last_block = get_last_scan_block(name)
            from_block = (last_block + 1) if last_block > 0 else (current_block - 1000)
            
            if from_block < current_block:
                def _streaming_callback(proto_name, users):
                    m = self.monitors.get(proto_name)
                    if not m: return
                    found = m.scan_users(users, zombie_queue=self.zombie_queue)
                    if found:
                        liquidatable = [p for p in found if p.get("health_factor", 2.0) <= 1.0]
                        if liquidatable and self.on_liquidatable:
                            self.on_liquidatable(liquidatable)
                        for pos in found:
                            upsert_position(pos)

                monitor.load_borrowers_from_events(from_block, current_block, on_batch_found=_streaming_callback)

    def scan_all_protocols(self) -> List[dict]:
        all_liquidatable = []

        # 1. Scan everything
        for name, monitor in self.monitors.items():
            try:
                liquidatable = monitor.scan_all(zombie_queue=self.zombie_queue)
                all_liquidatable.extend(liquidatable)
            except Exception as e:
                logger.error(f"[{name}] Scan error: {e}")

        # 2. Track velocity
        watching = self.zombie_queue.get_watching()
        for pos in watching:
            self.velocity.record(pos["protocol"], pos["user"], pos["health_factor"])

        # 3. Cleanup stale positions from JSON files
        # A position is stale if it's NOT in the current scan results (meaning HF > 1.15 or debt = 0)
        from .persistence import critical_store, zombies_store, watching_store
        now = time.time()
        # We use a longer timeout for cleanup to avoid flickering if a scan takes longer or fails once
        # Default interval is often 30-60s. 300s (5m) is a safe "stale" threshold.
        stale_threshold = max(300, cfg("scanning", "main_loop_interval_seconds") * 3)

        for store in [critical_store, zombies_store, watching_store]:
            stored_items = store.get_all_list()
            for item in stored_items:
                if now - item.get("last_updated", 0) > stale_threshold:
                    logger.info(f"[CLEANUP] Removing stale position {item.get('address','?')[:8]} "
                                f"from {item.get('protocol','?')} (last updated {int(now - item.get('last_updated',0))}s ago)")
                    remove_position(item['protocol'], item['address'])

        # 4. Ready to fire
        ready = self.zombie_queue.get_ready()
        for pos in ready:
            if pos not in all_liquidatable:
                all_liquidatable.append(pos)

        self.zombie_queue.evict_old()
        self.velocity.cleanup()

        return all_liquidatable

    def get_stats(self) -> dict:
        total = sum(m.get_borrower_count() for m in self.monitors.values())
        return {
            "protocols":       list(self.monitors.keys()),
            "total_borrowers": total,
            "zombie_watching": self.zombie_queue.size(),
        }
