"""
database.py — SQLite-backed persistence layer.

Stores:
  - borrowers   : all known wallet addresses per protocol (instant restart)
  - positions   : last-seen HF, debt, collateral per wallet (approaching positions)
  - liquidations: full history with profit estimates (analytics + dashboard)
  - scan_state  : last scanned block per protocol (incremental scanning)
"""

import sqlite3
import logging
import time
from pathlib import Path
from typing import Optional, List

from .utils import cfg

logger = logging.getLogger("liquidation_bot.db")

DB_PATH = Path(__file__).parent.parent / "logs" / "bot.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create all tables if they don't exist."""
    # Ensure logs dir exists
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = get_conn()
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS borrowers (
            address     TEXT NOT NULL,
            protocol    TEXT NOT NULL,
            first_seen  INTEGER NOT NULL,
            last_seen   INTEGER NOT NULL,
            PRIMARY KEY (address, protocol)
        );

        CREATE TABLE IF NOT EXISTS positions (
            address          TEXT NOT NULL,
            protocol         TEXT NOT NULL,
            health_factor    REAL,
            total_debt_usd   REAL,
            total_col_usd    REAL,
            collateral_token TEXT,
            debt_token       TEXT,
            last_updated     INTEGER,
            PRIMARY KEY (address, protocol)
        );

        CREATE TABLE IF NOT EXISTS liquidations (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            tx_hash          TEXT UNIQUE,
            protocol         TEXT,
            borrower         TEXT,
            collateral_token TEXT,
            debt_token       TEXT,
            debt_covered_usd REAL,
            estimated_profit REAL,
            actual_profit    REAL,
            gas_used         INTEGER,
            block_number     INTEGER,
            timestamp        INTEGER
        );

        CREATE TABLE IF NOT EXISTS scan_state (
            protocol    TEXT PRIMARY KEY,
            last_block  INTEGER DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_borrowers_protocol ON borrowers(protocol);
        CREATE INDEX IF NOT EXISTS idx_positions_hf ON positions(health_factor);
        CREATE INDEX IF NOT EXISTS idx_liq_timestamp ON liquidations(timestamp);
        CREATE INDEX IF NOT EXISTS idx_liq_protocol ON liquidations(protocol);
        """)
        conn.commit()
        logger.debug("Database initialised")
    finally:
        conn.close()


# ── Borrowers ─────────────────────────────────────────────────────────────────

def upsert_borrowers(addresses: List[str], protocol: str):
    if not addresses:
        return
    now  = int(time.time())
    conn = get_conn()
    try:
        conn.executemany(
            """INSERT INTO borrowers (address, protocol, first_seen, last_seen)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(address, protocol)
               DO UPDATE SET last_seen=excluded.last_seen""",
            [(addr.lower(), protocol, now, now) for addr in addresses]
        )
        conn.commit()
    finally:
        conn.close()

def delete_position(address: str, protocol: str):
    conn = get_conn()
    try:
        conn.execute(
            "DELETE FROM positions WHERE address=? AND protocol=?",
            (address.lower(), protocol)
        )
        conn.commit()
    finally:
        conn.close()


def get_borrowers(protocol: str) -> List[str]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT address FROM borrowers WHERE protocol=?", (protocol,)
        ).fetchall()
        return [r["address"] for r in rows]
    finally:
        conn.close()


def get_all_borrower_count() -> int:
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT COUNT(DISTINCT address) FROM borrowers"
        ).fetchone()[0]
    finally:
        conn.close()


# ── Positions ─────────────────────────────────────────────────────────────────

def upsert_position(pos: dict):
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO positions
               (address, protocol, health_factor, total_debt_usd, total_col_usd,
                collateral_token, debt_token, last_updated)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(address, protocol)
               DO UPDATE SET
                 health_factor=excluded.health_factor,
                 total_debt_usd=excluded.total_debt_usd,
                 total_col_usd=excluded.total_col_usd,
                 collateral_token=excluded.collateral_token,
                 debt_token=excluded.debt_token,
                 last_updated=excluded.last_updated""",
            (
                pos.get("user", "").lower(),
                pos.get("protocol", ""),
                pos.get("health_factor"),
                pos.get("total_debt_usd"),
                pos.get("total_col_usd"),
                pos.get("collateral_token"),
                pos.get("debt_token"),
                int(time.time())
            )
        )
        conn.commit()
    finally:
        conn.close()


def get_approaching_positions(max_hf: float = 1.15) -> List[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT * FROM positions
               WHERE health_factor IS NOT NULL AND health_factor <= ?
               ORDER BY health_factor ASC""",
            (max_hf,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def delete_stale_positions(max_age_seconds: int = 86400):
    """Delete positions that haven't been updated in 24 hours."""
    now = int(time.time())
    conn = get_conn()
    try:
        conn.execute("DELETE FROM positions WHERE last_updated < ?", (now - max_age_seconds,))
        conn.commit()
    finally:
        conn.close()


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
    conn = get_conn()
    try:
        conn.execute(
            """INSERT OR IGNORE INTO liquidations
               (tx_hash, protocol, borrower, collateral_token, debt_token,
                debt_covered_usd, estimated_profit, actual_profit,
                gas_used, block_number, timestamp)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (tx_hash, protocol, borrower.lower(), col_token, debt_token,
             debt_usd, est_profit, actual_profit, gas_used, block,
             int(time.time()))
        )
        conn.commit()
    finally:
        conn.close()


def get_stats() -> dict:
    conn = get_conn()
    try:
        total   = conn.execute("SELECT COUNT(*) FROM liquidations").fetchone()[0]
        profit  = conn.execute(
            "SELECT COALESCE(SUM(estimated_profit),0) FROM liquidations"
        ).fetchone()[0]
        today   = int(time.time()) - 86400
        today_r = conn.execute(
            """SELECT COUNT(*), COALESCE(SUM(estimated_profit),0)
               FROM liquidations WHERE timestamp > ?""",
            (today,)
        ).fetchone()
        recent  = conn.execute(
            """SELECT borrower, protocol, collateral_token, debt_token,
                      debt_covered_usd, estimated_profit, timestamp, tx_hash
               FROM liquidations ORDER BY timestamp DESC LIMIT 20"""
        ).fetchall()
        borrowers = conn.execute(
            "SELECT COUNT(DISTINCT address) FROM borrowers"
        ).fetchone()[0]
        return {
            "total_liquidations":   total,
            "total_profit_est_usd": round(float(profit), 2),
            "today_liquidations":   today_r[0],
            "today_profit_est_usd": round(float(today_r[1]), 2),
            "total_borrowers":      borrowers,
            "recent":               [dict(r) for r in recent],
        }
    finally:
        conn.close()


# ── Scan state ────────────────────────────────────────────────────────────────

def get_last_scan_block(protocol: str) -> int:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT last_block FROM scan_state WHERE protocol=?", (protocol,)
        ).fetchone()
        return row["last_block"] if row else 0
    finally:
        conn.close()


def set_last_scan_block(protocol: str, block: int):
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO scan_state (protocol, last_block) VALUES (?,?)
               ON CONFLICT(protocol)
               DO UPDATE SET last_block=excluded.last_block""",
            (protocol, block)
        )
        conn.commit()
    finally:
        conn.close()


# Initialise on import
init_db()
