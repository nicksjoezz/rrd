"""
velocity.py — Health Factor velocity tracking.

Insight: A position dropping from HF 1.3 → 1.05 in 2 minutes is much more
likely to cross below 1.0 than one sitting at HF 1.05 stable for hours.

This module tracks HF over time and surfaces "fast-falling" positions
for priority scanning — giving you a head start before reactive bots notice.

This is a pure Python edge — no large bot bothers with this complexity.
"""

import time
import logging
from collections import defaultdict, deque
from typing import Optional, List

logger = logging.getLogger("liquidation_bot.velocity")

# Keep N samples per position for velocity calculation
MAX_SAMPLES = 10
# How old a sample can be before it's discarded (seconds)
MAX_SAMPLE_AGE = 600  # 10 minutes


class VelocityTracker:
    """
    Tracks health factor history per position.
    Computes HF velocity (change per minute).
    Identifies positions on a fast downward trajectory.
    """

    def __init__(self):
        # {key -> deque of (timestamp, hf)}
        self._history: dict = defaultdict(lambda: deque(maxlen=MAX_SAMPLES))

    def record(self, protocol: str, user: str, hf: float):
        """Record a health factor observation."""
        key = f"{protocol}:{user.lower()}"
        now = time.time()

        history = self._history[key]

        # Purge old samples
        while history and (now - history[0][0]) > MAX_SAMPLE_AGE:
            history.popleft()

        history.append((now, hf))

    def get_velocity(self, protocol: str, user: str) -> Optional[float]:
        """
        Returns HF change per minute (negative = falling).
        Returns None if not enough data.
        """
        key     = f"{protocol}:{user.lower()}"
        history = self._history.get(key)

        if not history or len(history) < 2:
            return None

        oldest_ts, oldest_hf = history[0]
        newest_ts, newest_hf = history[-1]

        elapsed_min = (newest_ts - oldest_ts) / 60.0
        if elapsed_min < 0.1:
            return None

        return (newest_hf - oldest_hf) / elapsed_min  # negative = falling

    def time_to_liquidation(self, protocol: str, user: str, current_hf: float) -> Optional[float]:
        """
        Estimate minutes until HF reaches 1.0 at current velocity.
        Returns None if velocity is positive or flat.
        """
        v = self.get_velocity(protocol, user)
        if v is None or v >= 0:
            return None
        if current_hf <= 1.0:
            return 0.0

        distance = current_hf - 1.0
        return distance / abs(v)

    def get_fast_falling(self, positions: list, velocity_threshold: float = -0.05) -> List[dict]:
        """
        Filter positions that are falling faster than threshold HF/minute.
        Default: positions losing more than 0.05 HF per minute.

        Example: HF 1.2 → 1.1 in 2 minutes = -0.05/min → flagged
        """
        flagged = []
        for pos in positions:
            user     = pos.get("user", "")
            protocol = pos.get("protocol", "")
            hf       = pos.get("health_factor", 99)

            v   = self.get_velocity(protocol, user)
            ttl = self.time_to_liquidation(protocol, user, hf)

            if v is not None and v <= velocity_threshold:
                pos["hf_velocity"]          = round(v, 5)
                pos["est_minutes_to_liq"]   = round(ttl, 1) if ttl is not None else None
                flagged.append(pos)
                logger.info(
                    f"[VELOCITY] {user[:10]}... HF={hf:.4f} "
                    f"velocity={v:+.4f}/min "
                    f"TTL={'%.1f min' % ttl if ttl else '?'} "
                    f"[{protocol}]"
                )

        return sorted(flagged, key=lambda x: x.get("est_minutes_to_liq") or 999)

    def cleanup(self):
        """Remove stale entries from tracker."""
        now     = time.time()
        stale   = []
        for key, history in self._history.items():
            if history and (now - history[-1][0]) > MAX_SAMPLE_AGE * 3:
                stale.append(key)
        for k in stale:
            del self._history[k]
        if stale:
            logger.debug(f"Velocity tracker: removed {len(stale)} stale entries")
