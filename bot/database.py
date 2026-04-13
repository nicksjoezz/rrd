"""
database.py — Persistence proxy layer for LiqBot.

Interfaces with JSON-based categorized stores in persistence.py.
"""

import logging
import time
from typing import Optional, List, Dict, Any

from . import persistence as p

logger = logging.getLogger("liquidation_bot.db")

def init_db():
    """Migrated to persistence.py (JSON)."""
    pass

def get_conn():
    """Deprecated: SQLite connection no longer used."""
    return None

# ── Borrowers ─────────────────────────────────────────────────────────────────
def upsert_borrowers(addresses: List[str], protocol: str):
    p.borrowers.add_borrowers(protocol, addresses)

def get_borrowers(protocol: str) -> List[str]:
    return p.borrowers.get_borrowers(protocol)

def remove_borrower(protocol: str, address: str):
    p.borrowers.remove_borrower(protocol, address)

def get_all_borrower_count() -> int:
    return p.borrowers.get_total_count()

# ── Positions (Categorized) ───────────────────────────────────────────────────
def upsert_position(pos: dict):
    """Saves a position to the appropriate categorized JSON file."""
    p.save_categorized_position(pos)

def get_approaching_positions(max_hf: float = 1.15) -> List[dict]:
    """Retrieves positions from the categorized stores."""
    all_pos = []
    if max_hf >= 1.0:
        all_pos.extend(p.critical_store.get_all_list())
    if max_hf >= 1.05:
        all_pos.extend(p.zombies_store.get_all_list())
    if max_hf >= 1.15:
        all_pos.extend(p.watching_store.get_all_list())

    # Filter just in case
    return [pos for pos in all_pos if pos.get("health_factor", 9.9) <= max_hf]

def remove_position(protocol: str, user: str):
    """Removes a position from all categorized JSON files."""
    p.cleanup_categorized_positions(protocol, user, "none")

# ── Liquidation history ───────────────────────────────────────────────────────
def record_liquidation(
    tx_hash: str,
    protocol: str,
    borrower: str,
    col_token: str,
    debt_token: str,
    debt_usd: float,
    est_profit: float,
    gas_used: int = 0,
    block: int = 0,
    actual_profit: float = 0.0
):
    record = {
        "tx_hash": tx_hash,
        "protocol": protocol,
        "borrower": borrower,
        "collateral_token": col_token,
        "debt_token": debt_token,
        "debt_covered_usd": debt_usd,
        "estimated_profit": est_profit,
        "actual_profit": actual_profit,
        "gas_used": gas_used,
        "block_number": block,
        "timestamp": int(time.time())
    }
    p.history.record_liquidation(record)

def get_stats() -> dict:
    total_borrowers = get_all_borrower_count()
    stats = p.history.get_stats(total_borrowers)

    # Unified counting directly from categorized stores
    stats["crit_count"]   = len(p.critical_store.get_all_list())
    stats["zombie_count"] = len(p.zombies_store.get_all_list())
    stats["warn_count"]   = stats["zombie_count"] # Dashboard uses warn_count for Danger (1.0-1.05)
    stats["watch_count"]  = len(p.watching_store.get_all_list())

    return stats

# ── Scan state ────────────────────────────────────────────────────────────────
def get_last_scan_block(protocol: str) -> int:
    return p.state.get_last_block(protocol)

def set_last_scan_block(protocol: str, block: int):
    p.state.set_last_block(protocol, block)
