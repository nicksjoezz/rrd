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
    d = load_config()
    for k in keys:
        d = d[k]
    return d

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

    # File handler with rotation
    fh = logging.handlers.RotatingFileHandler(
        ROOT_DIR / log_cfg["file"],
        maxBytes=log_cfg["max_bytes"],
        backupCount=log_cfg["backup_count"]
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger

logger = setup_logging()

# ── Web3 ──────────────────────────────────────────────────────────────────────
_public_w3: Optional[Web3] = None
_alchemy_w3: Optional[Web3] = None

def get_public_web3() -> Web3:
    global _public_w3
    if _public_w3 is None or not _public_w3.is_connected():
        rpc = cfg("network", "rpc_http")
        _public_w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 60}))
        if not _public_w3.is_connected():
            logger.warning(f"Public RPC connection failed: {rpc}")
    return _public_w3

def get_alchemy_web3() -> Web3:
    global _alchemy_w3
    if _alchemy_w3 is None or not _alchemy_w3.is_connected():
        key = load_config()["network"].get("alchemy_key", "")
        if not key or key == "YOUR_ALCHEMY_KEY_HERE":
            return get_public_web3() # Fallback
        
        if key.startswith("http"):
            rpc = key
        else:
            rpc = f"https://arb-mainnet.g.alchemy.com/v2/{key}"
            
        try:
            _alchemy_w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 30}))
            if not _alchemy_w3.is_connected():
                logger.warning(f"Alchemy RPC failed (check key: {rpc[:25]}...) -- using Public fallback")
                return get_public_web3()
        except Exception as e:
            logger.error(f"Alchemy connection error: {e} (URL: {rpc[:25]}...)")
            return get_public_web3()
    return _alchemy_w3

def get_web3() -> Web3:
    """General connection — prefers Alchemy but stable."""
    return get_alchemy_web3()

def get_account():
    try:
        pk = cfg("wallet", "private_key")
        if not pk or pk == "YOUR_PRIVATE_KEY_HERE":
            return None
        return Account.from_key(pk)
    except Exception:
        return None

# ── ABIs ──────────────────────────────────────────────────────────────────────
BALANCER_VAULT_ABI = json.loads('''[
  {"name":"flashLoan","type":"function","stateMutability":"nonpayable",
   "inputs":[
     {"name":"recipient","type":"address"},
     {"name":"tokens","type":"address[]"},
     {"name":"amounts","type":"uint256[]"},
     {"name":"userData","type":"bytes"}
   ],"outputs":[]}
]''')

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
   ]},
  {"name":"getReservesList","type":"function","stateMutability":"view",
   "inputs":[],"outputs":[{"type":"address[]"}]},
  {"name":"Borrow","type":"event","inputs":[
     {"name":"reserve","type":"address","indexed":true},
     {"name":"user","type":"address","indexed":false},
     {"name":"onBehalfOf","type":"address","indexed":true},
     {"name":"amount","type":"uint256","indexed":false},
     {"name":"interestRateMode","type":"uint8","indexed":false},
     {"name":"borrowRate","type":"uint256","indexed":false},
     {"name":"referralCode","type":"uint16","indexed":true}
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
   ]},
  {"name":"getReserveConfigurationData","type":"function","stateMutability":"view",
   "inputs":[{"name":"asset","type":"address"}],
   "outputs":[
     {"name":"decimals","type":"uint256"},
     {"name":"ltv","type":"uint256"},
     {"name":"liquidationThreshold","type":"uint256"},
     {"name":"liquidationBonus","type":"uint256"},
     {"name":"reserveFactor","type":"uint256"},
     {"name":"usageAsCollateralEnabled","type":"bool"},
     {"name":"borrowingEnabled","type":"bool"},
     {"name":"stableBorrowRateEnabled","type":"bool"},
     {"name":"isActive","type":"bool"},
     {"name":"isFrozen","type":"bool"}
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
     {"name":"minProfit","type":"uint256"}
   ],"outputs":[]},
  {"name":"executeLiquidationMultiHop","type":"function","stateMutability":"nonpayable",
   "inputs":[
     {"name":"debtToken","type":"address"},
     {"name":"collateralToken","type":"address"},
     {"name":"borrower","type":"address"},
     {"name":"debtAmount","type":"uint256"},
     {"name":"lendingPool","type":"address"},
     {"name":"swapPath","type":"bytes"},
     {"name":"minProfit","type":"uint256"}
   ],"outputs":[]},
  {"name":"withdraw","type":"function","stateMutability":"nonpayable",
   "inputs":[{"name":"token","type":"address"}],"outputs":[]},
  {"name":"owner","type":"function","stateMutability":"view",
   "inputs":[],"outputs":[{"type":"address"}]}
]''')

AAVE_ORACLE_ABI = json.loads('''[
  {"name":"getAssetPrice","type":"function","stateMutability":"view",
   "inputs":[{"name":"asset","type":"address"}],"outputs":[{"type":"uint256"}]}
]''')

ADDRESSES_PROVIDER_ABI = json.loads('''[
  {"name":"getPriceOracle","type":"function","stateMutability":"view",
   "inputs":[],"outputs":[{"type":"address"}]}
]''')

# ── Multicall3 (Arbitrum) ───────────────────────────────────────────────────
def get_multicall3_addr() -> str:
    return cfg("network", "multicall3")

MULTICALL3_ADDR = get_multicall3_addr()
MULTICALL3_ABI  = json.loads('[{"inputs":[{"components":[{"internalType":"address","name":"target","type":"address"},{"internalType":"bytes","name":"callData","type":"bytes"}],"internalType":"struct Multicall3.Call[]","name":"calls","type":"tuple[]"}],"name":"aggregate","outputs":[{"internalType":"uint256","name":"blockNumber","type":"uint256"},{"internalType":"bytes[]","name":"returnData","type":"bytes[]"}],"stateMutability":"payable","type":"function"}]')

# ── Token helpers ─────────────────────────────────────────────────────────────
def get_token_map() -> dict:
    """Returns {address_lower: {symbol, decimals, liquidation_bonus}}"""
    tokens = cfg("tokens")
    result = {}
    for sym, info in tokens.items():
        result[info["address"].lower()] = {
            "symbol": sym,
            "address": info["address"],
            "decimals": info["decimals"],
            "liquidation_bonus": info["liquidation_bonus"]
        }
    return result

def get_address_to_symbol() -> dict:
    tokens = cfg("tokens")
    return {info["address"].lower(): sym for sym, info in tokens.items()}

def get_swap_fee(collateral_addr: str, debt_addr: str) -> int:
    """Return best Uniswap V3 fee tier for a token pair."""
    a2s = get_address_to_symbol()
    col  = a2s.get(collateral_addr.lower(), "")
    debt = a2s.get(debt_addr.lower(), "")
    fee_tiers = cfg("swap", "fee_tiers")
    key1 = f"{col}-{debt}"
    key2 = f"{debt}-{col}"
    return fee_tiers.get(key1) or fee_tiers.get(key2) or cfg("swap", "default_fee_tier")

def checksum(addr: str) -> str:
    return Web3.to_checksum_address(addr)

def wei_to_usd_base(value: int) -> float:
    """Convert Aave base units (8 decimals) to USD float."""
    return value / 1e8

def health_factor_float(hf_raw: int) -> float:
    """Convert raw health factor (18 decimals) to float."""
    if hf_raw >= (2**256 - 1) // 10:
        return float("inf")
    return hf_raw / 1e18

# ── Telegram notifications (optional) ────────────────────────────────────────
def notify(message: str):
    try:
        notif_cfg = cfg("notifications")
        if not notif_cfg["enabled"]:
            return
        import requests
        token   = notif_cfg["telegram_bot_token"]
        chat_id = notif_cfg["telegram_chat_id"]
        if not token or not chat_id:
            return
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        requests.post(url, json={"chat_id": chat_id, "text": message}, timeout=5)
    except Exception:
        pass
