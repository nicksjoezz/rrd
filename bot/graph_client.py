"""
graph_client.py — Fetches active borrowers from The Graph (Aave v3).
"""

import requests
import logging
from typing import List
from .utils import cfg, logger

# Standard Aave V3 Arbitrum Subgraph
# Note: Hosted service is being sunset, but this is a common legacy endpoint.
AAVE_V3_ARBITRUM_SUBGRAPH = "https://api.thegraph.com/subgraphs/name/messari/aave-v3-arbitrum"

def fetch_active_borrowers() -> List[str]:
    """
    Queries The Graph for all accounts with debt on Aave V3 Arbitrum.
    Uses pagination to fetch the full list.
    """
    logger.info("Fetching active borrowers from The Graph...")

    # Messari schema uses 'accounts' or 'users'.
    # Let's use a query that works with the Messari Aave V3 schema which is very common.
    query = """
    query GetAccounts($lastId: String) {
      accounts(first: 1000, where: {id_gt: $lastId}) {
        id
      }
    }
    """

    borrowers = []
    last_id = ""

    # Try custom URL from config or default
    url = cfg("network", "graph_url") if "graph_url" in cfg("network") else AAVE_V3_ARBITRUM_SUBGRAPH

    try:
        while True:
            resp = requests.post(url, json={
                "query": query,
                "variables": {"lastId": last_id}
            }, timeout=30)

            data = resp.json()
            if "errors" in data:
                logger.error(f"Graph query error: {data['errors']}")
                break

            accounts = data.get("data", {}).get("accounts", [])
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
