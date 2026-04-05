import time
import json
from .persistence import history, borrowers

def get_stats():
    """Get arbitrage statistics."""
    data = history.get_all()
    total_liqs = len(data)
    total_profit = sum(float(r.get("estimated_profit", 0)) for r in data)

    today_ts = int(time.time()) - 86400
    today_liqs = [r for r in data if r.get("timestamp", 0) > today_ts]

    watchlist_count = 0
    try:
        from .utils import ROOT_DIR
        with open(ROOT_DIR / "logs" / "watchlist.json", "r") as f:
            watchlist_count = len(json.load(f))
    except:
        pass

    return {
        "total_liquidations": total_liqs,
        "total_profit_est_usd": round(total_profit, 2),
        "today_liquidations": len(today_liqs),
        "today_profit_est_usd": round(sum(float(r.get("estimated_profit", 0)) for r in today_liqs), 2),
        "total_borrowers": watchlist_count, # Re-using key for dashboard
        "zombie_count": 0, # Placeholder
        "recent": sorted(data, key=lambda x: x.get("timestamp", 0), reverse=True)[:20]
    }

def init_db():
    pass

def get_conn():
    return None

def record_liquidation(record):
    history.record_liquidation(record)

def upsert_borrowers(addresses, protocol):
    pass

def get_borrowers(protocol):
    return []

def upsert_position(pos):
    pass

def remove_position(protocol, user):
    pass

def get_last_scan_block(protocol):
    return 0

def set_last_scan_block(protocol, block):
    pass
