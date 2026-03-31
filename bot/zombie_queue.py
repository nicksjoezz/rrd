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
_zq_lock   = threading.Lock()

def get_zombie_queue(entry_hf: float = 1.05, fire_hf: float = 1.0) -> 'ZombieQueue':
    global _shared_zq
    with _zq_lock:
        if _shared_zq is None:
            _shared_zq = ZombieQueue(entry_hf=entry_hf, fire_hf=fire_hf)
        return _shared_zq


class ZombieQueue:
    """
    Tracks positions approaching liquidation.
    Entry at HF <= entry_hf (default 1.05)
    Fire  at HF <= fire_hf  (default 1.0)
    
    In-memory tracker. Persistence is handled by the monitor's call to upsert_position.
    """

    def __init__(self, entry_hf: float = 1.05, fire_hf: float = 1.0):
        self.entry_hf = entry_hf
        self.fire_hf  = fire_hf
        self._queue: dict = {}    # address+protocol -> position data
        self._lock  = threading.Lock()

    def update(self, protocol: str, user: str, position: dict):
        """
        Called every scan cycle. Updates or inserts position in queue.
        Returns "fire" if position is ready for liquidation, else "watch" or None.
        """
        key = f"{protocol}:{user.lower()}"
        # Use injected _queue_hf if present, otherwise fallback
        hf  = position.get("_queue_hf", position.get("health_factor", 99.0))

        with self._lock:
            if hf <= self.fire_hf:
                # Ready to liquidate — pop from queue and return
                self._queue.pop(key, None)
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
                return "watch"

            else:
                # Healthy — remove from queue
                if key in self._queue:
                    logger.info(f"[ZOMBIE] Recovered {user[:8]}... HF={hf:.4f} -- removed from queue")
                    self._queue.pop(key, None)
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

    def remove(self, protocol: str, user: str):
        """Manually remove a user from the queue (e.g. if position closed)."""
        key = f"{protocol}:{user.lower()}"
        with self._lock:
            if key in self._queue:
                self._queue.pop(key)

    def evict_old(self, max_age_seconds: int = 3600):
        """Remove positions that have been in queue > max_age (likely recovered)."""
        now = time.time()
        with self._lock:
            stale = [k for k, v in self._queue.items()
                     if now - v.get("queued_at", now) > max_age_seconds]
            for k in stale:
                logger.debug(f"[ZOMBIE] Evicted stale entry: {k}")
                del self._queue[k]

    def summary(self) -> str:
        with self._lock:
            if not self._queue:
                return "Zombie queue: empty"
            lines = [f"Zombie queue ({len(self._queue)} positions):"]
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
