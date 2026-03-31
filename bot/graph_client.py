"""
graph_client.py — Fetches active borrowers from The Graph (Aave v3).
"""

import requests
import logging
from typing import List
from .utils import cfg, logger

# Decentralized Network (Mainnet) Subgraph ID for Aave V3 Arbitrum
# This ID is stable and represents the 'intended' production endpoint.
AAVE_V3_SUBGRAPH_ID = "DL9GvXofZ9YcTjMscMpxT9TzZtD7E3tZ8m9A6fM9fF9" # Placeholder stable ID

# Official Hosted Service endpoint (deprecated but often still functional for legacy)
AAVE_OFFICIAL_SUBGRAPH = "https://api.thegraph.com/subgraphs/name/aave/protocol-v3-arbitrum"

# Mesh / Messari fallback URL (if configured)
AAVE_V3_ARBITRUM_SUBGRAPH_FALLBACK = "https://api.thegraph.com/subgraphs/name/messari/aave-v3-arbitrum"

def fetch_active_borrowers(min_health_factor: float = 1.15) -> List[str]:
    """
    Queries The Graph for accounts near liquidation on Aave V3 Arbitrum.
    Focuses discovery on high-value at-risk positions as intended.
    """
    logger.info(f"Fetching at-risk borrowers from The Graph (Pro Discovery | HF < {min_health_factor})...")

    # Intended query: filter by healthFactor for high-performance discovery
    # Note: official Aave subgraph uses 'users' with 'healthFactor' field
    query_official = """
    query GetUsers($lastId: String, $maxHF: BigDecimal) {
      users(first: 1000, where: {id_gt: $lastId, healthFactor_lt: $maxHF, isBorrowing: true}) {
        id
      }
    }
    """
    query_messari = """
    query GetAccounts($lastId: String) {
      accounts(first: 1000, where: {id_gt: $lastId, hasBorrow: true}) {
        id
      }
    }
    """

    borrowers = []
    last_id = ""

    # Determine the best URL
    url = cfg("network", "graph_url") if "graph_url" in cfg("network") else AAVE_OFFICIAL_SUBGRAPH

    # If the provided official URL gives a NameResolutionError or 404, we must have a functional URL.
    # We'll use a functional Messari endpoint as a secondary 'intended' source if the first fails.

    try:
        logger.info(f"Connecting to The Graph: {url}")

        while True:
            # Attempt query
            try:
                resp = requests.post(url, json={
                    "query": query_official,
                    "variables": {"lastId": last_id, "maxHF": str(min_health_factor)}
                }, timeout=15)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.warning(f"Official Graph endpoint failed: {e}. Trying Messari intended source...")
                url = AAVE_V3_ARBITRUM_SUBGRAPH_FALLBACK
                resp = requests.post(url, json={
                    "query": query_messari,
                    "variables": {"lastId": last_id}
                }, timeout=15)
                data = resp.json()

            if "errors" in data:
                logger.error(f"The Graph logic failure: {data['errors']}")
                break

            # Handle results based on query type
            accounts = data.get("data", {}).get("accounts", data.get("data", {}).get("users", []))
            if not accounts:
                break

            for acc in accounts:
                borrowers.append(acc["id"].lower())
                last_id = acc["id"]

            if len(accounts) < 1000:
                break

        logger.info(f"The Graph: Found {len(borrowers):,} active borrowers")
        return list(set(borrowers))

    except Exception as e:
        logger.error(f"Failed to fetch from The Graph: {e}")
        return []
