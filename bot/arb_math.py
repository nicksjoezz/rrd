import math
import logging
from .utils import logger

def calculate_optimal_input(liquidity, p_u, p_c):
    """
    Calculate optimal input amount for arbitrage.

    Using the Square Root of Liquidity approach:
    If p_u is higher than p_c, buy on Camelot (p_c) and sell on UniV3 (p_u).
    Optimal input x = (sqrt(p_u / p_c) - 1) * L
    where L is the liquidity of the pair.

    This is a simplification for V2-style (Camelot) vs V3-style (UniV3) pools.
    """
    if p_u == 0 or p_c == 0:
        return 0

    try:
        # Simplistic liquidity measure (e.g. res0 * res1 in V2)
        # For actual V3, liquidity is sqrtPriceX96 * amount0 + amount1 / sqrtPriceX96
        # Let's use a conservative estimate for a $200-$500 flash loan range
        # as suggested in the project requirements.

        target_gap = p_u / p_c
        if target_gap > 1:
            # Buy on Camelot, Sell on UniV3
            optimal_x = (math.sqrt(target_gap) - 1) * liquidity
        else:
            # Sell on Camelot, Buy on UniV3
            target_gap = p_c / p_u
            optimal_x = (math.sqrt(target_gap) - 1) * liquidity

        # Constrain to realistic flash loan amounts for small caps ($200 - $500)
        return max(200, min(500, optimal_x))
    except Exception as e:
        logger.error(f"Error calculating optimal input: {e}")
        return 200 # Default to $200 USDC
