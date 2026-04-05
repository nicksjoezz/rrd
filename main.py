"""
main.py -- ArbBot: Hybrid Sentinel Arbitrage Bot

Single entry point. Starts Flask dashboard + arb scan engine.
"""

import os, sys, time, json, logging, argparse, threading, asyncio
from pathlib import Path

from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from bot.utils    import load_config, get_web3, get_account, cfg, logger, notify
from bot.database import (
    get_stats as db_get_stats, init_db
)
from bot.scout    import update_watchlist
from bot.arb_monitor import ArbMonitor
from bot.executor import ArbExecutor

# -- Flask app -----------------------------------------------------------------
app = Flask(__name__, template_folder=str(ROOT / "templates"))
app.config["SECRET_KEY"] = os.urandom(32).hex()
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

LOG_PATH = ROOT / "logs" / "bot.log"
LOG_PATH.parent.mkdir(exist_ok=True)

# ── Bot engine state ──────────────────────────────────────────────────────────
_bot_running  = threading.Event()
_bot_lock     = threading.Lock()
_bot_stats    = {"cycle": 0, "last_scan": None, "tokens_found": 0}

def bot_is_running():
    return _bot_running.is_set()

def start_bot_engine():
    with _bot_lock:
        if bot_is_running():
            return False
        _bot_running.set()
        threading.Thread(target=_bot_loop_wrapper, daemon=True, name="bot-engine").start()
        logger.info("Arb bot engine started")
        return True

def stop_bot_engine():
    with _bot_lock:
        if not bot_is_running():
            return False
        _bot_running.clear()
        logger.info("Bot engine stopping...")
        return True

def _bot_loop_wrapper():
    asyncio.run(_bot_loop())

async def _bot_loop():
    try:
        executor = ArbExecutor()

        async def on_opportunity(opp):
            logger.info(f"Opportunity found: {opp['symbol']} Gap: {opp['gap']:.2%}")
            await executor.execute(opp)

        monitor = ArbMonitor(on_opportunity=on_opportunity)

        # Start loops
        tasks = [
            asyncio.create_task(monitor.static_scanner_loop()),
            asyncio.create_task(monitor.event_listener()),
            asyncio.create_task(_scout_loop())
        ]

        while _bot_running.is_set():
            await asyncio.sleep(1)

        for task in tasks:
            task.cancel()

    except Exception as e:
        logger.error(f"Bot engine fatal error: {e}", exc_info=True)
    finally:
        _bot_running.clear()
        logger.info("Bot engine stopped")

async def _scout_loop():
    while _bot_running.is_set():
        await update_watchlist()
        await asyncio.sleep(1800) # Every 30 mins

# ── Pages ─────────────────────────────────────────────────────────────────────
@app.route("/")
def page_dashboard():  return render_template("dashboard.html")
@app.route("/positions")
def page_positions():  return render_template("positions.html")
@app.route("/terminal")
def page_terminal():   return render_template("terminal.html")
@app.route("/analytics")
def page_analytics():  return render_template("analytics.html")
@app.route("/settings")
def page_settings():   return render_template("settings.html")
@app.route("/wallet")
def page_wallet():     return render_template("wallet.html")

# ── API: stats & positions ────────────────────────────────────────────────────
@app.route("/api/stats")
def api_stats():
    # Adapt to arbitrage stats
    stats = db_get_stats()
    return jsonify(stats)

@app.route("/api/positions")
def api_positions():
    # Return watchlist for now
    try:
        with open(ROOT / "logs" / "watchlist.json", "r") as f:
            return jsonify(json.load(f))
    except:
        return jsonify([])

@app.route("/api/bot/status")
def api_bot_status():
    c = load_config()
    return jsonify({
        "running":   bot_is_running(),
        "mode":      c.get("mode", "simulate"),
        "cycle":     _bot_stats["cycle"],
        "last_scan": _bot_stats["last_scan"],
        "network":   c.get("network", {}).get("chain_name", "Arbitrum One"),
        "contract":  (c.get("wallet", {}).get("arb_contract", "") or "")[:10] + "...",
    })

@app.route("/api/bot/start", methods=["POST"])
def api_bot_start():
    ok = start_bot_engine()
    return jsonify({"ok": ok, "msg": "Started -- monitoring 24/7" if ok else "Already running"})

@app.route("/api/bot/stop", methods=["POST"])
def api_bot_stop():
    ok = stop_bot_engine()
    return jsonify({"ok": ok, "msg": "Stopping..." if ok else "Not running"})

@app.route("/api/mode", methods=["POST"])
def api_mode_set():
    try:
        new_mode = (request.get_json() or {}).get("mode", "simulate")
        cfg_data = load_config()
        cfg_data["mode"] = new_mode
        with open(ROOT / "config.json", "w") as f:
            json.dump(cfg_data, f, indent=2)
        import bot.utils as _u; _u._config = None
        return jsonify({"ok": True, "mode": new_mode})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

# ── Real-time SocketIO push ───────────────────────────────────────────────────
def _push_loop():
    while True:
        with app.app_context():
            try:
                stats = db_get_stats()
                socketio.emit("stats", stats)
                socketio.emit("bot_status", {
                    "running":   bot_is_running(),
                    "mode":      (load_config() or {}).get("mode", "simulate"),
                })
                # Emit watchlist as positions to keep frontend logic working
                try:
                    with open(ROOT / "logs" / "watchlist.json", "r") as f:
                        socketio.emit("positions", json.load(f))
                except:
                    pass
            except Exception:
                pass
        time.sleep(1)

# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="ArbBot — Sentinel Hybrid Arbitrage Bot")
    parser.add_argument("--port",   type=int, default=5000)
    parser.add_argument("--host",   type=str, default="0.0.0.0")
    args = parser.parse_args()

    threading.Thread(target=_push_loop, daemon=True, name="push").start()

    socketio.run(app, host=args.host, port=args.port, debug=False, log_output=False, allow_unsafe_werkzeug=True)

if __name__ == "__main__":
    main()
