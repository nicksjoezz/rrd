"""
auto_tuner.py — Adaptive parameter tuning.

The bot starts with config defaults, but over time it learns:
  - What minimum profit threshold actually produces wins on THIS chain
  - Whether the current gas cap is competitive
  - Which collateral types are most reliable to liquidate
  - When NOT to execute (e.g. during gas spikes, low success rate periods)

Every 50 cycles, the tuner reviews performance from the DB and
may adjust these live parameters (without restarting):
  - effective_min_profit_usd
  - effective_gas_cap_gwei
  - collateral_skip_list (assets with consistent swap failures)
  - scan_interval (speed up during volatile markets)

Changes are in-memory only — config.json is never modified.
The tuner logs every change with its reasoning.
"""

import logging
import time
from typing import Optional
from .database import get_conn, get_stats
from .utils import cfg

logger = logging.getLogger("liquidation_bot.tuner")


class AutoTuner:
    """
    Monitors bot performance and adjusts operational parameters.
    All adjustments are in-memory — config.json is never touched.
    """

    def __init__(self):
        # Start with config values
        self.min_profit_usd:    float = cfg("strategy", "min_profit_usd")
        self.gas_cap_gwei:      float = cfg("gas", "max_fee_per_gas_gwei")
        self.scan_interval:     int   = cfg("scanning", "main_loop_interval_seconds")
        self.collateral_skip:   set   = set()   # tokens with repeated swap failures
        self.rival_gas_prices:  list  = []      # last N rival gas prices seen

        self._cycle_count:      int   = 0
        self._last_tune_cycle:  int   = 0
        self._tune_every:       int   = 50      # tune every N cycles
        self._failed_swaps:     dict  = {}      # symbol → failure count
        self._recent_wins:      list  = []      # timestamps of successful liquidations
        self._recent_scans:     list  = []      # timestamps of scan cycles

        logger.info(
            f"[TUNER] Initialized | "
            f"min_profit=${self.min_profit_usd} | "
            f"gas_cap={self.gas_cap_gwei} gwei | "
            f"interval={self.scan_interval}s"
        )

    def record_cycle(self):
        """Call once per main loop cycle."""
        self._cycle_count += 1
        self._recent_scans.append(time.time())
        # Keep only last 200 scans
        if len(self._recent_scans) > 200:
            self._recent_scans = self._recent_scans[-200:]

    def record_success(self, collateral_symbol: str, profit_usd: float):
        """Call after a successful liquidation."""
        self._recent_wins.append({
            "ts":       time.time(),
            "profit":   profit_usd,
            "symbol":   collateral_symbol,
        })
        if len(self._recent_wins) > 100:
            self._recent_wins = self._recent_wins[-100:]

    def record_swap_failure(self, collateral_symbol: str):
        """Call when a swap reverts (indicates thin liquidity for that asset)."""
        self._failed_swaps[collateral_symbol] = \
            self._failed_swaps.get(collateral_symbol, 0) + 1
        fails = self._failed_swaps[collateral_symbol]
        if fails >= 3 and collateral_symbol not in self.collateral_skip:
            self.collateral_skip.add(collateral_symbol)
            logger.warning(
                f"[TUNER] ⚠️  Added {collateral_symbol} to skip list after {fails} swap failures. "
                f"Consider investigating pool liquidity or adjusting fee tier."
            )

    def record_revert(self, reason: str):
        """Call when a liquidation tx reverts."""
        if "slippage" in reason.lower() or "insufficient" in reason.lower():
            logger.info("[TUNER] Swap slippage revert -- noting for parameter review")

    def record_rival_gas(self, gas_gwei: float):
        """Call when we lose to a rival to track their bidding patterns."""
        self.rival_gas_prices.append(gas_gwei)
        if len(self.rival_gas_prices) > 20:
            self.rival_gas_prices.pop(0)
        logger.info(f"[TUNER] Recorded rival gas: {gas_gwei:.2f} gwei (avg of last {len(self.rival_gas_prices)}: {sum(self.rival_gas_prices)/len(self.rival_gas_prices):.2f})")

    def should_tune(self) -> bool:
        return (self._cycle_count - self._last_tune_cycle) >= self._tune_every

    def tune(self):
        """
        Review performance and adjust parameters if needed.
        Called automatically every _tune_every cycles.
        """
        if not self.should_tune():
            return

        self._last_tune_cycle = self._cycle_count
        logger.info(f"[TUNER] Reviewing performance at cycle #{self._cycle_count}...")

        db_stats = get_stats()
        self._tune_min_profit(db_stats)
        self._tune_gas_cap()
        self._tune_scan_interval()
        self._report()

    def _tune_min_profit(self, db_stats: dict):
        """
        Adjust min_profit_usd based on actual liquidation outcomes.
        
        Logic:
          - If success rate is low (many attempts, few wins), raise the bar
          - If we're not finding any opportunities, lower the bar slightly
          - Bounded by config min/max (never goes below $5, never above $200)
        """
        config_min = cfg("strategy", "min_profit_usd")
        config_max = config_min * 10
        
        total = db_stats.get("total_liquidations", 0)
        recent_wins = [w for w in self._recent_wins 
                       if time.time() - w["ts"] < 3600]  # last hour

        if total == 0:
            # No history yet — keep default
            return

        if len(recent_wins) == 0 and self._cycle_count > 100:
            # No wins in last hour and we've been running a while
            # Maybe min_profit is too high — lower it by 10%
            new_val = max(5.0, self.min_profit_usd * 0.9)
            if new_val != self.min_profit_usd:
                logger.info(
                    f"[TUNER] ↓ min_profit: ${self.min_profit_usd:.1f} → ${new_val:.1f} "
                    f"(no wins in last hour)"
                )
                self.min_profit_usd = new_val
        elif len(recent_wins) >= 10:
            # Many wins — can afford to be more selective
            avg_profit = sum(w["profit"] for w in recent_wins) / len(recent_wins)
            new_val = min(config_max, self.min_profit_usd * 1.15)
            if new_val != self.min_profit_usd and avg_profit > self.min_profit_usd * 2:
                logger.info(
                    f"[TUNER] ↑ min_profit: ${self.min_profit_usd:.1f} → ${new_val:.1f} "
                    f"(high win rate, avg profit=${avg_profit:.1f})"
                )
                self.min_profit_usd = new_val

    def _tune_gas_cap(self):
        """
        Check if our gas cap is competitive.
        If we're consistently losing to other bots, raise it slightly.
        """
        from .gas_manager import get_current_base_fee
        config_max = cfg("gas", "max_fee_per_gas_gwei")
        
        try:
            current_base = get_current_base_fee() / 1e9  # gwei
            
            # If we've seen rivals recently, use their average + buffer
            if self.rival_gas_prices:
                avg_rival = sum(self.rival_gas_prices) / len(self.rival_gas_prices)
                if avg_rival > self.gas_cap_gwei * 0.9:
                    new_cap = min(config_max * 5, avg_rival * 1.5)
                    if new_cap > self.gas_cap_gwei:
                        logger.info(
                            f"[TUNER] ↑ gas_cap: {self.gas_cap_gwei:.3f} → {new_cap:.3f} gwei "
                            f"(rivals seen bidding at {avg_rival:.3f} avg)"
                        )
                        self.gas_cap_gwei = new_cap
                        return

            # If base fee > 80% of our cap, we're cutting it close
            if current_base > self.gas_cap_gwei * 0.8:
                new_cap = min(config_max * 3, current_base * 2.0)
                if new_cap > self.gas_cap_gwei:
                    logger.info(
                        f"[TUNER] ↑ gas_cap: {self.gas_cap_gwei:.3f} → {new_cap:.3f} gwei "
                        f"(base fee at {current_base:.3f} gwei)"
                    )
                    self.gas_cap_gwei = new_cap
            elif current_base < self.gas_cap_gwei * 0.2 and not self.rival_gas_prices:
                # Very cheap gas — we can lower cap to save margin
                new_cap = max(config_max * 0.5, current_base * 3.0)
                if new_cap < self.gas_cap_gwei:
                    logger.info(
                        f"[TUNER] ↓ gas_cap: {self.gas_cap_gwei:.3f} → {new_cap:.3f} gwei "
                        f"(gas is cheap, optimizing)"
                    )
                    self.gas_cap_gwei = new_cap
        except Exception:
            pass

    def _tune_scan_interval(self):
        """
        Speed up scanning when market is volatile (more liquidations happening),
        slow down when market is quiet (save RPC calls).
        """
        config_interval = cfg("scanning", "main_loop_interval_seconds")
        recent_wins = [w for w in self._recent_wins 
                       if time.time() - w["ts"] < 1800]  # last 30 min

        if len(recent_wins) >= 5:
            # Active market — scan faster
            new_interval = max(5, config_interval // 2)
            if new_interval != self.scan_interval:
                logger.info(
                    f"[TUNER] ↑ speed: interval {self.scan_interval}s → {new_interval}s "
                    f"({len(recent_wins)} liquidations in 30min)"
                )
                self.scan_interval = new_interval
        elif len(recent_wins) == 0 and self._cycle_count > 200:
            # Quiet market — scan slower to conserve RPC calls
            new_interval = min(60, config_interval * 2)
            if new_interval != self.scan_interval:
                logger.info(
                    f"[TUNER] ↓ speed: interval {self.scan_interval}s → {new_interval}s "
                    f"(quiet market)"
                )
                self.scan_interval = new_interval

    def _report(self):
        """Log current tuned parameters."""
        skip_str = ", ".join(self.collateral_skip) or "none"
        logger.info(
            f"[TUNER] Parameters @ cycle #{self._cycle_count}: "
            f"min_profit=${self.min_profit_usd:.1f} | "
            f"gas_cap={self.gas_cap_gwei:.3f} gwei | "
            f"interval={self.scan_interval}s | "
            f"skip=[{skip_str}]"
        )

    def get_effective_params(self) -> dict:
        """Returns current tuned parameters for other modules to use."""
        return {
            "min_profit_usd":  self.min_profit_usd,
            "gas_cap_gwei":    self.gas_cap_gwei,
            "scan_interval":   self.scan_interval,
            "collateral_skip": list(self.collateral_skip),
        }


# Global tuner instance — shared across modules
_tuner: Optional[AutoTuner] = None

def get_tuner() -> AutoTuner:
    global _tuner
    if _tuner is None:
        _tuner = AutoTuner()
    return _tuner
