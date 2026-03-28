"""
zombie_queue.py — Pre-queue positions with HF between entry_hf and fire_hf.
These are positions that big bots ignore. We watch them closely and fire
the moment they cross below the liquidation threshold.

Gap exploited: 
  - Thousands of wallets persist between HF 0.95–1.00 unliquidated
  - On Arbitrum at $0.05 gas, these are profitable even at small sizes
"""

import json
import os
import time
import logging
import threading
from typing import Optional

logger = logging.getLogger("liquidation_bot.zombie")

_shared_zq = None

def get_zombie_queue(entry_hf: float = 1.05, fire_hf: float = 1.0) -> 'ZombieQueue':
    global _shared_zq
    if _shared_zq is None:
        _shared_zq = ZombieQueue(entry_hf=entry_hf, fire_hf=fire_hf)
    return _shared_zq


class ZombieQueue:
    """
    Tracks positions approaching liquidation.
    Entry at HF <= entry_hf (default 1.05)
    Fire  at HF <= fire_hf  (default 1.0)
    
    Persistent version — saves to zombies.json
    """

    def __init__(self, entry_hf: float = 1.05, fire_hf: float = 1.0, 
                 filename: str = "zombies.json"):
        self.entry_hf = entry_hf
        self.fire_hf  = fire_hf
        self.filename = filename
        self._queue: dict = {}    # address+protocol -> position data
        self._lock  = threading.Lock()
        self._load()

    def _load(self):
        """Load queue from disk on startup."""
        if not os.path.exists(self.filename):
            return
        try:
            with open(self.filename, 'r') as f:
                data = json.load(f)
            
            # Convert "hex:..." back to bytes
            def _decode_hex(obj):
                if isinstance(obj, dict):
                    return {k: _decode_hex(v) for k, v in obj.items()}
                if isinstance(obj, list):
                    return [_decode_hex(x) for x in obj]
                if isinstance(obj, str) and obj.startswith("hex:"):
                    return bytes.fromhex(obj[4:])
                return obj

            self._queue = _decode_hex(data)
            logger.info(f"[ZOMBIE] Loaded {len(self._queue)} positions from {self.filename}")
        except Exception as e:
            logger.error(f"[ZOMBIE] Failed to load {self.filename}: {e}")

    def _save(self):
        """Save queue to disk atomically."""
        class BytesEncoder(json.JSONEncoder):
            def default(self, obj):
                if isinstance(obj, bytes):
                    return "hex:" + obj.hex()
                return super().default(obj)

        tmp_file = self.filename + ".tmp"
        try:
            with open(tmp_file, 'w') as f:
                json.dump(self._queue, f, indent=2, cls=BytesEncoder)
            # Atomic rename (on Windows this requires os.replace or deleting first)
            if os.path.exists(self.filename):
                os.remove(self.filename)
            os.rename(tmp_file, self.filename)
        except Exception as e:
            logger.error(f"[ZOMBIE] Failed to save {self.filename}: {e}")
            if os.path.exists(tmp_file):
                try: os.remove(tmp_file)
                except: pass

    def update(self, protocol: str, user: str, position: dict):
        """
        Called every scan cycle. Updates or inserts position in queue.
        Returns "fire" if position is ready for liquidation, else None.
        """
        key = f"{protocol}:{user.lower()}"
        hf  = position.get("health_factor", 99.0)

        with self._lock:
            if hf <= self.fire_hf:
                # Ready to liquidate — pop from queue and return
                self._queue.pop(key, None)
                self._save()
                return "fire"

            elif hf <= self.entry_hf:
                # Watch closely
                if key not in self._queue:
                    logger.info(
                        f"[ZOMBIE] Queued {user[:8]}... HF={hf:.4f} "
                        f"debt=${position.get('total_debt_usd',0):.0f} "
                        f"[{protocol}]"
                    )
                old_entry = self._queue.get(key, {})
                self._queue[key] = {
                    **position,
                    "user": user,
                    "protocol": protocol,
                    "queued_at": old_entry.get("queued_at", time.time()),
                    "hf_at_entry": old_entry.get("hf_at_entry", hf)
                }
                self._save()
                return "watch"

            else:
                # Healthy — remove from queue
                if key in self._queue:
                    logger.info(f"[ZOMBIE] Recovered {user[:8]}... HF={hf:.4f} -- removed from queue")
                    self._queue.pop(key, None)
                    self._save()
                return None

    def get_ready(self) -> list:
        """Return all positions currently ready to liquidate (HF <= fire_hf)."""
        with self._lock:
            return [v for v in self._queue.values() if v.get("health_factor", 99) <= self.fire_hf]

    def get_watching(self) -> list:
        """Return all positions being watched (between entry_hf and fire_hf)."""
        with self._lock:
            return list(self._queue.values())

    def size(self) -> int:
        with self._lock:
            return len(self._queue)

    def evict_old(self, max_age_seconds: int = 3600):
        """Remove positions that have been in queue > max_age (likely recovered)."""
        now = time.time()
        with self._lock:
            before = len(self._queue)
            stale = [k for k, v in self._queue.items()
                     if now - v.get("queued_at", now) > max_age_seconds]
            for k in stale:
                logger.debug(f"[ZOMBIE] Evicted stale entry: {k}")
                del self._queue[k]
            
            if len(self._queue) != before:
                self._save()

    def summary(self) -> str:
        with self._lock:
            if not self._queue:
                return "Zombie queue: empty"
            lines = [f"Zombie queue ({len(self._queue)} positions):"]
            # Cast to list of items to help linter with dictionary types
            items = list(self._queue.items())
            items.sort(key=lambda x: float(x[1].get("health_factor", 99)))
            for k, v in items:
                u_str = str(v.get('user','?'))[:10]
                hf_val = float(v.get('health_factor', 0))
                debt_val = float(v.get('total_debt_usd', 0))
                lines.append(
                    f"  {u_str} HF={hf_val:.4f} "
                    f"debt=${debt_val:.0f} [{v.get('protocol','?')}]"
                )
            return "\n".join(lines)
