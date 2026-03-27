"""
main.py -- LiqBot: Arbitrum Flash Loan Liquidation Bot
"""

import os, sys, time, json, sqlite3, logging, argparse, threading
from pathlib import Path
from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from bot.utils    import load_config, get_web3, get_account, cfg, logger, notify
from bot.database import get_stats as db_get_stats, get_approaching_positions, init_db

app = Flask(__name__, template_folder=str(ROOT / "templates"))
app.config["SECRET_KEY"] = os.urandom(32).hex()
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

_bot_running = threading.Event()
_bot_stats = {"cycle": 0, "last_scan": None, "positions_found": 0}

def start_bot_engine():
    if _bot_running.is_set(): return False
    _bot_running.set()
    threading.Thread(target=_bot_loop, daemon=True).start()
    return True

def stop_bot_engine():
    _bot_running.clear()
    return True

def _bot_loop():
    from bot.monitor import MultiProtocolMonitor
    from bot.liquidator import LiquidationExecutor
    monitor = MultiProtocolMonitor(); executor = LiquidationExecutor()
    monitor.load_all_borrowers()
    while _bot_running.is_set():
        _bot_stats["cycle"] += 1
        pos = monitor.scan_all_protocols()
        if pos: executor.execute_batch(pos)
        _bot_stats["last_scan"] = time.strftime("%H:%M:%S")
        time.sleep(cfg("scanning", "main_loop_interval_seconds") or 20)

@app.route("/")
def page_dashboard(): return render_template("dashboard.html")
@app.route("/positions")
def page_positions(): return render_template("positions.html")
@app.route("/settings")
def page_settings(): return render_template("settings.html")

@app.route("/api/stats")
def api_stats(): return jsonify(db_get_stats())
@app.route("/api/positions")
def api_positions(): return jsonify(get_approaching_positions())

@app.route("/api/zombies")
def api_zombies():
    try:
        with open(ROOT / "zombies.json") as f: return jsonify(json.load(f))
    except: return jsonify({})

@app.route("/api/bot/status")
def api_bot_status():
    return jsonify({"running": _bot_running.is_set(), "cycle": _bot_stats["cycle"], "last_scan": _bot_stats["last_scan"]})
@app.route("/api/bot/start", methods=["POST"])
def api_bot_start(): return jsonify({"ok": start_bot_engine()})
@app.route("/api/bot/stop", methods=["POST"])
def api_bot_stop(): return jsonify({"ok": stop_bot_engine()})

@app.route("/api/mode", methods=["GET", "POST"])
def api_mode():
    if request.method == "POST":
        mode = request.json.get("mode")
        c = load_config(); c["mode"] = mode
        with open(ROOT / "config.json", "w") as f: json.dump(c, f, indent=2)
        import bot.utils as _u; _u._config = None
        return jsonify({"ok": True})
    return jsonify({"mode": cfg("mode") or "simulate"})

def _push_loop():
    while True:
        with app.app_context():
            socketio.emit("stats", db_get_stats())
            socketio.emit("positions", get_approaching_positions())
            try:
                with open(ROOT / "zombies.json") as f: socketio.emit("zombies", json.load(f))
            except: pass
        time.sleep(5)

if __name__ == "__main__":
    init_db()
    threading.Thread(target=_push_loop, daemon=True).start()
    socketio.run(app, host="0.0.0.0", port=5000, allow_unsafe_werkzeug=True)
