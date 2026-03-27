"""
utils.py — Config loading, Web3 setup, ABIs, shared helpers
"""

import json
import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Optional
from web3 import Web3
from eth_account import Account

# ── Paths ────────────────────────────────────────────────────────────────────
ROOT_DIR   = Path(__file__).parent.parent
CONFIG_PATH = ROOT_DIR / "config.json"

# ── Config ───────────────────────────────────────────────────────────────────
_config: Optional[dict] = None

def load_config() -> dict:
    global _config
    if _config is None:
        with open(CONFIG_PATH) as f:
            _config = json.load(f)
    return _config

def cfg(*keys):
    """Deep-get config values: cfg('strategy','min_profit_usd')"""
    try:
        d = load_config()
        for k in keys:
            d = d[k]
        return d
    except (KeyError, TypeError):
        return None

# ── Logging ───────────────────────────────────────────────────────────────────
def setup_logging() -> logging.Logger:
    config = load_config()
    log_cfg = config["logging"]
    log_dir = ROOT_DIR / "logs"
    log_dir.mkdir(exist_ok=True)

    level = getattr(logging, log_cfg["level"].upper(), logging.INFO)
    logger = logging.getLogger("liquidation_bot")
    logger.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    fh = logging.handlers.RotatingFileHandler(
        ROOT_DIR / log_cfg["file"],
        maxBytes=log_cfg["max_bytes"],
        backupCount=log_cfg["backup_count"]
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger

logger = setup_logging()

# ── Web3 ──────────────────────────────────────────────────────────────────────
import threading

_public_w3: Optional[Web3] = None
_alchemy_w3: Optional[Web3] = None
_rpc_lock = threading.RLock()
_key_index = 0

def get_public_web3() -> Web3:
    global _public_w3
    with _rpc_lock:
        if _public_w3 is None or not _public_w3.is_connected():
            rpc = cfg("network", "rpc_http") or "https://arb1.arbitrum.io/rpc"
            _public_w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 60}))
    return _public_w3

def get_alchemy_web3() -> Web3:
    global _alchemy_w3, _key_index
    with _rpc_lock:
        if _alchemy_w3 is None or not _alchemy_w3.is_connected():
            keys = cfg("network", "alchemy_keys")
            if not keys:
                key = cfg("network", "alchemy_key")
                keys = [key] if key else []

            if not keys or keys[0] == "YOUR_ALCHEMY_KEY_HERE":
                return get_public_web3()

            # Try keys until one works
            for _ in range(len(keys)):
                key = keys[_key_index % len(keys)]
                rpc = key if key.startswith("http") else f"https://arb-mainnet.g.alchemy.com/v2/{key}"
                try:
                    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 20}))
                    if w3.is_connected():
                        _alchemy_w3 = w3
                        return _alchemy_w3
                except: pass
                _key_index += 1

            return get_public_web3()
    return _alchemy_w3

def get_web3() -> Web3: return get_alchemy_web3()

def get_account():
    try:
        pk = cfg("wallet", "private_key")
        if not pk or pk == "YOUR_PRIVATE_KEY_HERE": return None
        return Account.from_key(pk)
    except: return None

# ── ABIs ──────────────────────────────────────────────────────────────────────
AAVE_POOL_ABI = json.loads('''[
  {"name":"liquidationCall","type":"function","stateMutability":"nonpayable",
   "inputs":[
     {"name":"collateralAsset","type":"address"},
     {"name":"debtAsset","type":"address"},
     {"name":"user","type":"address"},
     {"name":"debtToCover","type":"uint256"},
     {"name":"receiveAToken","type":"bool"}
   ],"outputs":[]},
  {"name":"getUserAccountData","type":"function","stateMutability":"view",
   "inputs":[{"name":"user","type":"address"}],
   "outputs":[
     {"name":"totalCollateralBase","type":"uint256"},
     {"name":"totalDebtBase","type":"uint256"},
     {"name":"availableBorrowsBase","type":"uint256"},
     {"name":"currentLiquidationThreshold","type":"uint256"},
     {"name":"ltv","type":"uint256"},
     {"name":"healthFactor","type":"uint256"}
   ]}
]''')

DATA_PROVIDER_ABI = json.loads('''[
  {"name":"getUserReserveData","type":"function","stateMutability":"view",
   "inputs":[
     {"name":"asset","type":"address"},
     {"name":"user","type":"address"}
   ],
   "outputs":[
     {"name":"currentATokenBalance","type":"uint256"},
     {"name":"currentStableDebt","type":"uint256"},
     {"name":"currentVariableDebt","type":"uint256"},
     {"name":"principalStableDebt","type":"uint256"},
     {"name":"scaledVariableDebt","type":"uint256"},
     {"name":"stableBorrowRate","type":"uint256"},
     {"name":"liquidityRate","type":"uint256"},
     {"name":"stableRateLastUpdated","type":"uint40"},
     {"name":"usageAsCollateralEnabled","type":"bool"}
   ]}
]''')

ERC20_ABI = json.loads('''[
  {"name":"approve","type":"function","stateMutability":"nonpayable",
   "inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],
   "outputs":[{"type":"bool"}]},
  {"name":"balanceOf","type":"function","stateMutability":"view",
   "inputs":[{"name":"account","type":"address"}],
   "outputs":[{"type":"uint256"}]},
  {"name":"decimals","type":"function","stateMutability":"view",
   "inputs":[],"outputs":[{"type":"uint8"}]},
  {"name":"symbol","type":"function","stateMutability":"view",
   "inputs":[],"outputs":[{"type":"string"}]}
]''')

CHAINLINK_FEED_ABI = json.loads('''[
  {"name":"latestRoundData","type":"function","stateMutability":"view",
   "inputs":[],"outputs":[
     {"name":"roundId","type":"uint80"},
     {"name":"answer","type":"int256"},
     {"name":"startedAt","type":"uint256"},
     {"name":"updatedAt","type":"uint256"},
     {"name":"answeredInRound","type":"uint80"}
   ]}
]''')

LIQUIDATOR_CONTRACT_ABI = json.loads('''[
  {"name":"executeLiquidation","type":"function","stateMutability":"nonpayable",
   "inputs":[
     {"name":"debtToken","type":"address"},
     {"name":"collateralToken","type":"address"},
     {"name":"borrower","type":"address"},
     {"name":"debtAmount","type":"uint256"},
     {"name":"lendingPool","type":"address"},
     {"name":"swapFee","type":"uint24"},
     {"name":"minProfit","type":"uint256"},
     {"name":"protocol","type":"uint8"}
   ],"outputs":[]},
  {"name":"executeLiquidationMultiHop","type":"function","stateMutability":"nonpayable",
   "inputs":[
     {"name":"debtToken","type":"address"},
     {"name":"collateralToken","type":"address"},
     {"name":"borrower","type":"address"},
     {"name":"debtAmount","type":"uint256"},
     {"name":"lendingPool","type":"address"},
     {"name":"swapPath","type":"bytes"},
     {"name":"minProfit","type":"uint256"},
     {"name":"protocol","type":"uint8"}
   ],"outputs":[]},
  {"name":"withdraw","type":"function","stateMutability":"nonpayable",
   "inputs":[{"name":"token","type":"address"}],"outputs":[]},
  {"name":"owner","type":"function","stateMutability":"view",
   "inputs":[],"outputs":[{"type":"address"}]}
]''')

COMET_ABI = json.loads('''[
  {"name":"absorb","type":"function","stateMutability":"nonpayable",
   "inputs":[{"name":"absorber","type":"address"},{"name":"accounts","type":"address[]"}],
   "outputs":[]},
  {"name":"baseToken","type":"function","stateMutability":"view",
   "inputs":[],"outputs":[{"type":"address"}]},
  {"name":"isLiquidatable","type":"function","stateMutability":"view",
   "inputs":[{"name":"account","type":"address"}],
   "outputs":[{"type":"bool"}]},
  {"name":"userCollateral","type":"function","stateMutability":"view",
   "inputs":[{"name":"account","type":"address"},{"name":"asset","type":"address"}],
   "outputs":[{"name":"balance","type":"uint128"},{"name":"_reserved","type":"uint128"}]},
  {"name":"userBasic","type":"function","stateMutability":"view",
   "inputs":[{"name":"account","type":"address"}],
   "outputs":[{"name":"principal","type":"int104"},{"name":"_reserved","type":"uint152"}]},
  {"name":"numAssets","type":"function","stateMutability":"view",
   "inputs":[],"outputs":[{"type":"uint8"}]},
  {"name":"getAssetInfo","type":"function","stateMutability":"view",
   "inputs":[{"name":"i","type":"uint8"}],
   "outputs":[{"components":[{"internalType":"uint8","name":"offset","type":"uint8"},{"internalType":"address","name":"asset","type":"address"},{"internalType":"address","name":"priceFeed","type":"address"},{"internalType":"uint64","name":"scale","type":"uint64"},{"internalType":"uint64","name":"borrowCollateralFactor","type":"uint64"},{"internalType":"uint64","name":"liquidateCollateralFactor","type":"uint64"},{"internalType":"uint64","name":"liquidationFactor","type":"uint64"},{"internalType":"uint128","name":"supplyCap","type":"uint128"}],"internalType":"struct CometStructs.AssetInfo","name":"","type":"tuple"}]},
  {"name":"getPrice","type":"function","stateMutability":"view",
   "inputs":[{"name":"priceFeed","type":"address"}],
   "outputs":[{"type":"uint256"}]}
]''')

# ── Multicall3 (Arbitrum) ───────────────────────────────────────────────────
MULTICALL3_ADDR = "0xcA11bde05977b3631167028862bE2a173976CA11"
MULTICALL3_ABI  = json.loads('[{"inputs":[{"components":[{"internalType":"address","name":"target","type":"address"},{"internalType":"bytes","name":"callData","type":"bytes"}],"internalType":"struct Multicall3.Call[]","name":"calls","type":"tuple[]"}],"name":"aggregate","outputs":[{"internalType":"uint256","name":"blockNumber","type":"uint256"},{"internalType":"bytes[]","name":"returnData","type":"bytes[]"}],"stateMutability":"payable","type":"function"}]')

# ── Token helpers ─────────────────────────────────────────────────────────────
def get_token_map() -> dict:
    tokens = cfg("tokens") or {}
    return {info["address"].lower(): {"symbol": sym, "address": info["address"], "decimals": info["decimals"], "liquidation_bonus": info["liquidation_bonus"]} for sym, info in tokens.items()}

def get_address_to_symbol() -> dict:
    tokens = cfg("tokens") or {}
    return {info["address"].lower(): sym for sym, info in tokens.items()}

def get_swap_fee(collateral_addr: str, debt_addr: str) -> int:
    a2s = get_address_to_symbol(); col = a2s.get(collateral_addr.lower(), ""); debt = a2s.get(debt_addr.lower(), "")
    fee_tiers = cfg("swap", "fee_tiers") or {}
    return fee_tiers.get(f"{col}-{debt}") or fee_tiers.get(f"{debt}-{col}") or cfg("swap", "default_fee_tier") or 3000

def checksum(addr: str) -> str: return Web3.to_checksum_address(addr)
def wei_to_usd_base(value: int) -> float: return value / 1e8
def health_factor_float(hf_raw: int) -> float: return hf_raw / 1e18 if hf_raw < 1e50 else float("inf")

def notify(message: str):
    try:
        notif = cfg("notifications")
        if not notif or not notif["enabled"]: return
        import requests
        requests.post(f"https://api.telegram.org/bot{notif['telegram_bot_token']}/sendMessage", json={"chat_id": notif["telegram_chat_id"], "text": message}, timeout=5)
    except: pass
