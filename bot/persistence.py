import json
import time
from pathlib import Path
from .utils import ROOT_DIR, logger

HISTORY_PATH = ROOT_DIR / "logs" / "history.json"

class HistoryStore:
    def __init__(self):
        self._ensure_exists()

    def _ensure_exists(self):
        HISTORY_PATH.parent.mkdir(exist_ok=True)
        if not HISTORY_PATH.exists():
            with open(HISTORY_PATH, "w") as f:
                json.dump([], f)

    def record_execution(self, record):
        try:
            with open(HISTORY_PATH, "r") as f:
                data = json.load(f)
            data.append(record)
            # Keep last 100 records
            data = data[-100:]
            with open(HISTORY_PATH, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to record history: {e}")

    def get_stats(self, watchlist_count):
        try:
            with open(HISTORY_PATH, "r") as f:
                data = json.load(f)

            total_profit = sum(float(r.get("estimated_profit", 0)) for r in data)
            total_liqs = len(data)

            # Filter today's profit
            now = time.time()
            today_start = now - (now % 86400)
            today_records = [r for r in data if r.get("timestamp", 0) >= today_start]
            today_profit = sum(float(r.get("estimated_profit", 0)) for r in today_records)
            today_liqs = len(today_records)

            return {
                "total_profit_est_usd": total_profit,
                "today_profit_est_usd": today_profit,
                "total_liquidations": total_liqs,
                "today_liquidations": today_liqs,
                "total_borrowers": watchlist_count, # Using for watchlist size
                "zombie_count": "0.00%", # Placeholder for max gap
                "recent": data[::-1][:10]
            }
        except Exception as e:
            logger.error(f"Failed to get stats: {e}")
            return {
                "total_profit_est_usd": 0, "today_profit_est_usd": 0,
                "total_liquidations": 0, "today_liquidations": 0,
                "total_borrowers": watchlist_count, "zombie_count": "0.00%",
                "recent": []
            }

history = HistoryStore()
