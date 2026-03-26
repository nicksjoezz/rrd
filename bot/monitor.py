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
import urllib.request as _urllib



from .zombie_queue import ZombieQueue
from .database import (
    upsert_borrowers, get_borrowers, upsert_position,
    get_last_scan_block, set_last_scan_block
)
from .velocity import VelocityTracker

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
                data = self.pool.functions.getUserAccountData(
                    checksum(user)
                ).call()

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

            # Only care about positions near or below threshold
            zombie_entry = cfg("strategy", "zombie_queue", "entry_hf")
            if hf > zombie_entry:
                return None

            # Find best collateral and debt token
            tokens       = self._get_best_tokens(user)
            if not tokens:
                return None

            col_token, col_symbol, col_bonus, debt_token, debt_symbol, debt_raw = tokens

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
            logger.debug(f"[{self.name}] check_position error for {user[:8]}: {e}")
            return None

    def _get_best_tokens(self, user: str):
        """
        Find the best collateral (highest USD value or highest bonus) and
        highest debt token to use in the liquidation call.
        Returns (col_addr, col_sym, col_bonus, debt_addr, debt_sym, debt_raw_wei) or None.
        """
        if not self.data_provider:
            return None

        token_map = get_token_map()
        best_col_value = 0
        best_col_bonus = 0
        best_col  = None
        best_debt_value = 0
        best_debt = None
        best_debt_raw = 0

        tokens = cfg("tokens")

        for sym, info in tokens.items():
            addr = info["address"]
            if not self.data_provider:
                return None

            try:
                rd = self.data_provider.functions.getUserReserveData(
                    checksum(addr), checksum(user)
                ).call()

                a_token_bal   = rd[0]  # collateral
                variable_debt = rd[2]  # debt
                decimals      = info["decimals"]
                bonus         = info["liquidation_bonus"]

                # Score collateral: prefer high-bonus assets when enabled
                if a_token_bal > 0:
                    col_usd = a_token_bal / (10 ** decimals)  # rough score
                    score   = col_usd * (1 + bonus) if cfg("strategy", "prioritize_high_bonus") else col_usd
                    if score > best_col_bonus:
                        best_col_bonus = score
                        best_col_value = col_usd
                        best_col = (addr, sym, bonus)

                # Track highest debt
                if variable_debt > 0:
                    debt_score = variable_debt / (10 ** decimals)
                    if debt_score > best_debt_value:
                        best_debt_value = debt_score
                        best_debt = (addr, sym)
                        best_debt_raw = variable_debt

            except Exception:
                continue

        if not best_col or not best_debt:
            return None

        return (
            best_col[0], best_col[1], best_col[2],
            best_debt[0], best_debt[1],
            best_debt_raw
        )

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
        
        w3 = get_web3()
        mc = w3.eth.contract(address=MULTICALL3_ADDR, abi=MULTICALL3_ABI)
        
        batch_size = cfg("scanning", "batch_size") or 500
        zombie_entry = cfg("strategy", "zombie_queue", "entry_hf")

        for i in range(0, len(users), batch_size):
            chunk = users[i : i + batch_size]
            calls = []
            
            # Step 1: Batch getUserAccountData
            for user in chunk:
                call_data = self.pool.encodeABI("getUserAccountData", [checksum(user)])
                calls.append({"target": self.pool.address, "callData": call_data})
            
            try:
                _, return_data = mc.functions.aggregate(calls).call()
                
                # Step 2: Process results
                for j, raw_res in enumerate(return_data):
                    user = chunk[j]
                    dec = w3.codec.decode(["uint256", "uint256", "uint256", "uint256", "uint256", "uint256"], raw_res)
                    
                    if dec[1] == 0: continue
                    
                    hf = health_factor_float(dec[5])
                    if hf > zombie_entry: continue
                    
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
        AAVE_V2_BORROW = "0xc6a898309e823ee50bac64e45ca8adba6690e99e7841c45d39871800d985639b"

        for name, pcfg in protocols.items():
            if not pcfg.get("enabled", False):
                continue
            pool_addr = pcfg.get("pool", "")
            dp_addr   = pcfg.get("data_provider", "")
            if not pool_addr or pool_addr.startswith("0x000"):
                logger.warning(f"Protocol {name} has no valid pool address -- skipping")
                continue
            
            # Default to V3 topic, override for Radiant (V2)
            topic = AAVE_V3_BORROW
            if "radiant" in name.lower():
                topic = AAVE_V2_BORROW

            self.monitors[name] = ProtocolMonitor(
                name=name,
                pool_addr=pool_addr,
                data_provider_addr=dp_addr,
                borrow_topic=topic
            )
            logger.info(f"Initialized protocol monitor: {name} (topic: {topic[:10]}...)")

    def load_all_borrowers(self):
        """
        Load borrowers using DB cache + incremental event scan.
        1. SQLite DB cache (instant restart recovery)
        2. Borrow event scan (initial 50-day window on first run)
        """
        w3            = get_web3()
        current_block = w3.eth.block_number

        # -- DB cache + incremental event scan for all protocols ----------------
        for name, monitor in self.monitors.items():
            monitor.load_borrowers_from_db()
            last_block = get_last_scan_block(name)

            # Arbitrum block speed: ~4 blocks/second = 345,600 blocks/day
            # Use 50 days (~17.28M blocks) for a thorough initial borrower list
            # Arbitrum block speed is approx 4 blocks/sec (345,600/day)
            ARBITRUM_50_DAYS = 17_280_000

            if last_block == 0:
                from_block = max(0, current_block - ARBITRUM_50_DAYS)
                logger.info(
                    f"[{name}] First run -- scanning last 50 days "
                    f"({ARBITRUM_50_DAYS:,} blocks on Arbitrum)"
                )
            else:
                from_block = last_block + 1
                logger.info(
                    f"[{name}] Incremental: blocks {from_block:,} -> {current_block:,}"
                )

        # -- Load manually added/persistent zombies into monitors ----------------
        zombies = self.zombie_queue.get_watching()
        for z in zombies:
            proto = z.get("protocol")
            user  = z.get("user")
            if proto and user and proto in self.monitors:
                self.monitors[proto]._borrowers.add(user.lower())
                logger.debug(f"[ZOMBIE] Loaded {user[:8]} from persistence into {proto} monitor")

        # -- Start incremental scan --------------------------------------------
        for name, monitor in self.monitors.items():
            last_block = get_last_scan_block(name)
            if last_block == 0:
                from_block = max(0, current_block - ARBITRUM_50_DAYS)
            else:
                from_block = last_block + 1
            
            if from_block < current_block:
                # Streaming callback: verifies borrowers as they are found
                def _streaming_callback(proto_name, users):
                    m = self.monitors.get(proto_name)
                    if not m: return
                    
                    found = m.scan_users(users, zombie_queue=self.zombie_queue)
                    if found:
                        logger.info(f"[{proto_name}] Streaming verification: {len(found)} at-risk positions found!")
                        # In the main loop, we'll need to handle these. 
                        # For now, scan_users already updates the zombie_queue.
                        for p in found:
                            if p.get("health_factor", 2.0) <= 1.0:
                                logger.warning(f"[STREAMS] LIQUIDATABLE: {p['user']} HF={p['health_factor']:.4f}")

                monitor.load_borrowers_from_events(from_block, current_block, on_batch_found=_streaming_callback)

    def refresh_borrowers(self):
        """Incremental update -- scan only new blocks found since last refresh."""
        w3 = get_web3()
        current_block = w3.eth.block_number

        for name, monitor in self.monitors.items():
            last_block = get_last_scan_block(name)
            if last_block == 0:
                # Fallback if first run didn't finish properly
                from_block = max(0, current_block - 1000)
            else:
                from_block = last_block + 1
            
            if from_block < current_block:
                monitor.load_borrowers_from_events(from_block, current_block)

    def scan_all_protocols(self) -> List[dict]:
        """
        Scan all enabled protocols. Returns list of liquidatable positions
        across all protocols, sorted by profit potential.
        Also records HF velocity for fast-falling detection.
        """
        all_positions = []

        for name, monitor in self.monitors.items():
            try:
                positions = monitor.scan_all(zombie_queue=self.zombie_queue)
                all_positions.extend(positions)
                logger.debug(f"[{name}] Found {len(positions)} liquidatable positions")
            except Exception as e:
                logger.error(f"[{name}] Scan error: {e}")

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

        return all_positions

    def get_stats(self) -> dict:
        total = sum(m.get_borrower_count() for m in self.monitors.values())
        return {
            "protocols":       list(self.monitors.keys()),
            "total_borrowers": total,
            "zombie_watching": self.zombie_queue.size(),
        }
