"""
monitor.py -- Scans multiple lending protocols for liquidatable positions.
"""

import logging
import time
from typing import List, Optional, Dict
from web3 import Web3

from .utils import (
    get_web3, get_public_web3, cfg, get_token_map, checksum,
    wei_to_usd_base, health_factor_float,
    AAVE_POOL_ABI, DATA_PROVIDER_ABI, COMET_ABI, ERC20_ABI,
    logger, notify,
    MULTICALL3_ADDR, MULTICALL3_ABI
)
from .swap_router import get_best_swap
from .zombie_queue import ZombieQueue
from .database import (
    upsert_borrowers, get_borrowers, upsert_position,
    get_last_scan_block, set_last_scan_block
)
from .velocity import VelocityTracker

logger = logging.getLogger("liquidation_bot.monitor")

class BaseMonitor:
    def __init__(self, name: str, pool_addr: str):
        self.name = name; self.pool_addr = pool_addr
        self._borrowers: set = set(); self.w3 = get_web3()

    def load_borrowers_from_db(self):
        cached = get_borrowers(self.name)
        self._borrowers.update(cached)
        if cached: logger.info(f"[{self.name}] Loaded {len(cached):,} borrowers")

    def scan_users(self, users: List[str], zombie_queue: Optional[ZombieQueue] = None) -> List[dict]:
        raise NotImplementedError

class AaveMonitor(BaseMonitor):
    def __init__(self, name: str, pool_addr: str, dp_addr: str, topic: str):
        super().__init__(name, pool_addr); self.dp_addr = dp_addr; self.topic = topic
        self.pool = self.w3.eth.contract(address=checksum(pool_addr), abi=AAVE_POOL_ABI)
        self.dp   = self.w3.eth.contract(address=checksum(dp_addr), abi=DATA_PROVIDER_ABI) if dp_addr else None

    def load_borrowers_from_events(self, from_block: int, to_block: int):
        w3 = get_public_web3(); chunk = cfg("scanning", "event_scan_chunk") or 50000
        new_borrowers = set()
        for start in range(from_block, to_block, chunk):
            end = min(start + chunk - 1, to_block)
            try:
                logs = w3.eth.get_logs({"address": checksum(self.pool_addr), "topics": [self.topic], "fromBlock": start, "toBlock": end})
                for log in logs:
                    if len(log["topics"]) >= 3: new_borrowers.add(checksum("0x" + log["topics"][2].hex()[-40:]))
            except: pass
        self._borrowers.update(new_borrowers); set_last_scan_block(self.name, to_block)
        if new_borrowers: upsert_borrowers(list(new_borrowers), self.name)

    def scan_users(self, users: List[str], zombie_queue: Optional[ZombieQueue] = None) -> List[dict]:
        liq = []; mc = self.w3.eth.contract(address=MULTICALL3_ADDR, abi=MULTICALL3_ABI)
        batch = cfg("scanning", "batch_size") or 100; z_entry = cfg("strategy", "zombie_queue", "entry_hf") or 1.05
        for i in range(0, len(users), batch):
            chunk = users[i : i+batch]
            calls = [{"target": self.pool.address, "callData": self.pool.encodeABI("getUserAccountData", [checksum(u)])} for u in chunk]
            try:
                _, res = mc.functions.aggregate(calls).call()
                for j, raw in enumerate(res):
                    dec = self.w3.codec.decode(["uint256"]*6, raw); hf = health_factor_float(dec[5])
                    if hf <= z_entry and dec[1] > 0:
                        p = self._build_pos(chunk[j], dec)
                        if p:
                            if zombie_queue and zombie_queue.update(self.name, chunk[j], p) == "fire": liq.append(p)
                            elif hf <= 1.0: liq.append(p)
            except: pass
        return liq

    def _build_pos(self, user: str, data: tuple) -> Optional[dict]:
        tokens = self._get_tokens(user);
        if not tokens: return None
        col_t, col_s, col_b, debt_t, debt_s, debt_r = tokens; hf = health_factor_float(data[5])
        debt_usd = wei_to_usd_base(data[1]); cf = 1.0 if (hf < 0.95 or debt_usd < 2000) else (cfg("strategy", "close_factor") or 0.5)
        _, _, swap = get_best_swap(col_t, debt_t, int(debt_r * cf))
        return {
            "protocol": self.name, "user": user, "collateral_token": col_t, "collateral_symbol": col_s, "collateral_bonus": col_b,
            "debt_token": debt_t, "debt_symbol": debt_s, "debt_to_cover": int(debt_r * cf), "health_factor": hf,
            "total_debt_usd": debt_usd, "total_col_usd": wei_to_usd_base(data[0]), "pool_address": self.pool_addr, "swap_params": swap
        }

    def _get_tokens(self, user: str):
        if not self.dp: return None
        b_col = None; b_debt = None; m_col = 0; m_debt = 0; b_dr = 0
        tokens = cfg("tokens") or {}
        mc = self.w3.eth.contract(address=MULTICALL3_ADDR, abi=MULTICALL3_ABI)
        calls = []
        syms = list(tokens.keys())
        for s in syms:
            calls.append({"target": self.dp.address, "callData": self.dp.encodeABI("getUserReserveData", [checksum(tokens[s]["address"]), checksum(user)])})
        try:
            _, res = mc.functions.aggregate(calls).call()
            for j, raw in enumerate(res):
                info = tokens[syms[j]]; rd = self.w3.codec.decode(["uint256", "uint256", "uint256", "uint256", "uint256", "uint256", "uint256", "uint40", "bool"], raw)
                if rd[0] > 0 and (rd[0]/(10**info["decimals"])) > m_col:
                    m_col = rd[0]/(10**info["decimals"]); b_col = (info["address"], syms[j], info["liquidation_bonus"])
                if rd[2] > 0 and (rd[2]/(10**info["decimals"])) > m_debt:
                    m_debt = rd[2]/(10**info["decimals"]); b_debt = (info["address"], syms[j]); b_dr = rd[2]
        except: return None
        return (*b_col, *b_debt, b_dr) if b_col and b_debt else None

class CompoundMonitor(BaseMonitor):
    def __init__(self, name: str, pool_addr: str):
        super().__init__(name, pool_addr); self.comet = self.w3.eth.contract(address=checksum(pool_addr), abi=COMET_ABI)
        self.assets = []
        try:
            num = self.comet.functions.numAssets().call()
            for i in range(num):
                info = self.comet.functions.getAssetInfo(i).call()
                self.assets.append({
                    "address": info[1],
                    "priceFeed": info[2],
                    "scale": info[3],
                    "liqFactor": info[5] / 1e18
                })
        except: logger.error(f"[{name}] Failed to load assets")

    def load_borrowers_from_events(self, from_block: int, to_block: int):
        w3 = get_public_web3(); chunk = 50000; new = set()
        TOPIC_SUPPLY = "0xd1cfc6befeb23f07af82798e404b9989f668707a006c078864098939c0631e7c"
        for start in range(from_block, to_block, chunk):
            end = min(start + chunk - 1, to_block)
            try:
                logs = w3.eth.get_logs({"address": checksum(self.pool_addr), "topics": [TOPIC_SUPPLY], "fromBlock": start, "toBlock": end})
                for log in logs:
                    if len(log["topics"]) >= 3: new.add(checksum("0x" + log["topics"][2].hex()[-40:]))
            except: pass
        self._borrowers.update(new); set_last_scan_block(self.name, to_block)
        if new: upsert_borrowers(list(new), self.name)

    def scan_users(self, users: List[str], zombie_queue: Optional[ZombieQueue] = None) -> List[dict]:
        liq = []; mc = self.w3.eth.contract(address=MULTICALL3_ADDR, abi=MULTICALL3_ABI)
        z_entry = cfg("strategy", "zombie_queue", "entry_hf") or 1.05

        # We need prices for HF calculation
        prices = {}
        for a in self.assets:
            try: prices[a["address"]] = self.comet.functions.getPrice(a["priceFeed"]).call() / 1e8
            except: prices[a["address"]] = 0

        for i in range(0, len(users), 50):
            chunk = users[i : i+50]
            calls = []
            for u in chunk:
                calls.append({"target": self.pool_addr, "callData": self.comet.encodeABI("userBasic", [checksum(u)])})
                for a in self.assets:
                    calls.append({"target": self.pool_addr, "callData": self.comet.encodeABI("userCollateral", [checksum(u), checksum(a["address"])])})

            try:
                _, res = mc.functions.aggregate(calls).call()
                ptr = 0
                for u in chunk:
                    basic = self.w3.codec.decode(["int104", "uint152"], res[ptr]); ptr += 1
                    debt_raw = abs(basic[0]) if basic[0] < 0 else 0

                    total_col_usd = 0; total_bor_cap_usd = 0; best_col = None; max_col_val = 0
                    for a in self.assets:
                        col_raw = self.w3.codec.decode(["uint128", "uint128"], res[ptr])[0]; ptr += 1
                        if col_raw > 0:
                            val_usd = (col_raw / a["scale"]) * prices[a["address"]]
                            total_col_usd += val_usd
                            total_bor_cap_usd += val_usd * a["liqFactor"]
                            if val_usd > max_col_val:
                                max_col_val = val_usd
                                tokens = cfg("tokens") or {}
                                bonus = 0.05
                                for sym, info in tokens.items():
                                    if info["address"].lower() == a["address"].lower(): bonus = info["liquidation_bonus"]; break
                                best_col = (a["address"], bonus)

                    if debt_raw > 0:
                        # In Comet, HF = total_bor_cap_usd / debt_usd
                        # But Comet prices are usually vs USD or indexed.
                        # For simplicity, assume baseToken is 1 USD (it is for USDC Comet)
                        debt_usd = debt_raw / 1e6 # USDC 6 decimals
                        hf = total_bor_cap_usd / debt_usd if debt_usd > 0 else float('inf')

                        if hf <= z_entry:
                            p = self._build_pos_from_data(u, hf, debt_raw, total_col_usd, best_col)
                            if p:
                                if hf <= 1.0: liq.append(p)
                                elif zombie_queue: zombie_queue.update(self.name, u, p)
            except Exception as e: logger.debug(f"[{self.name}] scan_users batch error: {e}")
        return liq

    def _build_pos_from_data(self, user: str, hf: float, debt: int, total_col_usd: float, best_col: tuple) -> Optional[dict]:
        if not best_col: return None
        try:
            base = self.comet.functions.baseToken().call()
            token_map = get_token_map()
            col_info = token_map.get(best_col[0].lower(), {"symbol": "???", "liquidation_bonus": best_col[1]})
            base_info = token_map.get(base.lower(), {"symbol": "USDC"})

            return {
                "protocol": self.name, "user": user, "collateral_token": best_col[0], "collateral_symbol": col_info["symbol"],
                "collateral_bonus": col_info["liquidation_bonus"], "debt_token": base, "debt_symbol": base_info["symbol"],
                "debt_to_cover": debt, "health_factor": hf, "total_debt_usd": debt/1e6, "total_col_usd": total_col_usd,
                "pool_address": self.pool_addr, "swap_params": {}
            }
        except: return None

class MultiProtocolMonitor:
    def __init__(self):
        self.monitors: Dict[str, BaseMonitor] = {}
        self.zombie_queue = ZombieQueue(entry_hf=cfg("strategy", "zombie_queue", "entry_hf") or 1.05, fire_hf=cfg("strategy", "zombie_queue", "fire_hf") or 1.0)
        self.velocity = VelocityTracker(); self.w3 = get_web3(); self._init_protocols()

    def _init_protocols(self):
        A_TOPIC = "0xb3d084820fb1a9decffb176436bd02558d15fac9b0ddfed8c465bc7359d7dce0"
        for name, p in (cfg("protocols") or {}).items():
            if not p.get("enabled"): continue
            pool = p.get("pool")
            if not pool or pool.startswith("0x000"): continue
            if "aave" in name.lower() or "radiant" in name.lower(): self.monitors[name] = AaveMonitor(name, pool, p.get("data_provider"), A_TOPIC)
            elif "compound" in name.lower(): self.monitors[name] = CompoundMonitor(name, pool)
            logger.info(f"Initialized {name}")

    def load_all_borrowers(self):
        curr = self.w3.eth.block_number; limit = cfg("scanning", "blocks_to_scan_for_borrowers") or 1728000
        for name, m in self.monitors.items():
            m.load_borrowers_from_db()
            last = get_last_scan_block(name)
            start = max(0, curr - limit) if last == 0 else last + 1
            if start < curr:
                if hasattr(m, 'load_borrowers_from_events'): m.load_borrowers_from_events(start, curr)

    def refresh_borrowers(self):
        curr = self.w3.eth.block_number
        for name, m in self.monitors.items():
            last = get_last_scan_block(name)
            start = last + 1 if last > 0 else curr - 1000
            if start < curr:
                if hasattr(m, 'load_borrowers_from_events'): m.load_borrowers_from_events(start, curr)

    def scan_all_protocols(self) -> List[dict]:
        all_pos = []
        for name, m in self.monitors.items():
            try:
                pos = m.scan_users(list(m._borrowers), self.zombie_queue)
                all_pos.extend(pos)
            except Exception as e: logger.error(f"[{name}] Scan error: {e}")

        # Deduplicate and filter zero-debt positions
        all_pos = [p for p in all_pos if p.get("debt_to_cover", 0) > 0]

        for p in all_pos: self.velocity.record(p["protocol"], p["user"], p["health_factor"]); upsert_position(p)
        ready = self.zombie_queue.get_ready()
        for r in ready:
            if r not in all_pos: all_pos.append(r)
        self.zombie_queue.evict_old(); self.velocity.cleanup()
        return all_pos

    def get_stats(self) -> dict:
        return {"protocols": list(self.monitors.keys()), "total_borrowers": sum(len(m._borrowers) for m in self.monitors.values()), "zombie_watching": self.zombie_queue.size()}
