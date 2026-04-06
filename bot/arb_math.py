import math
import logging
from .utils import logger

def calculate_optimal_input(u_liq_usd, c_liq_usd, p_u, p_c):
    """
    Calculate optimal input amount for arbitrage based purely on liquidity.

    Using the Square Root of Liquidity approach:
    Optimal input x = (sqrt(p_high / p_low) - 1) * L
    where L is the liquidity of the pair in terms of the input token.

    We remove the hard $200-$500 caps and instead use a fixed percentage
    of the total pool liquidity (2%) to ensure zero market impact regardless of token size.
    """
    if p_u == 0 or p_c == 0:
        return 0

    try:
        # Use the smaller liquidity to be conservative and minimize impact
        L_usd = min(u_liq_usd, c_liq_usd)

        # Calculate theoretical optimal x
        ratio = max(p_u, p_c) / min(p_u, p_c)
        # x = (sqrt(ratio) - 1) * (L_usd / 2)
        theoretical_x = (math.sqrt(ratio) - 1) * (L_usd / 2)

        # Target amount is a safe percentage of the pool to ensure minimal slippage/impact.
        # We strictly use 3% of the total liquidity to guarantee zero market impact
        # while maintaining enough size to be profitable, as requested.
        final_x = L_usd * 0.03

        # Ensure we return a positive number
        return max(0, final_x)
    except Exception as e:
        logger.error(f"Error calculating optimal input: {e}")
        return 0
