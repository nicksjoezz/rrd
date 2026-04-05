import math
import logging
from .utils import logger

def calculate_optimal_input(liquidity_usd, p_u, p_c):
    """
    Calculate optimal input amount for arbitrage in USDC.

    Using the Square Root of Liquidity approach:
    If p_u is higher than p_c, buy on Camelot (p_c) and sell on UniV3 (p_u).
    Optimal input x = (sqrt(p_u / p_c) - 1) * L
    where L is the liquidity of the pair in terms of the input token.

    liquidity_usd: total USD value in the pool (approximate)
    L (input liquidity) = liquidity_usd / 2
    """
    if p_u == 0 or p_c == 0:
        return 0

    try:
        # L is roughly half the total pool value
        L = liquidity_usd / 2

        ratio = max(p_u, p_c) / min(p_u, p_c)
        optimal_x = (math.sqrt(ratio) - 1) * L

        # Constrain to realistic flash loan amounts for small caps ($200 - $500)
        # as per user requirement.
        return max(200, min(500, optimal_x))
    except Exception as e:
        logger.error(f"Error calculating optimal input: {e}")
        return 200 # Default to $200 USDC
