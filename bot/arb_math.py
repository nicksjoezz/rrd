import math
import logging
from .utils import logger

def calculate_optimal_input(u_liq_usd, c_liq_usd, p_u, p_c):
    """
    Calculate optimal input amount for arbitrage based on liquidity and price gap.

    Formula for optimal amount x in a constant product pool:
    x = (sqrt(p_high / p_low) - 1) * L
    where L is the liquidity (reserve) of the input token.

    However, for small-cap tokens with liquidity between $15k-$100k,
    we must strictly avoid market impact.

    We use the Square Root of Liquidity to find the theoretical maximum,
    but cap it at 3% of the smaller pool's liquidity to ensure zero impact.
    """
    if p_u == 0 or p_c == 0 or u_liq_usd == 0 or c_liq_usd == 0:
        return 0

    try:
        # 1. Identify high/low price sources
        p_high = max(p_u, p_c)
        p_low = min(p_u, p_c)
        price_ratio = p_high / p_low

        # 2. Use the smaller pool's USD liquidity to be conservative.
        # Total USD liquidity = 2 * Input Token Reserve (in USD)
        # So L_input_usd = Total_USD_Liq / 2
        L_input_usd = min(u_liq_usd, c_liq_usd) / 2

        # 3. Theoretical optimal input (Square Root of Liquidity logic)
        # x = (sqrt(ratio) - 1) * L
        theoretical_x = (math.sqrt(price_ratio) - 1) * L_input_usd

        # 4. Strict Market Impact Cap (3% of pool reserve)
        # For a $15k liquidity pool, this would be $15k * 0.03 = $450.
        # This matches the "Goldilocks" amount requested ($200-$500).
        impact_cap = min(u_liq_usd, c_liq_usd) * 0.03

        final_x = min(theoretical_x, impact_cap)

        # Ensure we don't trade dust
        if final_x < 10: return 0

        return final_x
    except Exception as e:
        logger.error(f"Math Error: {e}")
        return 0
