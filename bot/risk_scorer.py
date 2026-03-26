"""
risk_scorer.py — Multi-factor position scoring engine.

Most liquidation bots rank positions purely by debt size or estimated profit.
This scorer uses 7 factors to produce a composite score, letting you:
  - Find high-bonus positions before they get competitive
  - Avoid positions with poor swap liquidity (high slippage risk)
  - Prioritize positions that are most likely to actually execute
  - Skip positions where the HF may recover (wasting your gas)

Scoring factors (all normalized 0–1, higher = better opportunity):
  1. Profit magnitude     — bigger profit = higher score
  2. Liquidation bonus    — 15% bonus (GMX) beats 5% (ETH) at same debt size
  3. HF urgency           — how far below 1.0 (deeper = less likely to recover)
  4. HF velocity          — fast-falling = fire now, slow = wait
  5. E-Mode risk          — E-Mode positions degrade faster on depeg events
  6. Position size        — sweet spot: $2k–$50k (too small = gas risk, too big = slippage)
  7. Collateral liquidity — well-traded assets = cleaner swap execution
"""

import math
import logging
from typing import Optional
from .utils import cfg

logger = logging.getLogger("liquidation_bot.scorer")

# Collateral liquidity tiers (higher = more liquid = cleaner swap)
COLLATERAL_LIQUIDITY = {
    "WETH":   1.0,
    "WBTC":   1.0,
    "USDC":   1.0,
    "USDCe":  1.0,
    "USDT":   0.95,
    "DAI":    0.90,
    "wstETH": 0.85,
    "rETH":   0.80,
    "weETH":  0.75,
    "ezETH":  0.70,
    "ARB":    0.65,
    "LINK":   0.60,
    "GMX":    0.45,   # thinner liquidity but 15% bonus compensates
}

# E-Mode categories — higher risk score = more likely to cascade liquidate
EMODE_RISK = {
    0: 0.2,   # No E-Mode — standard
    1: 0.8,   # ETH-correlated (93% LTV — very sensitive to LST depeg)
    2: 0.6,   # Stablecoins (97% LTV — depeg risk)
    3: 0.7,   # BTC-correlated
}


def score_position(position: dict) -> float:
    """
    Compute composite opportunity score for a position (0–100).
    Higher score = execute this first.

    Required fields in position:
      - health_factor, total_debt_usd, collateral_symbol, collateral_bonus
      - profit_info (from profitability.py)
      - hf_velocity (optional, from velocity.py)
      - emode_category (optional, from emode_detector.py)
    """
    scores = {}

    # ── 1. Profit magnitude (0–1) ─────────────────────────────────────────────
    profit = position.get("profit_info", {}).get("estimated_profit_usd", 0)
    # Score plateaus at $500 profit (above that it's just gravy)
    scores["profit"] = min(profit / 500.0, 1.0)

    # ── 2. Liquidation bonus (0–1) ───────────────────────────────────────────
    bonus = position.get("collateral_bonus", 0.05)
    # 5% bonus → 0.2 score, 15% → 1.0 score
    scores["bonus"] = min((bonus - 0.05) / 0.10 + 0.2, 1.0)

    # ── 3. HF urgency — how far under 1.0 (0–1) ─────────────────────────────
    hf = position.get("health_factor", 1.0)
    if hf >= 1.0:
        # Not yet liquidatable — in zombie queue
        scores["urgency"] = max(0, (1.05 - hf) / 0.05)
    else:
        # Below 1.0 — scale by how far under (deeper = more urgent)
        # 0.99 → 0.5, 0.90 → 0.8, 0.50 → 1.0
        depth = 1.0 - hf
        scores["urgency"] = min(0.5 + depth * 0.5 / 0.5, 1.0)

    # ── 4. HF velocity — fast-falling gets priority (0–1) ────────────────────
    velocity = position.get("hf_velocity")
    if velocity is None:
        scores["velocity"] = 0.3  # Unknown — neutral
    elif velocity >= 0:
        scores["velocity"] = 0.0  # Rising — deprioritize
    else:
        # -0.01/min → 0.3, -0.05/min → 0.7, -0.10/min → 1.0
        scores["velocity"] = min(abs(velocity) / 0.10, 1.0)

    # ── 5. E-Mode category risk (0–1) ────────────────────────────────────────
    emode = position.get("emode_category", 0)
    scores["emode"] = EMODE_RISK.get(emode, 0.3)

    # ── 6. Position size sweet spot (0–1) ────────────────────────────────────
    debt = position.get("total_debt_usd", 0)
    min_debt = cfg("strategy", "min_debt_usd")
    if debt < min_debt:
        scores["size"] = 0.0
    elif debt < 2_000:
        # Small — profitable on Arbitrum but lower priority
        scores["size"] = 0.3 + (debt - min_debt) / (2000 - min_debt) * 0.3
    elif debt <= 50_000:
        # Sweet spot — good profit, manageable slippage
        scores["size"] = 0.6 + (debt - 2000) / (50000 - 2000) * 0.4
    elif debt <= 500_000:
        # Large — still good, slight slippage risk
        scores["size"] = 0.9 - (debt - 50000) / (500000 - 50000) * 0.3
    else:
        # Very large — flash loan capacity risk, heavy slippage
        scores["size"] = max(0.3, 0.6 - (debt - 500000) / 1_000_000 * 0.3)

    # ── 7. Collateral liquidity (0–1) ────────────────────────────────────────
    col_sym = position.get("collateral_symbol", "")
    scores["liquidity"] = COLLATERAL_LIQUIDITY.get(col_sym, 0.5)

    # ── Weighted composite score (0–100) ─────────────────────────────────────
    weights = {
        "profit":    0.30,
        "urgency":   0.25,
        "velocity":  0.15,
        "bonus":     0.12,
        "size":      0.10,
        "liquidity": 0.05,
        "emode":     0.03,
    }

    composite = sum(scores[k] * weights[k] for k in weights) * 100

    # Log breakdown for top positions
    if composite > 50:
        logger.debug(
            f"Score {composite:.1f} | {position.get('user','?')[:8]}... | "
            + " | ".join(f"{k}={v:.2f}" for k, v in scores.items())
        )

    return round(composite, 2)


def rank_by_score(positions: list) -> list:
    """
    Score and sort all positions. Annotates each with 'composite_score'.
    Returns highest-scored positions first.
    """
    for pos in positions:
        pos["composite_score"] = score_position(pos)

    ranked = sorted(positions, key=lambda x: x["composite_score"], reverse=True)
    return ranked


def get_score_breakdown(position: dict) -> dict:
    """Return human-readable score breakdown for a position."""
    col_sym  = position.get("collateral_symbol", "")
    hf       = position.get("health_factor", 1.0)
    velocity = position.get("hf_velocity")
    emode    = position.get("emode_category", 0)
    debt     = position.get("total_debt_usd", 0)
    profit   = position.get("profit_info", {}).get("estimated_profit_usd", 0)
    bonus    = position.get("collateral_bonus", 0.05)

    ttl = position.get("est_minutes_to_liq")
    ttl_str = f"{ttl:.1f} min" if ttl is not None else "unknown"

    return {
        "composite_score":  position.get("composite_score", score_position(position)),
        "health_factor":    hf,
        "est_profit_usd":   profit,
        "bonus_rate":       f"{bonus*100:.1f}%",
        "collateral":       col_sym,
        "liquidity_tier":   COLLATERAL_LIQUIDITY.get(col_sym, 0.5),
        "velocity":         f"{velocity:+.4f}/min" if velocity else "not tracked",
        "time_to_liq":      ttl_str,
        "emode_category":   emode,
        "emode_risk":       EMODE_RISK.get(emode, 0.3),
        "debt_usd":         debt,
    }
