"""
zombie_queue.py — Pre-queue positions with HF between entry_hf and fire_hf.
"""

import json
import os
import time
import logging
import threading
from typing import Optional
from .utils import ROOT_DIR

logger = logging.getLogger("liquidation_bot.zombie")

class ZombieQueue:
    """
    Tracks positions approaching liquidation.
    Entry at HF <= entry_hf (default 1.05)
    Fire  at HF <= fire_hf  (default 1.0)
    """

    def __init__(self, entry_hf: float = 1.05, fire_hf: float = 1.0, 
                 filename: str = None):
        self.entry_hf = entry_hf
        self.fire_hf  = fire_hf
        self.filename = filename or str(ROOT_DIR / "zombies.json")
        self._queue: dict = {}
        self._lock  = threading.Lock()
        self._load()

    def _load(self):
        if not os.path.exists(self.filename): return
        try:
            with open(self.filename, 'r') as f: data = json.load(f)
            def _decode(obj):
                if isinstance(obj, dict): return {k: _decode(v) for k, v in obj.items()}
                if isinstance(obj, list): return [_decode(x) for x in obj]
                if isinstance(obj, str) and obj.startswith("hex:"): return bytes.fromhex(obj[4:])
                return obj
            self._queue = _decode(data)
            logger.info(f"[ZOMBIE] Loaded {len(self._queue)} positions")
        except Exception as e: logger.error(f"[ZOMBIE] Load failed: {e}")

    def _save(self):
        class BytesEncoder(json.JSONEncoder):
            def default(self, obj):
                if isinstance(obj, bytes): return "hex:" + obj.hex()
                return super().default(obj)
        tmp = self.filename + ".tmp"
        try:
            with open(tmp, 'w') as f: json.dump(self._queue, f, indent=2, cls=BytesEncoder)
            os.replace(tmp, self.filename)
        except Exception as e: logger.error(f"[ZOMBIE] Save failed: {e}")

    def update(self, protocol: str, user: str, position: dict):
        key = f"{protocol}:{user.lower()}"; hf = position.get("health_factor", 99.0)
        with self._lock:
            if hf <= self.fire_hf:
                self._queue.pop(key, None); self._save(); return "fire"
            elif hf <= self.entry_hf:
                if key not in self._queue: logger.info(f"[ZOMBIE] Queued {user[:8]}... HF={hf:.4f} [{protocol}]")
                old = self._queue.get(key, {})
                self._queue[key] = {**position, "user": user, "protocol": protocol, "queued_at": old.get("queued_at", time.time()), "hf_at_entry": old.get("hf_at_entry", hf)}
                self._save(); return "watch"
            else:
                if key in self._queue: logger.info(f"[ZOMBIE] Recovered {user[:8]}..."); self._queue.pop(key, None); self._save()
                return None

    def get_ready(self) -> list:
        with self._lock: return [v for v in self._queue.values() if v.get("health_factor", 99) <= self.fire_hf]

    def get_watching(self) -> list:
        with self._lock: return list(self._queue.values())

    def size(self) -> int:
        with self._lock: return len(self._queue)

    def evict_old(self, max_age: int = 3600):
        now = time.time()
        with self._lock:
            before = len(self._queue)
            self._queue = {k: v for k, v in self._queue.items() if now - v.get("queued_at", now) < max_age}
            if len(self._queue) != before: self._save()
