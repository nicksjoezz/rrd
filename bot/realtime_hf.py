"""
realtime_hf.py — Continuous HF tracking using real-time prices.

Iterates through all known at-risk positions and recalculates HF
using the streamer's price map. Triggers liquidation immediately
without waiting for the next scan cycle.
"""

import time
import logging
import threading
from typing import List, Dict, Any, Optional

from .utils import logger, cfg
from .persistence import critical_store, zombies_store, watching_store, save_categorized_position
from .ws_prices import get_price_streamer
from .liquidator import LiquidationExecutor
from .auto_tuner import get_tuner

class RealTimeHFTracker:
    def __init__(self, executor: LiquidationExecutor):
        self.executor = executor
        self.streamer = get_price_streamer()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def _recalculate_hf(self, pos: Dict[str, Any]) -> Optional[float]:
        """
        Recalculate HF using streamed prices.
        Requires the position dict to have enough metadata from previous scans.
        """
        # We need a fresh scan to have the base values.
        # If we just have the total USD values, we can scale them by price moves.
        # But for 'true' HF, we need the token balances.

        # ProtocolMonitor.check_position already calculates 'fresh_hf' if it can.
        # However, to be 'high speed', we want to avoid RPC in this loop.

        # If we have 'col_price' and 'debt_price' at the time of last scan:
        last_col_price = pos.get('col_price', 0)
        last_debt_price = pos.get('debt_price', 0)
        last_hf = pos.get('health_factor', 0)

        if last_col_price == 0 or last_debt_price == 0: return None

        # Get real-time prices
        now_col_price = self.streamer.get_price(pos.get('collateral_token', ''))
        now_debt_price = self.streamer.get_price(pos.get('debt_token', ''))

        if not now_col_price or not now_debt_price: return None

        # HF = (Total Collateral USD * Threshold) / Total Debt USD
        # If we have the used_threshold (which accounts for E-Mode), we can be more accurate.
        # New HF = (Last Col USD * (Now Price / Last Price) * Threshold) / (Last Debt USD * (Now Price / Last Price))

        col_move = now_col_price / last_col_price
        debt_move = now_debt_price / last_debt_price

        # We need the ratio of total values.
        # Since we only track the price of the 'best' tokens, this is an approximation.
        # But for liquidation triggers, the 'best' tokens usually represent the majority of the position.
        new_hf = last_hf * (col_move / debt_move)
        return new_hf

    def _process_loop(self):
        self._running = True
        logger.info("[RT-HF] Starting real-time HF tracking loop")

        while self._running:
            try:
                # Aggregate all positions being watched
                all_pos = (
                    critical_store.get_all_list() +
                    zombies_store.get_all_list() +
                    watching_store.get_all_list()
                )

                liquidatable = []

                for pos in all_pos:
                    new_hf = self._recalculate_hf(pos)
                    if new_hf is not None:
                        # Update the health factor in the object
                        old_hf = pos.get('health_factor', 0)
                        pos['health_factor'] = new_hf

                        # If crossing thresholds, update persistence
                        # This moves them between files automatically
                        if abs(new_hf - old_hf) > 0.001:
                            save_categorized_position(pos)

                        if new_hf <= 1.0:
                            liquidatable.append(pos)

                if liquidatable:
                    logger.info(f"[RT-HF] Detected {len(liquidatable)} liquidatable positions via price stream")
                    # Rank and execute
                    from .profitability import rank_positions
                    ranked = rank_positions(liquidatable)
                    if ranked:
                        # Ensure we use the current mode
                        from .utils import load_config
                        mode = load_config().get("mode", "simulate")
                        logger.info(f"[RT-HF] Firing {len(ranked)} positions in {mode.upper()} mode")
                        self.executor.execute_batch(ranked)

            except Exception as e:
                logger.error(f"[RT-HF] Loop error: {e}")

            time.sleep(1) # Frequency of real-time check

    def start(self):
        if self._thread and self._thread.is_alive(): return
        self._thread = threading.Thread(target=self._process_loop, daemon=True, name="rt-hf")
        self._thread.start()

    def stop(self):
        self._running = False
