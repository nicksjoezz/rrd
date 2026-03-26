"""
emode_detector.py — Aave V3 E-Mode position awareness.

E-Mode (Efficiency Mode) lets users borrow at up to 93% LTV for correlated
assets (e.g. ETH LSTs). These positions go underwater FASTER when prices
diverge (e.g. weETH depegging from ETH).

E-Mode categories on Arbitrum (Aave V3):
  0 = No E-Mode (standard)
  1 = ETH-correlated (WETH, wstETH, rETH, weETH, ezETH) — LTV 93%
  2 = Stablecoins (USDC, USDT, DAI) — LTV 97%
  3 = BTC-correlated (WBTC) — LTV 90%

EDGE: During LST depegging events E-Mode positions hit liquidation threshold
much faster than regular positions. Velocity + E-Mode = early detection.
"""

import logging
from typing import Optional
from web3 import Web3

from .utils import get_web3, checksum, cfg

logger = logging.getLogger("liquidation_bot.emode")

POOL_EMODE_ABI = [
    {
        "name": "getUserEMode",
        "type": "function",
        "stateMutability": "view",
        "inputs":  [{"name": "user", "type": "address"}],
        "outputs": [{"type": "uint256"}],
    },
    {
        "name": "getEModeCategoryData",
        "type": "function",
        "stateMutability": "view",
        "inputs":  [{"name": "id", "type": "uint8"}],
        "outputs": [
            {"name": "ltv",                  "type": "uint16"},
            {"name": "liquidationThreshold", "type": "uint16"},
            {"name": "liquidationBonus",     "type": "uint16"},
            {"name": "priceSource",          "type": "address"},
            {"name": "label",                "type": "string"},
        ],
    },
]

EMODE_CATEGORIES = {
    0: {"name": "Standard",       "ltv": None,  "liq_threshold": None,  "bonus": None},
    1: {"name": "ETH-correlated", "ltv": 0.93,  "liq_threshold": 0.95,  "bonus": 0.01},
    2: {"name": "Stablecoins",    "ltv": 0.97,  "liq_threshold": 0.975, "bonus": 0.01},
    3: {"name": "BTC-correlated", "ltv": 0.90,  "liq_threshold": 0.925, "bonus": 0.015},
}


class EModeDetector:
    """Detects E-Mode category for Aave V3 positions."""

    def __init__(self, pool_address: str):
        self._pool_addr = pool_address
        self._cache: dict = {}

    def _get_pool(self):
        w3 = get_web3()
        return w3.eth.contract(
            address=checksum(self._pool_addr),
            abi=POOL_EMODE_ABI
        )

    def get_user_emode(self, user: str) -> int:
        key = user.lower()
        if key in self._cache:
            return self._cache[key]
        try:
            pool   = self._get_pool()
            emode  = pool.functions.getUserEMode(checksum(user)).call()
            self._cache[key] = emode
            return emode
        except Exception as e:
            logger.debug(f"getUserEMode error for {user[:8]}: {e}")
            return 0

    def get_emode_info(self, emode_id: int) -> dict:
        if emode_id in EMODE_CATEGORIES:
            return EMODE_CATEGORIES[emode_id]
        try:
            pool = self._get_pool()
            data = pool.functions.getEModeCategoryData(emode_id).call()
            return {
                "name":           data[4],
                "ltv":            data[0] / 10000,
                "liq_threshold":  data[1] / 10000,
                "bonus":          (data[2] - 10000) / 10000,
            }
        except Exception:
            return EMODE_CATEGORIES[0]

    def enrich_position(self, position: dict) -> dict:
        """Add E-Mode metadata to a position dict."""
        user     = position.get("user", "")
        protocol = position.get("protocol", "")

        # Only Aave V3 supports E-Mode
        if "aave" not in protocol.lower():
            position["emode_id"]   = 0
            position["emode_name"] = "N/A"
            return position

        emode_id   = self.get_user_emode(user)
        emode_info = self.get_emode_info(emode_id)

        position["emode_id"]   = emode_id
        position["emode_name"] = emode_info["name"]
        # Store for risk_scorer
        position["emode_category"] = emode_id

        if emode_id > 0 and emode_info.get("bonus"):
            position["collateral_bonus"] = emode_info["bonus"]
            logger.debug(
                f"[EMODE] {user[:8]}... E-Mode {emode_id} "
                f"({emode_info['name']}) bonus={emode_info['bonus']*100:.1f}%"
            )

        return position

    def clear_cache(self):
        self._cache.clear()


def flag_emode_risk_positions(positions: list, pool_address: str = None) -> list:
    """
    Enrich positions with E-Mode data. Prioritises E-Mode 1 (ETH LSTs).
    pool_address defaults to Aave V3 on Arbitrum from config if not provided.
    """
    if pool_address is None:
        # Default to Aave V3 pool from config
        protocols  = cfg("protocols")
        pool_address = protocols.get("aave_v3", {}).get(
            "pool", "0x794a61358D6845594F94dc1DB02A252b5b4814aD"
        )

    detector = EModeDetector(pool_address)
    enriched = [detector.enrich_position(p) for p in positions]

    emode1 = [p for p in enriched if p.get("emode_id") == 1]
    others = [p for p in enriched if p.get("emode_id") != 1]

    if emode1:
        logger.info(f"[EMODE] {len(emode1)} E-Mode 1 (ETH-correlated) positions flagged")

    return emode1 + others
