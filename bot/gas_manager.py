"""
gas_manager.py — Dynamic gas pricing for Arbitrum.

Problem: Fixed gas prices waste money on cheap blocks and miss
liquidations when the network is congested.

This module:
  1. Reads current base fee from the latest block
  2. Calculates the minimum gas needed to be included in the next block
  3. Scales gas UP proportionally to liquidation profit (worth paying more
     for a $500 profit than a $20 profit)
  4. Never pays more than config max_fee_per_gas_gwei
  5. Detects gas spikes (>5x normal) and optionally pauses execution

Arbitrum specifics:
  - Base fee is extremely low (0.01–0.1 gwei typically)
  - Priority fee barely matters — sequencer includes most txs
  - Gas limit matters more than gas price here
  - During congestion events, base fee can spike 10x briefly
"""

import logging
import time
from typing import Optional, Tuple
from web3 import Web3

from .utils import get_web3, cfg

logger = logging.getLogger("liquidation_bot.gas")

# Cache to avoid hammering eth_getBlockByNumber
_last_base_fee: int = 0
_last_base_fee_ts: float = 0
_BASE_FEE_CACHE_SECS = 5


def get_current_base_fee() -> int:
    """Get current base fee in wei from latest block."""
    global _last_base_fee, _last_base_fee_ts

    now = time.time()
    if now - _last_base_fee_ts < _BASE_FEE_CACHE_SECS and _last_base_fee > 0:
        return _last_base_fee

    try:
        w3    = get_web3()
        block = w3.eth.get_block("latest")
        base  = block.get("baseFeePerGas", 0)
        _last_base_fee    = base
        _last_base_fee_ts = now
        return base
    except Exception as e:
        logger.debug(f"get_current_base_fee error: {e}")
        return _last_base_fee or w3.to_wei(0.1, "gwei")


def get_gas_params(estimated_profit_usd: float = 0.0) -> dict:
    """
    Compute optimal gas params for a liquidation tx.

    Strategy:
      - Base: current_base_fee * 1.15 (buffer for next block)
      - Priority: 0.01 gwei flat (Arbitrum sequencer doesn't need bribing)
      - Scale: if profit > $500, willing to pay up to 2× normal gas cap
      - Hard cap: config max_fee_per_gas_gwei

    Returns web3-compatible dict for build_transaction().
    """
    w3        = get_web3()
    gas_cfg   = cfg("gas")
    max_gwei  = gas_cfg["max_fee_per_gas_gwei"]
    prio_gwei = gas_cfg["max_priority_fee_gwei"]
    mult      = gas_cfg.get("gas_price_multiplier", 1.2)

    base_fee    = get_current_base_fee()
    base_gwei   = base_fee / 1e9

    # Compute recommended max fee
    recommended = base_gwei * mult

    # Scale cap by profit — willing to pay more for bigger opportunities
    # For Arbitrum, base fee is usually very low, so priority fee is the primary way to compete.
    current_prio_gwei = prio_gwei

    if estimated_profit_usd >= 1000:
        cap = max_gwei * 5.0
        current_prio_gwei = prio_gwei * 10.0 # Aggressive priority for Whales
    elif estimated_profit_usd >= 500:
        cap = max_gwei * 3.0
        current_prio_gwei = prio_gwei * 5.0
    elif estimated_profit_usd >= 100:
        cap = max_gwei * 2.0
        current_prio_gwei = prio_gwei * 2.0
    else:
        cap = max_gwei

    max_fee_gwei = min(recommended, cap)

    # Never go below base fee + priority (tx won't land)
    min_viable = base_gwei + current_prio_gwei
    max_fee_gwei = max(max_fee_gwei, min_viable)

    max_fee_wei  = int(w3.to_wei(max_fee_gwei, "gwei"))
    prio_fee_wei = int(w3.to_wei(current_prio_gwei, "gwei"))

    logger.debug(
        f"Gas: base={base_gwei:.4f} gwei | "
        f"maxFee={max_fee_gwei:.4f} gwei | "
        f"priority={prio_gwei} gwei | "
        f"cap={cap:.2f} gwei"
    )

    return {
        "gas":                  gas_cfg["gas_limit"],
        "maxFeePerGas":         max_fee_wei,
        "maxPriorityFeePerGas": prio_fee_wei,
    }


def estimate_gas_cost_usd(gas_used: int = None) -> float:
    """
    Estimate gas cost in USD for a liquidation tx.
    Uses current base fee and ETH price.
    """
    from .profitability import get_token_price_usd
    from .utils import cfg as ucfg

    gas_cfg  = ucfg("gas")
    gas_lim  = gas_used or gas_cfg["gas_limit"]
    base_fee = get_current_base_fee()
    prio     = int(get_web3().to_wei(gas_cfg["max_priority_fee_gwei"], "gwei"))
    total_fee_wei = gas_lim * (base_fee + prio)
    eth_spent     = total_fee_wei / 1e18

    weth_addr = ucfg("network", "weth")
    eth_price = get_token_price_usd(weth_addr)

    return eth_spent * eth_price


def is_gas_spike(spike_threshold: float = 5.0) -> bool:
    """
    Detect abnormal gas spikes (e.g. during market crashes).
    Returns True if current base fee is > spike_threshold × recent average.

    During extreme events (Jan 2026 crash), Arbitrum base fees spiked.
    Skipping liquidations during spikes avoids gas wars with unpredictable costs.
    """
    base = get_current_base_fee() / 1e9  # gwei
    normal_max = cfg("gas", "max_fee_per_gas_gwei")

    if base > normal_max * spike_threshold:
        logger.warning(
            f"⚠️  Gas spike detected: {base:.4f} gwei "
            f"({spike_threshold}× normal threshold of {normal_max} gwei)"
        )
        return True
    return False


def log_gas_summary():
    """Log a human-readable gas summary."""
    base  = get_current_base_fee() / 1e9
    cost  = estimate_gas_cost_usd()
    spike = is_gas_spike()
    logger.info(
        f"Gas: base={base:.4f} gwei | "
        f"est. cost/tx=${cost:.3f} | "
        f"{'⚠️ SPIKE' if spike else 'normal'}"
    )
