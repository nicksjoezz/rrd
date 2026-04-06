import math
import logging
from .utils import logger

def calculate_optimal_input(u_liq_usd, c_liq_usd, p_u, p_c):
    """
    Calculate optimal input amount for arbitrage in USDC.

    Using the Square Root of Liquidity approach:
    Optimal input x = (sqrt(p_high / p_low) - 1) * L
    where L is the liquidity of the pair (the smaller of the two) in terms of the input token.

    u_liq_usd: UniV3 pool liquidity in USD
    c_liq_usd: Camelot pool liquidity in USD
    """
    if p_u == 0 or p_c == 0:
        return 0

    try:
        # Use the smaller liquidity to be conservative and minimize impact
        L_usd = min(u_liq_usd, c_liq_usd)
        L = L_usd / 2 # Approx liquidity in USDC (half the pool)

        ratio = max(p_u, p_c) / min(p_u, p_c)
        optimal_x = (math.sqrt(ratio) - 1) * L

        # User requested $200-$500 flash loans for small caps.
        # We cap it at $500 but also ensure we don't use more than 5% of L_usd to keep impact low.
        safe_x = min(optimal_x, L_usd * 0.05)

        return max(200, min(500, safe_x))
    except Exception as e:
        logger.error(f"Error calculating optimal input: {e}")
        return 200 # Default to $200 USDC
