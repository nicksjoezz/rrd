"""
swap_router.py — Smart swap path finder for Uniswap V3.

Problem with single-hop swaps:
  - Some collateral tokens (GMX, ARB) have poor direct liquidity vs USDC/USDT
  - Single-pool swaps cause high slippage → eat into profit
  - Example: GMX → USDC might be 5% slippage direct, but GMX → WETH → USDC is 0.3%

This module:
  1. Tries direct swap first (cheapest gas)
  2. Falls back to 2-hop route through WETH if direct liquidity is thin
  3. Quotes both paths and picks the better one
  4. Builds the correct calldata for the Uniswap V3 router

Edge: Better swap execution = more profit captured per liquidation.
"""

import logging
from web3 import Web3
from typing import Optional, Tuple
from .utils import get_web3, cfg, checksum, get_swap_fee

logger = logging.getLogger("liquidation_bot.swap")

# Uniswap V3 Quoter on Arbitrum (read-only price quotes)
QUOTER_ADDRESS = "0xb27308f9F90D607463bb33eA1BeBb41C27CE5AB6"

QUOTER_ABI = [
    {
        "name": "quoteExactInputSingle",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "tokenIn",           "type": "address"},
            {"name": "tokenOut",          "type": "address"},
            {"name": "fee",               "type": "uint24"},
            {"name": "amountIn",          "type": "uint256"},
            {"name": "sqrtPriceLimitX96", "type": "uint160"}
        ],
        "outputs": [{"name": "amountOut", "type": "uint256"}]
    },
    {
        "name": "quoteExactInput",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "path",     "type": "bytes"},
            {"name": "amountIn", "type": "uint256"}
        ],
        "outputs": [{"name": "amountOut", "type": "uint256"}]
    }
]

WETH_ADDRESS = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"


def encode_path(token_in: str, fee1: int, token_mid: str, fee2: int, token_out: str) -> bytes:
    """Encode a 2-hop Uniswap V3 swap path."""
    def addr_bytes(addr): return bytes.fromhex(checksum(addr)[2:])
    def fee_bytes(fee):   return fee.to_bytes(3, "big")
    return (addr_bytes(token_in) + fee_bytes(fee1) +
            addr_bytes(token_mid) + fee_bytes(fee2) +
            addr_bytes(token_out))


def get_best_swap(
    token_in: str,
    token_out: str,
    amount_in: int
) -> Tuple[int, str, dict]:
    """
    Find the best swap route and expected output.

    Returns:
        (expected_out, route_type, tx_params)
        route_type: "single" or "multi"
        tx_params: dict to pass to the contract
    """
    w3 = get_web3()

    # Skip quoting for same-token (stablecoin-stablecoin edge cases)
    if token_in.lower() == token_out.lower():
        return amount_in, "same", {}

    try:
        quoter = w3.eth.contract(address=checksum(QUOTER_ADDRESS), abi=QUOTER_ABI)

        fee_direct = get_swap_fee(token_in, token_out)
        best_out   = 0
        best_route = "single"
        best_params = {
            "route":    "single",
            "fee":      fee_direct,
            "token_in": token_in,
            "token_out": token_out,
        }

        # ── Quote direct single-hop ───────────────────────────────────────────
        try:
            direct_out = quoter.functions.quoteExactInputSingle(
                checksum(token_in),
                checksum(token_out),
                fee_direct,
                amount_in,
                0
            ).call()
            if direct_out > best_out:
                best_out    = direct_out
                best_route  = "single"
                best_params = {
                    "route":     "single",
                    "fee":       fee_direct,
                    "token_in":  token_in,
                    "token_out": token_out,
                    "amount_in": amount_in,
                }
        except Exception as e:
            logger.debug(f"Direct quote failed {token_in[:8]}->{token_out[:8]}: {e}")

        # ── Quote 2-hop via WETH ──────────────────────────────────────────────
        if token_in.lower() != WETH_ADDRESS.lower() and token_out.lower() != WETH_ADDRESS.lower():
            fee_in  = get_swap_fee(token_in, WETH_ADDRESS)
            fee_out = get_swap_fee(WETH_ADDRESS, token_out)
            path    = encode_path(token_in, fee_in, WETH_ADDRESS, fee_out, token_out)
            try:
                multi_out = quoter.functions.quoteExactInput(path, amount_in).call()
                if multi_out > best_out * 1.001:  # only switch if 0.1%+ better
                    best_out    = multi_out
                    best_route  = "multi"
                    best_params = {
                        "route":     "multi",
                        "path":      path,
                        "token_in":  token_in,
                        "token_out": token_out,
                        "amount_in": amount_in,
                        "fee_in":    fee_in,
                        "fee_out":   fee_out,
                    }
            except Exception as e:
                logger.debug(f"Multi-hop quote failed: {e}")

        if best_out == 0:
            logger.warning(f"No swap quote found for {token_in[:8]}->{token_out[:8]}")

        return best_out, best_route, best_params

    except Exception as e:
        logger.error(f"Swap routing error: {e}")
        fee = get_swap_fee(token_in, token_out)
        return 0, "single", {
            "route": "single", "fee": fee,
            "token_in": token_in, "token_out": token_out
        }


def log_swap_decision(token_in: str, token_out: str, amount_in: int,
                      expected_out: int, route: str):
    if expected_out == 0:
        return
    slippage_pct = max(0, (amount_in - expected_out) / amount_in * 100)
    logger.debug(
        f"Swap {token_in[:8]}→{token_out[:8]} | "
        f"in={amount_in} out={expected_out} | "
        f"route={route} | implied slippage≈{slippage_pct:.2f}%"
    )
