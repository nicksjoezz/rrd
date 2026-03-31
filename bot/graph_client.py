"""
graph_client.py — Fetches active borrowers from The Graph (Aave v3).
"""

import requests
import logging
from typing import List
from .utils import cfg, logger

# Standard Aave V3 Arbitrum Subgraph (Messari Standard)
AAVE_V3_ARBITRUM_SUBGRAPH = "https://gateway.thegraph.com/api/[api-key]/subgraphs/id/8mN6YXkX6B6Z3Z2Z2Z2Z2Z2Z2Z2Z2Z2Z"

# Official Aave V3 Arbitrum Subgraph
AAVE_OFFICIAL_SUBGRAPH = "https://api.thegraph.com/subgraphs/name/aave/protocol-v3-arbitrum"

def fetch_active_borrowers(min_health_factor: float = 1.1) -> List[str]:
    """
    Queries The Graph for accounts near liquidation on Aave V3 Arbitrum.
    Uses pagination to fetch the full list.
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

    # Use official Aave subgraph as primary if not configured otherwise
    url = cfg("network", "graph_url") if "graph_url" in cfg("network") else AAVE_OFFICIAL_SUBGRAPH

    try:
        logger.info(f"Using Subgraph: {url}")
        max_hf_wei = str(int(min_health_factor * 1e18)) # For some schemas

        while True:
            # Attempt Official Aave query with HF filter
            resp = requests.post(url, json={
                "query": query_official,
                "variables": {"lastId": last_id, "maxHF": str(min_health_factor)}
            }, timeout=30)

            data = resp.json()

            # Fallback to Messari if official fails or returns error
            if "errors" in data or not data.get("data", {}).get("users"):
                resp = requests.post(url, json={
                    "query": query_messari,
                    "variables": {"lastId": last_id}
                }, timeout=30)
                data = resp.json()

            if "errors" in data:
                logger.error(f"The Graph failed (schema error): {data['errors']}")
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
