"""
database.py — SQLite-backed persistence layer (Deprecated, now proxying to persistence.py).

Migrated to JSON-based persistence in persistence.py.
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

# ── Positions ─────────────────────────────────────────────────────────────────
def upsert_position(pos: dict):
    p.positions.upsert_position(pos)

def get_approaching_positions(max_hf: float = 1.15) -> List[dict]:
    all_pos = p.positions.get_all_positions()
    return [pos for pos in all_pos if pos.get("health_factor", 9.9) <= max_hf]

def remove_position(protocol: str, user: str):
    p.positions.remove_position(protocol, user)

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

    # Add counts for dashboard/positions using the unified merging logic
    from main import get_merged_positions
    all_merged = get_merged_positions()

    # Zombie count should exactly match what is flagged as 'is_zombie' (HF 1.0 - 1.05)
    stats["zombie_count"] = len([p for p in all_merged if p.get("is_zombie")])
    stats["crit_count"]   = len([p for p in all_merged if p.get("health_factor", 9.9) < 1.0])
    # Danger/Warning count matches the UI 'Danger (1.0-1.05)' chip
    stats["warn_count"]   = len([p for p in all_merged if 1.0 <= p.get("health_factor", 9.9) < 1.05])

    return stats

# ── Scan state ────────────────────────────────────────────────────────────────
def get_last_scan_block(protocol: str) -> int:
    return p.state.get_last_block(protocol)

def set_last_scan_block(protocol: str, block: int):
    p.state.set_last_block(protocol, block)
