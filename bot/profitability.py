"""
profitability.py — Accurate profit estimation before submitting any tx
Accounts for: flash loan fee, swap slippage, gas cost, liquidation bonus
"""

import logging
import requests
from typing import Optional
from web3 import Web3
from .utils import (
    get_web3, cfg, get_token_map, checksum,
    wei_to_usd_base, ERC20_ABI, CHAINLINK_FEED_ABI,
    AAVE_ORACLE_ABI, ADDRESSES_PROVIDER_ABI
)

logger = logging.getLogger("liquidation_bot.profit")

# Cache prices to avoid hammering RPC
_price_cache: dict = {}
_price_cache_block: int = 0
_aave_oracle_addr: Optional[str] = None

_cg_eth_cache: float = 0.0
_cg_eth_time: float = 0.0

def _get_aave_oracle() -> Optional[str]:
    global _aave_oracle_addr
    if _aave_oracle_addr: return _aave_oracle_addr
    try:
        w3 = get_web3()
        # Aave V3 Pool Addresses Provider on Arbitrum
        provider_addr = cfg("protocols", "aave_v3", "addresses_provider")
        provider = w3.eth.contract(address=checksum(provider_addr), abi=ADDRESSES_PROVIDER_ABI)
        _aave_oracle_addr = provider.functions.getPriceOracle().call()
        return _aave_oracle_addr
    except Exception: return None

def _get_coingecko_eth_price() -> float:
    """Final fallback for ETH price (with 60s cache)."""
    global _cg_eth_cache, _cg_eth_time
    now = time.time()
    if now - _cg_eth_time < 60 and _cg_eth_cache > 0:
        return _cg_eth_cache

    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd", timeout=5)
        price = float(r.json()["ethereum"]["usd"])
        if price > 0:
            _cg_eth_cache = price
            _cg_eth_time = now
        return price
    except Exception: return _cg_eth_cache

def get_token_price_usd(token_address: str, force_fresh: bool = False) -> float:
    """
    Get token price in USD using Chainlink feeds.
    Falls back to on-chain Aave oracle pricing via base unit conversion.
    Returns 0.0 if price unavailable.
    """
    w3   = get_web3()
    addr = token_address.lower()

    # Use cached prices if same block and not forced
    current_block = w3.eth.block_number
    global _price_cache, _price_cache_block
    if not force_fresh and current_block == _price_cache_block and addr in _price_cache:
        return _price_cache[addr]

    # Chainlink feed lookup
    chainlink_feeds = cfg("oracle", "chainlink_feeds")
    token_map       = get_token_map()
    token_info      = token_map.get(addr)

    if token_info:
        sym = token_info["symbol"]
        feed_key = f"{sym}_USD"
        if feed_key in chainlink_feeds:
            try:
                feed = w3.eth.contract(
                    address=checksum(chainlink_feeds[feed_key]),
                    abi=CHAINLINK_FEED_ABI
                )
                data  = feed.functions.latestRoundData().call()
                price = data[1] / 1e8  # Chainlink uses 8 decimals
                _price_cache[addr] = price
                _price_cache_block = current_block
                return price
            except Exception as e:
                logger.debug(f"Chainlink price fetch failed for {sym}: {e}")


    # Fallback 2: Aave Oracle
    oracle_addr = _get_aave_oracle()
    if oracle_addr:
        try:
            oracle = w3.eth.contract(address=checksum(oracle_addr), abi=AAVE_ORACLE_ABI)
            # Aave reports in 8 decimals for USD base
            price = oracle.functions.getAssetPrice(checksum(token_address)).call() / 1e8
            if price > 0:
                _price_cache[addr] = price
                _price_cache_block = current_block
                return price
        except Exception: pass

    # Fallback 3: CoinGecko (ETH only, lazy fetch)
    if addr == cfg("network", "weth").lower():
        price = _get_coingecko_eth_price()
        if price > 0:
            _price_cache[addr] = price
            _price_cache_block = current_block
            return price

    return 0.0  # Unknown — will be excluded from profitability check


def estimate_profit_usd(position: dict) -> dict:
    """
    Estimate net profit for a liquidation. Returns:
    {
        "profitable": bool,
        "estimated_profit_usd": float,
        "liquidation_bonus_usd": float,
        "estimated_gas_usd": float,
        "swap_slippage_usd": float,
        "debt_to_cover_usd": float
    }
    """
    w3 = get_web3()

    col_addr  = position["collateral_token"]
    debt_addr = position["debt_token"]
    token_map = get_token_map()

    col_info  = token_map.get(col_addr.lower())
    debt_info = token_map.get(debt_addr.lower())

    if not col_info or not debt_info:
        return {"profitable": False, "reason": "Unknown token pair"}

    # Raw debt amount (in token decimals)
    debt_amount_raw = position["debt_to_cover"]
    debt_decimals   = debt_info["decimals"]
    debt_amount     = debt_amount_raw / (10 ** debt_decimals)

    # USD values
    debt_price  = get_token_price_usd(debt_addr)
    col_price   = get_token_price_usd(col_addr)

    if debt_price <= 0 or col_price <= 0:
        return {"profitable": False, "reason": "Missing live price data"}

    position["debt_price"] = debt_price
    position["col_price"]  = col_price

    debt_usd          = debt_amount * debt_price
    bonus_rate        = col_info["liquidation_bonus"]
    liquidation_bonus = debt_usd * bonus_rate

    # Slippage cost (swap collateral → debt token)
    slippage_bps   = cfg("swap", "slippage_bps")
    slippage_cost  = debt_usd * (slippage_bps / 10000)

    # Flash loan cost (Balancer = 0%)
    flash_fee = cfg("flash_loan", "fee_bps") / 10000 * debt_usd

    # Gas estimate (Arbitrum L2 + approximate L1 calldata fee)
    gas_limit       = cfg("gas", "gas_limit")
    max_fee_gwei    = cfg("gas", "max_fee_per_gas_gwei")
    eth_price       = get_token_price_usd(cfg("network", "weth"))
    if eth_price <= 0:
        return {"profitable": False, "reason": "ETH price unavailable for gas estimation"}

    # L2 Execution cost
    l2_gas_cost_eth = (gas_limit * max_fee_gwei * 1e9) / 1e18
    # L1 Calldata overhead (approx ~3000-5000 gas per tx on L1 terms)
    # On Arbitrum, this manifests as a surcharge. We'll add a 25% safety margin
    # to the L2 cost to conservatively cover it until we fetch raw L1 gas prices.
    total_gas_cost_eth = l2_gas_cost_eth * 1.25
    gas_cost_usd = total_gas_cost_eth * eth_price

    # Net profit
    net_profit = liquidation_bonus - slippage_cost - flash_fee - gas_cost_usd

    min_profit = cfg("strategy", "min_profit_usd")
    profitable = net_profit >= min_profit

    return {
        "profitable": profitable,
        "estimated_profit_usd": round(net_profit, 4),
        "liquidation_bonus_usd": round(liquidation_bonus, 4),
        "estimated_gas_usd": round(gas_cost_usd, 4),
        "swap_slippage_usd": round(slippage_cost, 4),
        "flash_fee_usd": round(flash_fee, 4),
        "debt_to_cover_usd": round(debt_usd, 4),
        "bonus_rate": bonus_rate,
        "reason": "OK" if profitable else f"Profit ${net_profit:.2f} < min ${min_profit}"
    }


def rank_positions(positions: list) -> list:
    """Sort positions by estimated profit, highest first."""
    scored = []
    for pos in positions:
        profit_info = estimate_profit_usd(pos)
        if profit_info["profitable"]:
            # Find best swap route and attach to position
            from .swap_router import get_best_swap
            _, _, swap_params = get_best_swap(
                pos["collateral_token"],
                pos["debt_token"],
                pos["debt_to_cover"] # This is rough, but good for route selection
            )
            pos["swap_params"] = swap_params
            pos["profit_info"] = profit_info
            scored.append(pos)
        else:
            logger.debug(
                f"Skipping {pos['user'][:8]}... — {profit_info.get('reason','unprofitable')}"
            )

    scored.sort(key=lambda x: x["profit_info"]["estimated_profit_usd"], reverse=True)
    return scored
