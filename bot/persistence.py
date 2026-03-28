"""
persistence.py — JSON-based persistence layer for LiqBot.

Replaces SQLite (bot.db) with simpler, cache-friendly JSON files:
  - borrowers.json : All known wallet addresses per protocol.
  - history.json   : Full liquidation history with profit data.
  - state.json     : Last scanned block per protocol.
  - positions.json : Current tracking list with HFs and debt.

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
        self._data: Any = self._load_initial()
        self._lock = threading.RLock()

    def _load_initial(self) -> Any:
        if not self.path.exists():
            return {} if ".json" in self.path.name else []
        try:
            with self._lock:
                with open(self.path, "r") as f:
                    return json.load(f)
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
    def __init__(self):
        super().__init__("borrowers.json")
        if not isinstance(self._data, dict): self._data = {}

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

# ── History ───────────────────────────────────────────────────────────────────
class HistoryStore(JsonStore):
    def __init__(self):
        super().__init__("history.json")
        if not isinstance(self._data, list): self._data = []

    def record_liquidation(self, record: Dict[str, Any]):
        with self._lock:
            # Check for duplicate tx_hash
            if any(r.get("tx_hash") == record.get("tx_hash") for r in self._data):
                return

            record["timestamp"] = record.get("timestamp") or int(time.time())
            self._data.append(record)
            # Keep only last 1000 for performance
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
    def __init__(self):
        super().__init__("state.json")
        if not isinstance(self._data, dict): self._data = {}

    def set_last_block(self, protocol: str, block: int):
        with self._lock:
            self._data[protocol] = block
            self.save()

    def get_last_block(self, protocol: str) -> int:
        with self._lock:
            return self._data.get(protocol, 0)

# ── Positions ─────────────────────────────────────────────────────────────────
class PositionStore(JsonStore):
    def __init__(self):
        super().__init__("positions.json")
        if not isinstance(self._data, dict): self._data = {}

    def upsert_position(self, pos: Dict[str, Any]):
        with self._lock:
            key = f"{pos.get('protocol')}:{pos.get('user', '').lower()}"
            pos["last_updated"] = int(time.time())
            self._data[key] = pos
            self.save()

    def get_all_positions(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._data.values())

    def remove_position(self, protocol: str, user: str):
        with self._lock:
            key = f"{protocol}:{user.lower()}"
            if key in self._data:
                del self._data[key]
                self.save()

# Singletons
borrowers = BorrowerStore()
history   = HistoryStore()
state     = StateStore()
positions = PositionStore()
