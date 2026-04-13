"""
persistence.py — JSON-based persistence layer for LiqBot.

Categorized position storage:
  - critical.json : Positions with HF < 1.0 (Liquidatable).
  - zombies.json  : Positions with HF 1.0 - 1.05 (Danger).
  - watching.json : Positions with HF 1.05 - 1.15 (Watching).
  - borrowers.json: All known wallet addresses per protocol.
  - history.json  : Full liquidation history with profit data.
  - state.json    : Last scanned block per protocol.

All files are stored in the logs/ directory.
"""

import json
import os
import threading
import time
from pathlib import Path
from typing import Dict, List, Any, Optional

ROOT_DIR = Path(__file__).parent.parent
LOG_DIR = ROOT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

class JsonStore:
    def __init__(self, filename: str):
        self.path = LOG_DIR / filename
        self._lock = threading.RLock()
        self._data: Any = self._load_initial()

    def _load_initial(self) -> Any:
        if not self.path.exists():
            return {}
        try:
            with self._lock:
                with open(self.path, "r") as f:
                    data = json.load(f)
                    return data if data is not None else {}
        except Exception:
            return {}

    def save(self):
        """Atomic save to disk."""
        tmp_path = self.path.with_suffix(".tmp")
        try:
            with self._lock:
                with open(tmp_path, "w") as f:
                    json.dump(self._data, f, indent=2)
                if self.path.exists():
                    os.remove(self.path)
                os.rename(tmp_path, self.path)
        except Exception as e:
            if tmp_path.exists():
                os.remove(tmp_path)
            raise e

    def get_all(self) -> Any:
        with self._lock:
            return self._data

# ── Borrowers ─────────────────────────────────────────────────────────────────
class BorrowerStore(JsonStore):
    def add_borrowers(self, protocol: str, addresses: List[str]):
        with self._lock:
            if protocol not in self._data:
                self._data[protocol] = []

            existing = set(self._data[protocol])
            new_addrs = [a.lower() for a in addresses if a.lower() not in existing]
            if new_addrs:
                self._data[protocol].extend(new_addrs)
                self.save()

    def get_borrowers(self, protocol: str) -> List[str]:
        with self._lock:
            return self._data.get(protocol, [])

    def get_total_count(self) -> int:
        with self._lock:
            all_addrs = set()
            for addrs in self._data.values():
                all_addrs.update(addrs)
            return len(all_addrs)

    def remove_borrower(self, protocol: str, address: str):
        with self._lock:
            addr_l = address.lower()
            changed = False
            for proto in self._data:
                if addr_l in self._data[proto]:
                    self._data[proto].remove(addr_l)
                    changed = True
            if changed:
                self.save()

# ── History ───────────────────────────────────────────────────────────────────
class HistoryStore(JsonStore):
    def _load_initial(self) -> Any:
        if not self.path.exists():
            return []
        try:
            with self._lock:
                with open(self.path, "r") as f:
                    data = json.load(f)
                    return data if isinstance(data, list) else []
        except Exception:
            return []

    def record_liquidation(self, record: Dict[str, Any]):
        with self._lock:
            if any(r.get("tx_hash") == record.get("tx_hash") for r in self._data):
                return

            record["timestamp"] = record.get("timestamp") or int(time.time())
            self._data.append(record)
            if len(self._data) > 1000:
                self._data = self._data[-1000:]
            self.save()

    def get_stats(self, total_borrowers: int = 0) -> Dict[str, Any]:
        with self._lock:
            total_liqs = len(self._data)
            total_profit = sum(float(r.get("estimated_profit", 0)) for r in self._data)

            today_ts = int(time.time()) - 86400
            today_liqs = [r for r in self._data if r.get("timestamp", 0) > today_ts]

            return {
                "total_liquidations": total_liqs,
                "total_profit_est_usd": round(total_profit, 2),
                "today_liquidations": len(today_liqs),
                "today_profit_est_usd": round(sum(float(r.get("estimated_profit", 0)) for r in today_liqs), 2),
                "total_borrowers": total_borrowers,
                "recent": sorted(self._data, key=lambda x: x.get("timestamp", 0), reverse=True)[:20]
            }

# ── Scan State ────────────────────────────────────────────────────────────────
class StateStore(JsonStore):
    def set_last_block(self, protocol: str, block: int):
        with self._lock:
            self._data[protocol] = block
            self.save()

    def get_last_block(self, protocol: str) -> int:
        with self._lock:
            return self._data.get(protocol, 0)

# ── Categorized Position Stores ───────────────────────────────────────────────
class CategorizedPositionStore(JsonStore):
    def upsert(self, pos: Dict[str, Any]):
        with self._lock:
            addr = pos.get('address') or pos.get('user')
            if not addr: return
            key = f"{pos.get('protocol')}:{addr.lower()}"
            # Ensure address field is present
            pos["address"] = addr
            pos["last_updated"] = int(time.time())
            self._data[key] = pos
            self.save()

    def remove(self, protocol: str, user: str):
        with self._lock:
            key = f"{protocol}:{user.lower()}"
            if key in self._data:
                del self._data[key]
                self.save()

    def get_all_list(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._data.values())

# Singletons
borrowers = BorrowerStore("borrowers.json")
history   = HistoryStore("history.json")
state     = StateStore("state.json")

critical_store = CategorizedPositionStore("critical.json")
zombies_store  = CategorizedPositionStore("zombies.json")
watching_store = CategorizedPositionStore("watching.json")

# Compatibility layer for legacy positions.json
positions = CategorizedPositionStore("positions.json")

def cleanup_categorized_positions(protocol: str, user: str, current_category: str):
    """Ensures a position only exists in ONE category file."""
    for cat_name, store in [("critical", critical_store), ("zombie", zombies_store), ("watching", watching_store)]:
        if cat_name != current_category:
            store.remove(protocol, user)
    # Also remove from legacy positions.json
    positions.remove(protocol, user)

def save_categorized_position(pos: Dict[str, Any]):
    """Smart router to save position into correct JSON file based on HF."""
    hf = float(pos.get("health_factor", 9.9))
    proto = pos.get("protocol")
    user = pos.get("address") or pos.get("user")

    if not proto or not user: return

    category = "other"
    if hf < 1.0:
        category = "critical"
        pos["is_zombie"] = False
        critical_store.upsert(pos)
    elif hf < 1.05:
        category = "zombie"
        pos["is_zombie"] = True
        zombies_store.upsert(pos)
    elif hf < 1.15:
        category = "watching"
        pos["is_zombie"] = False
        watching_store.upsert(pos)
    else:
        # If HF recovered > 1.15, it should be in any of these files
        pass

    cleanup_categorized_positions(proto, user, category)
