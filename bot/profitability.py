"""
profitability.py — Accurate profit estimation before submitting any tx
Accounts for: flash loan fee, swap slippage, gas cost, liquidation bonus
"""

import logging
import time
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
_price_cache_time: dict = {}
_last_eth_block_fetch: float = 0
_cached_eth_block: int = 0
_aave_oracle_addr: Optional[str] = None

_cg_eth_cache: float = 0.0
_cg_eth_time: float = 0.0

def _get_aave_oracle() -> Optional[str]:
    global _aave_oracle_addr
    if _aave_oracle_addr: return _aave_oracle_addr
    try:
        w3 = get_web3()
        provider_addr = cfg("protocols", "aave_v3", "addresses_provider")
        provider = w3.eth.contract(address=checksum(provider_addr), abi=ADDRESSES_PROVIDER_ABI)
        _aave_oracle_addr = provider.functions.getPriceOracle().call()
        return _aave_oracle_addr
    except Exception: return None

def get_token_price_usd(token_address: str, force_fresh: bool = False) -> float:
    """
    Get token price in USD using 3 methods as requested:
    1. Chainlink (Primary)
    2. Aave Oracle (Secondary)
    3. CoinGecko (Fallback for ETH/WETH)
    """
    w3   = get_web3()
    addr = token_address.lower()
    now  = time.time()

    # Throttled price cache: 10s TTL for individual token prices
    global _price_cache, _price_cache_time
    if not force_fresh and addr in _price_cache and (now - _price_cache_time.get(addr, 0) < 10):
        return _price_cache[addr]

    token_map  = get_token_map()
    token_info = token_map.get(addr)
    sym = token_info["symbol"] if token_info else addr[:10]

    # Method 1: Chainlink
    chainlink_feeds = cfg("oracle", "chainlink_feeds")
    feed_key = f"{sym}_USD"
    if feed_key in chainlink_feeds:
        try:
            feed = w3.eth.contract(
                address=checksum(chainlink_feeds[feed_key]),
                abi=CHAINLINK_FEED_ABI
            )
            data  = feed.functions.latestRoundData().call()
            price = data[1] / 1e8
            logger.info(f"[PRICE] Chainlink used for {sym}: ${price:,.2f}")
            _price_cache[addr] = price
            _price_cache_time[addr] = now
            return price
        except Exception as e:
            logger.debug(f"Chainlink fetch failed for {sym}: {e}")

    # Method 2: Aave Oracle
    oracle_addr = _get_aave_oracle()
    if oracle_addr:
        try:
            oracle = w3.eth.contract(address=checksum(oracle_addr), abi=AAVE_ORACLE_ABI)
            price_raw = oracle.functions.getAssetPrice(checksum(token_address)).call()
            price = price_raw / 1e8 # Aave V3 uses 8 decimals for base currency (USD) on Arbitrum
            logger.info(f"[PRICE] Aave Oracle used for {sym}: ${price:,.2f}")
            _price_cache[addr] = price
            _price_cache_time[addr] = now
            return price
        except Exception as e:
            logger.debug(f"Aave Oracle fetch failed for {sym}: {e}")

    # Method 3: CoinGecko (Only for WETH/ETH)
    weth_addr = cfg("network", "weth").lower()
    if addr == weth_addr or sym == "WETH" or sym == "ETH":
        global _cg_eth_cache, _cg_eth_time
        if not force_fresh and now - _cg_eth_time < 60 and _cg_eth_cache > 0:
            logger.info(f"[PRICE] CoinGecko (cached) used for ETH: ${_cg_eth_cache:,.2f}")
            return _cg_eth_cache
        try:
            r = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd", timeout=5)
            price = float(r.json()["ethereum"]["usd"])
            if price > 0:
                _cg_eth_cache = price
                _cg_eth_time = now
                logger.info(f"[PRICE] CoinGecko used for ETH: ${price:,.2f}")
                _price_cache[addr] = price
                _price_cache_time[addr] = now
                return price
        except Exception as e:
            logger.debug(f"CoinGecko fetch failed: {e}")

    # Final Fallback from config (if any)
    fallback = cfg("oracle", "fallback_eth_price")
    if (addr == weth_addr or sym == "WETH") and fallback:
        logger.warning(f"[PRICE] Using hardcoded fallback for ETH: ${fallback}")
        return fallback

    logger.error(f"CRITICAL: All price sources failed for {sym} ({addr})")
    raise ValueError(f"Price unavailable for {sym}")


def estimate_profit_usd(position: dict, force_fresh: bool = False) -> dict:
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

    # Raw debt amount (in token decimals)
    debt_amount_raw = position["debt_to_cover"]

    # Use protocol metadata if not in config
    if debt_info:
        debt_decimals = debt_info["decimals"]
    else:
        # We need decimals to calculate amount. We can fetch them from the contract.
        from .utils import ERC20_ABI
        try:
            tok = w3.eth.contract(address=checksum(debt_addr), abi=ERC20_ABI)
            debt_decimals = tok.functions.decimals().call()
        except Exception:
            debt_decimals = 18 # Final fallback

    debt_amount = debt_amount_raw / (10 ** debt_decimals)

    # USD values
    debt_price  = get_token_price_usd(debt_addr, force_fresh=force_fresh)
    col_price   = get_token_price_usd(col_addr, force_fresh=force_fresh)

    if debt_price <= 0 or col_price <= 0:
        return {"profitable": False, "reason": "Missing live price data"}

    position["debt_price"] = debt_price
    position["col_price"]  = col_price

    debt_usd          = debt_amount * debt_price

    if col_info:
        bonus_rate = col_info["liquidation_bonus"]
    else:
        # Fetch bonus from position (which was populated from protocol metadata)
        bonus_rate = position.get("collateral_bonus", 0.05)

    liquidation_bonus = debt_usd * bonus_rate

    # Slippage cost (swap collateral → debt token)
    slippage_bps   = cfg("swap", "slippage_bps")
    slippage_cost  = debt_usd * (slippage_bps / 10000)

    # Flash loan cost (Balancer = 0%)
    flash_fee = cfg("flash_loan", "fee_bps") / 10000 * debt_usd

    # Gas estimate (Arbitrum L2 + approximate L1 calldata fee)
    gas_limit       = cfg("gas", "gas_limit")
    max_fee_gwei    = cfg("gas", "max_fee_per_gas_gwei")
    eth_price       = get_token_price_usd(cfg("network", "weth"), force_fresh=force_fresh)
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
