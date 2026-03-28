"""
main.py -- LiqBot: Arbitrum Flash Loan Liquidation Bot

Single entry point. Starts Flask dashboard + bot scan engine together.
The bot runs 24/7 in a background thread, scanning for liquidation
opportunities and executing them immediately when found.

    python main.py              start everything (port 5000)
    python main.py --port 8080  custom port
    python main.py --no-bot     UI only -- start bot via dashboard button

For a terminal opportunity snapshot:
    python scan_test.py
"""

import os, sys, time, json, sqlite3, logging, argparse, threading
from pathlib import Path

from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from bot.utils    import load_config, get_web3, get_account, cfg, logger, notify
from bot.database import (
    get_stats as db_get_stats, get_approaching_positions, init_db
)

# -- Flask app -----------------------------------------------------------------
app = Flask(__name__, template_folder=str(ROOT / "templates"))
app.config["SECRET_KEY"] = os.urandom(32).hex()
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

LOG_PATH = ROOT / "logs" / "bot.log"
LOG_PATH.parent.mkdir(exist_ok=True)

# ── Bot engine state ──────────────────────────────────────────────────────────
_bot_running  = threading.Event()
_bot_lock     = threading.Lock()
_bot_stats    = {"cycle": 0, "last_scan": None, "positions_found": 0}

def bot_is_running():
    return _bot_running.is_set()

def start_bot_engine():
    with _bot_lock:
        if bot_is_running():
            return False
        _bot_running.set()
        threading.Thread(target=_bot_loop, daemon=True, name="bot-engine").start()
        logger.info("Bot engine started -- scanning 24/7")
        return True

def stop_bot_engine():
    with _bot_lock:
        if not bot_is_running():
            return False
        _bot_running.clear()
        logger.info("Bot engine stopping...")
        return True

# -- Bot loop -- runs 24/7, executes immediately when opportunities found --------
def _bot_loop():
    try:
        from bot.monitor         import MultiProtocolMonitor
        from bot.liquidator      import LiquidationExecutor
        from bot.profitability   import rank_positions
        from bot.oracle_watcher  import OracleWatcher
        from bot.mempool_watcher import start_mempool_watcher_thread
        from bot.gas_manager     import log_gas_summary, is_gas_spike
        from bot.risk_scorer     import rank_by_score
        from bot.auto_tuner      import get_tuner
        from bot.ws_streamer     import WebSocketStreamer
        from bot.emode_detector  import flag_emode_risk_positions

        monitor  = MultiProtocolMonitor()
        executor = LiquidationExecutor()
        tuner    = get_tuner()
        _emerg   = threading.Event()

        # ── Emergency scan triggers ───────────────────────────────────────────
        def on_price_drop(move):
            logger.warning(
                f"Oracle drop: {move['feed']} {move['pct_change']:+.2f}% "
                f"-- emergency scan triggered"
            )
            _emerg.set()

        oracle  = OracleWatcher(on_price_drop=on_price_drop)
        mempool = start_mempool_watcher_thread(
            on_oracle_pending=lambda ev: (
                logger.info(f"Pending oracle update: {ev['feed_name']} -- triggering scan"),
                _emerg.set()
            )
        )

        # Real-time event streaming -- adds changed wallets to hotlist for
        # immediate priority re-check rather than waiting for next cycle
        streamer = WebSocketStreamer()
        streamer.start()

        # Load manually added/persistent zombies into monitors
        # We also need to add these to the monitor discovery list
        from bot.zombie_queue import ZombieQueue
        zq = ZombieQueue(
            entry_hf=(load_config().get("strategy", {}).get("zombie_queue", {}).get("entry_hf", 1.05)),
            fire_hf=(load_config().get("strategy", {}).get("zombie_queue", {}).get("fire_hf", 1.0))
        )
        zombies = zq.get_watching()
        for z in zombies:
            proto = z.get("protocol")
            user  = z.get("user")
            if proto and user and proto in monitor.monitors:
                monitor.monitors[proto]._borrowers.add(user.lower())
                logger.info(f"[ZOMBIE] Loaded {user[:8]} from persistence into {proto} monitor")

        # ── Load borrowers ────────────────────────────────────────────────────
        logger.info("Loading borrower history (DB cache -> event scan)...")
        monitor.load_all_borrowers()
        s = monitor.get_stats()
        logger.info(
            f"Ready to scan | Protocols: {s['protocols']} | "
            f"Borrowers: {s['total_borrowers']:,} | "
            f"Zombie queue: {s['zombie_watching']}"
        )
        notify(f"Bot started\nProtocols: {', '.join(s['protocols'])}\nBorrowers: {s['total_borrowers']:,}")

        # ── 24/7 scan loop ────────────────────────────────────────────────────
        cycle = 0
        while _bot_running.is_set():
            cycle += 1
            _bot_stats["cycle"] = cycle
            tuner.record_cycle()

            try:
                # Emergency scan — fires immediately on oracle/mempool signals
                if _emerg.is_set():
                    _emerg.clear()
                    logger.info("!! Emergency scan -- oracle/mempool signal")
                    _scan_and_execute(monitor, executor, tuner, emergency=True)

                # Log current state
                try:
                    block     = get_web3().eth.block_number
                    gas_gwei  = get_web3().eth.gas_price / 1e9
                except Exception:
                    block, gas_gwei = 0, 0

                logger.info(f"{'-'*56}")
                logger.info(
                    f"Cycle #{cycle} | Block: {block:,} | "
                    f"Gas: {gas_gwei:.4f} gwei | "
                    f"Mode: {(load_config() or {}).get('mode','live').upper()}"
                )

                # Check oracle price feeds for large moves
                oracle.check_all_feeds()

                # Priority re-check wallets with recent on-chain activity
                hot = streamer.get_and_clear_hotlist()
                if hot:
                    logger.info(f"[WS] {len(hot)} wallets had on-chain activity -- priority check")

                # Main scan → rank → execute immediately if opportunities found
                found = _scan_and_execute(monitor, executor, tuner)
                _bot_stats["positions_found"] = found
                _bot_stats["last_scan"] = time.strftime("%H:%M:%S")

                # Adaptive tuning every 50 cycles
                tuner.tune()

                # Refresh borrower list with new Borrow events
                monitor.refresh_borrowers()

                # Log cycle summary
                db   = db_get_stats()
                mst  = monitor.get_stats()
                logger.info(
                    f"Cycle #{cycle} complete | "
                    f"Borrowers: {mst['total_borrowers']:,} | "
                    f"Zombie watching: {mst['zombie_watching']} | "
                    f"Total liquidations: {db['total_liquidations']} | "
                    f"All-time profit: ${db['total_profit_est_usd']:,.2f}"
                )

            except Exception as e:
                logger.error(f"Bot loop error: {e}", exc_info=True)
                notify(f"[WARN] Bot error: {str(e)[:200]}")

            # Adaptive interval from auto-tuner (speeds up during volatile markets)
            interval = tuner.get_effective_params()["scan_interval"]
            for _ in range(interval):
                if not _bot_running.is_set():
                    break
                # Check for emergency signals during sleep too
                if _emerg.is_set():
                    break
                time.sleep(1)

    except Exception as e:
        logger.error(f"Bot engine fatal error: {e}", exc_info=True)
    finally:
        _bot_running.clear()
        logger.info("Bot engine stopped")


def _scan_and_execute(monitor, executor, tuner, emergency=False):
    """
    Full scan → score → filter → execute pipeline.
    Called every cycle AND immediately on oracle/mempool signals.
    Returns number of opportunities found.
    """
    from bot.profitability  import rank_positions
    from bot.risk_scorer    import rank_by_score
    from bot.emode_detector import flag_emode_risk_positions
    from bot.gas_manager    import is_gas_spike

    # Skip gas spikes unless it's an emergency scan
    if not emergency and is_gas_spike():
        logger.warning("Gas spike detected -- skipping non-emergency scan")
        return 0

    # Scan all enabled protocols
    positions = monitor.scan_all_protocols()
    if not positions:
        logger.info("No positions near liquidation threshold")
        return 0

    # Enrich with E-Mode data (ETH LST depeg awareness)
    positions = flag_emode_risk_positions(positions)

    # Velocity: bubble fast-falling positions to the top
    fast = monitor.velocity.get_fast_falling(positions, velocity_threshold=-0.03)
    if fast:
        logger.info(f"[!] {len(fast)} fast-falling position(s) detected -- priority execution")
        positions = fast + [p for p in positions if p not in fast]

    # Score by 7-factor composite (profit, bonus, urgency, velocity, size, liquidity, emode)
    skip   = set(tuner.get_effective_params().get("collateral_skip", []))
    ranked = rank_positions(rank_by_score(
        [p for p in positions if p.get("collateral_symbol") not in skip]
    ))

    if not ranked:
        logger.info("No profitable positions after scoring and filtering")
        return 0

    mode = (load_config() or {}).get("mode", "live")
    logger.info(
        f"{'[SIMULATE]' if mode == 'simulate' else '[LIVE]'} | "
        f"{len(ranked)} position(s) to execute:"
    )
    for i, pos in enumerate(ranked[:5], 1):
        pi  = pos.get("profit_info", {})
        vel = pos.get("hf_velocity")
        ttl = pos.get("est_minutes_to_liq")
        logger.info(
            f"  #{i} {pos['user'][:10]}... "
            f"HF={pos['health_factor']:.4f}"
            + (f" down {abs(vel):.3f}/m" if vel and vel < 0 else "")
            + (f" Time {ttl:.1f}m" if ttl else "")
            + f" | ${pi.get('estimated_profit_usd', 0):.2f} est."
            f" | {pos.get('collateral_symbol','?')} ({pos.get('collateral_bonus',0)*100:.0f}% bonus)"
            f" | [{pos.get('protocol','?')}]"
        )

    # Execute immediately — no delay between scan and execution
    results = executor.execute_batch(ranked)

    for pos in ranked[:len(results)]:
        tuner.record_success(
            pos.get("collateral_symbol", ""),
            pos.get("profit_info", {}).get("estimated_profit_usd", 0)
        )

    if results:
        socketio.emit("liquidation_executed", {
            "count": len(results),
            "mode":  mode,
        })

    return len(ranked)


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
    return jsonify(db_get_stats())

@app.route("/api/profit-history")
def api_profit_history():
    try:
        con  = sqlite3.connect(str(ROOT / "logs" / "bot.db"))
        rows = con.execute(
            """SELECT date(timestamp,'unixepoch') as day,
                      COUNT(*) as count,
                      COALESCE(SUM(estimated_profit),0) as profit
               FROM liquidations
               GROUP BY day ORDER BY day DESC LIMIT 30"""
        ).fetchall()
        con.close()
        return jsonify([
            {"day": r[0], "count": r[1], "profit": round(float(r[2]), 2)}
            for r in rows
        ])
    except Exception:
        return jsonify([])

def get_merged_positions():
    # 1. Get positions from database (approaching liquidation)
    positions = get_approaching_positions()

    # 2. Get positions from zombie queue (monitored but not yet in DB or with different HF)
    try:
        from bot.zombie_queue import ZombieQueue
        zq = ZombieQueue(
            entry_hf=(load_config().get("strategy", {}).get("zombie_queue", {}).get("entry_hf", 1.05)),
            fire_hf=(load_config().get("strategy", {}).get("zombie_queue", {}).get("fire_hf", 1.0))
        )
        zombies = zq.get_watching()

        pos_map = {f"{p['protocol']}:{p['address'].lower()}": p for p in positions}
        for z in zombies:
            key = f"{z['protocol']}:{z['user'].lower()}"
            if key not in pos_map:
                pos_map[key] = {
                    "address":          z["user"],
                    "protocol":         z["protocol"],
                    "health_factor":    z.get("health_factor"),
                    "total_debt_usd":   z.get("total_debt_usd"),
                    "total_col_usd":    z.get("total_col_usd"),
                    "collateral_token": z.get("collateral_token"),
                    "debt_token":       z.get("debt_token"),
                    "last_updated":     z.get("queued_at"),
                    "is_zombie":        True
                }
            else:
                pos_map[key]["is_zombie"] = True

        return sorted(pos_map.values(), key=lambda x: x.get("health_factor", 9.9))
    except Exception as e:
        logger.error(f"Error merging zombie positions: {e}")
        return positions

@app.route("/api/positions")
def api_positions():
    return jsonify(get_merged_positions())

@app.route("/api/logs")
def api_logs():
    try:
        n = int(request.args.get("lines", 100))
        if LOG_PATH.exists():
            lines = LOG_PATH.read_text(errors="replace").splitlines()
            return jsonify({"lines": lines[-n:]})
    except Exception as e:
        return jsonify({"lines": [], "error": str(e)})
    return jsonify({"lines": []})


# ── API: bot control ──────────────────────────────────────────────────────────
@app.route("/api/bot/status")
def api_bot_status():
    c = load_config()
    return jsonify({
        "running":   bot_is_running(),
        "mode":      c.get("mode", "simulate"),
        "cycle":     _bot_stats["cycle"],
        "last_scan": _bot_stats["last_scan"],
        "network":   c.get("network", {}).get("chain_name", "Arbitrum One"),
        "contract":  (c.get("wallet", {}).get("liquidator_contract", "") or "")[:10] + "...",
        "protocols": {k: v.get("enabled") for k, v in c.get("protocols", {}).items()},
    })

@app.route("/api/bot/start", methods=["POST"])
def api_bot_start():
    ok = start_bot_engine()
    return jsonify({"ok": ok, "msg": "Started -- scanning 24/7" if ok else "Already running"})

@app.route("/api/bot/stop", methods=["POST"])
def api_bot_stop():
    ok = stop_bot_engine()
    return jsonify({"ok": ok, "msg": "Stopping..." if ok else "Not running"})


# ── API: config ───────────────────────────────────────────────────────────────
@app.route("/api/config")
def api_config_get():
    c  = load_config()
    m  = json.loads(json.dumps(c))
    pk = m.get("wallet", {}).get("private_key", "")
    if pk and pk != "YOUR_PRIVATE_KEY_HERE" and len(pk) > 10:
        m["wallet"]["private_key"] = pk[:6] + "••••••••" + pk[-4:]
    return jsonify(m)

@app.route("/api/config", methods=["POST"])
def api_config_save():
    try:
        new = request.get_json()
        cur = load_config()
        pk  = new.get("wallet", {}).get("private_key", "")
        if "••••••••" in pk:
            new["wallet"]["private_key"] = cur.get("wallet", {}).get("private_key", pk)
        with open(ROOT / "config.json", "w") as f:
            json.dump(new, f, indent=2)
        import bot.utils as _u; _u._config = None
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


# ── API: mode toggle ──────────────────────────────────────────────────────────
@app.route("/api/mode")
def api_mode_get():
    return jsonify({"mode": (load_config() or {}).get("mode", "simulate")})

@app.route("/api/mode", methods=["POST"])
def api_mode_set():
    try:
        new_mode = (request.get_json() or {}).get("mode", "simulate")
        if new_mode not in ("simulate", "live"):
            return jsonify({"ok": False, "msg": "mode must be 'simulate' or 'live'"})
        cfg_data = load_config()
        cfg_data["mode"] = new_mode
        with open(ROOT / "config.json", "w") as f:
            json.dump(cfg_data, f, indent=2)
        import bot.utils as _u; _u._config = None
        logger.info(f"Mode switched to {new_mode.upper()} -- takes effect immediately")
        return jsonify({"ok": True, "mode": new_mode})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


# ── API: wallet & withdraw ────────────────────────────────────────────────────
@app.route("/api/wallet/balances")
def api_wallet_balances():
    try:
        from bot.utils import get_web3, get_account, cfg as _cfg, checksum, ERC20_ABI
        from bot.profitability import get_token_price_usd

        w3            = get_web3()
        account       = get_account()
        contract_addr = _cfg("wallet", "liquidator_contract")
        
        if not account:
            return jsonify({
                "wallet":    "Not Configured",
                "eth":       0,
                "eth_usd":   0,
                "contract":  contract_addr,
                "balances":  {},
                "total_usd": 0,
                "error":     "Private key not set"
            })

        eth_wei       = w3.eth.get_balance(account.address)
        eth_bal       = float(w3.from_wei(eth_wei, "ether"))
        eth_price     = get_token_price_usd("0x82aF49447D8a07e3bd95BD0d56f35241523fBab1") or 3000.0

        result = {
            "wallet":    account.address,
            "eth":       round(eth_bal, 6),
            "eth_usd":   round(eth_bal * eth_price, 2),
            "contract":  contract_addr,
            "balances":  {},
            "total_usd": 0.0,
        }

        if not contract_addr or contract_addr == "DEPLOY_CONTRACT_ADDRESS_HERE":
            return jsonify(result)

        tokens = _cfg("tokens")
        total  = 0.0
        for sym, info in tokens.items():
            try:
                tok = w3.eth.contract(address=checksum(info["address"]), abi=ERC20_ABI)
                raw = tok.functions.balanceOf(checksum(contract_addr)).call()
                if raw == 0: continue
                amount = raw / (10 ** info["decimals"])
                price  = get_token_price_usd(info["address"]) or 0
                usd    = amount * price
                total += usd
                result["balances"][sym] = {
                    "amount": round(amount, 6),
                    "usd":    round(usd, 2),
                    "address": info["address"],
                }
            except Exception:
                pass

        result["total_usd"] = round(total, 2)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e), "balances": {}, "total_usd": 0})


@app.route("/api/wallet/withdraw", methods=["POST"])
def api_wallet_withdraw():
    from bot.utils import get_web3, get_account, cfg as _cfg, checksum, ERC20_ABI, LIQUIDATOR_CONTRACT_ABI

    if (load_config() or {}).get("mode", "simulate") == "simulate":
        return jsonify({
            "ok":  False,
            "msg": "Switch to LIVE mode in Settings before withdrawing"
        })

    data          = request.get_json() or {}
    token_sym     = data.get("token")
    w3            = get_web3()
    account       = get_account()
    contract_addr = _cfg("wallet", "liquidator_contract")

    if not account:
        return jsonify({"ok": False, "msg": "Private key not set in Settings"})
    if not contract_addr or contract_addr == "DEPLOY_CONTRACT_ADDRESS_HERE":
        return jsonify({"ok": False, "msg": "Contract not deployed or address not set"})

    contract = w3.eth.contract(address=checksum(contract_addr), abi=LIQUIDATOR_CONTRACT_ABI)
    try:
        owner = contract.functions.owner().call()
        if owner.lower() != account.address.lower():
            return jsonify({"ok": False, "msg": f"Wallet is not contract owner ({owner[:10]}...)"})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"Ownership check failed: {e}"})

    gas_cfg = _cfg("gas")
    tokens  = _cfg("tokens")
    if token_sym:
        tokens = {k: v for k, v in tokens.items() if k == token_sym}

    results = []
    for sym, info in tokens.items():
        try:
            tok     = w3.eth.contract(address=checksum(info["address"]), abi=ERC20_ABI)
            raw_bal = tok.functions.balanceOf(checksum(contract_addr)).call()
            if raw_bal == 0:
                results.append({"token": sym, "status": "skipped", "msg": "zero balance"})
                continue
            nonce = w3.eth.get_transaction_count(account.address)
            tx    = contract.functions.withdraw(checksum(info["address"])).build_transaction({
                "from": account.address, "nonce": nonce, "gas": 120_000,
                "maxFeePerGas":         w3.to_wei(gas_cfg["max_fee_per_gas_gwei"], "gwei"),
                "maxPriorityFeePerGas": w3.to_wei(gas_cfg["max_priority_fee_gwei"], "gwei"),
                "chainId":              _cfg("network", "chain_id"),
            })
            signed  = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            amount  = raw_bal / (10 ** info["decimals"])
            if receipt.status == 1:
                logger.info(f"Withdrew {amount:.4f} {sym} | TX: {tx_hash.hex()[:20]}")
                results.append({"token": sym, "status": "success", "amount": round(amount, 6), "tx_hash": tx_hash.hex()})
            else:
                results.append({"token": sym, "status": "failed", "tx_hash": tx_hash.hex()})
        except Exception as e:
            results.append({"token": sym, "status": "error", "msg": str(e)})

    return jsonify({"ok": True, "results": results})


# ── API: health check ─────────────────────────────────────────────────────────
@app.route("/api/health-check")
def api_health_check():
    import time as _time
    from bot.utils import AAVE_POOL_ABI, CHAINLINK_FEED_ABI

    checks = []
    def add(name, status, detail):
        checks.append({"name": name, "status": status, "detail": detail})

    cfg_data = load_config()

    pk = cfg_data.get("wallet", {}).get("private_key", "")
    if not pk or pk == "YOUR_PRIVATE_KEY_HERE":
        add("Private Key", "fail", "Not set in config.json")
    else:
        add("Private Key", "ok", pk[:6] + "••••" + pk[-4:])

    contract = cfg_data.get("wallet", {}).get("liquidator_contract", "")
    if not contract or contract == "DEPLOY_CONTRACT_ADDRESS_HERE":
        add("Contract Address", "warn", "Deploy contracts/FlashLoanLiquidator.sol first")
    else:
        add("Contract Address", "ok", contract[:10] + "...")

    rpc = cfg_data.get("network", {}).get("rpc_http", "")
    if not rpc:
        add("RPC Endpoint", "fail", "network.rpc_http not set")
        return jsonify({"checks": checks, "summary": "fail"})
    add("RPC Endpoint", "ok", rpc[:45] + ("…" if len(rpc) > 45 else ""))

    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
        if not w3.is_connected():
            add("RPC Connection", "fail", "Connected but not responding")
            return jsonify({"checks": checks, "summary": "fail"})
        block = w3.eth.block_number
        gas   = w3.eth.gas_price / 1e9
        add("RPC Connection", "ok", f"Block {block:,} | Gas {gas:.4f} gwei")
    except Exception as e:
        add("RPC Connection", "fail", str(e)[:80])
        return jsonify({"checks": checks, "summary": "fail"})

    try:
        from eth_account import Account as _A
        acc  = _A.from_key(pk)
        bal  = float(w3.from_wei(w3.eth.get_balance(acc.address), "ether"))
        if bal >= 0.01:   add("Wallet ETH", "ok",   f"{acc.address[:10]}... | {bal:.5f} ETH")
        elif bal >= 0.002: add("Wallet ETH", "warn", f"{acc.address[:10]}... | {bal:.5f} ETH (low)")
        else:              add("Wallet ETH", "fail", f"{acc.address[:10]}... | {bal:.6f} ETH (too low)")
    except Exception as e:
        add("Wallet ETH", "fail", str(e)[:80])

    if contract and contract != "DEPLOY_CONTRACT_ADDRESS_HERE":
        try:
            code = w3.eth.get_code(w3.to_checksum_address(contract))
            if len(code) > 2: add("Liquidator Contract", "ok",   f"{contract[:10]}... ({len(code)} bytes)")
            else:              add("Liquidator Contract", "fail", "No code — deploy contract")
        except Exception as e:
            add("Liquidator Contract", "fail", str(e)[:80])
    else:
        add("Liquidator Contract", "warn", "Not deployed yet")

    for name, pcfg in cfg_data.get("protocols", {}).items():
        if not pcfg.get("enabled"):
            add(f"Protocol: {name}", "warn", "Disabled"); continue
        pool = pcfg.get("pool", "")
        if not pool or pool.startswith("0x000"):
            add(f"Protocol: {name}", "warn", "No pool address"); continue
        try:
            p = w3.eth.contract(address=w3.to_checksum_address(pool), abi=AAVE_POOL_ABI)
            p.functions.getUserAccountData("0x0000000000000000000000000000000000000001").call()
            add(f"Protocol: {name}", "ok", pool[:10] + "... responsive")
        except Exception as e:
            add(f"Protocol: {name}", "fail", str(e)[:60])

    try:
        vault = cfg_data.get("flash_loan", {}).get("balancer_vault", "0xBA12222222228d8Ba445958a75a0704d566BF2C8")
        code  = w3.eth.get_code(w3.to_checksum_address(vault))
        add("Balancer Vault (0% fee)", "ok" if len(code) > 2 else "fail",
            vault[:10] + "... reachable" if len(code) > 2 else "Not found")
    except Exception as e:
        add("Balancer Vault (0% fee)", "fail", str(e)[:60])

    if cfg_data.get("oracle", {}).get("watch_chainlink"):
        for feed_name, addr in cfg_data.get("oracle", {}).get("chainlink_feeds", {}).items():
            try:
                from bot.utils import CHAINLINK_FEED_ABI as CL_ABI
                feed  = w3.eth.contract(address=w3.to_checksum_address(addr), abi=CL_ABI)
                data  = feed.functions.latestRoundData().call()
                price = data[1] / 1e8
                age   = int(_time.time()) - data[3]
                add(f"Oracle: {feed_name}", "ok" if age < 3600 else "warn",
                    f"${price:,.2f} ({age}s ago)" if age < 3600 else f"${price:,.2f} (stale {age//3600}h)")
            except Exception as e:
                add(f"Oracle: {feed_name}", "fail", str(e)[:60])
    else:
        add("Oracle Feeds", "warn", "Chainlink watching disabled")

    try:
        code = w3.eth.get_code(w3.to_checksum_address("0xb27308f9F90D607463bb33eA1BeBb41C27CE5AB6"))
        add("Uniswap V3 Quoter", "ok" if len(code) > 2 else "fail",
            "Reachable" if len(code) > 2 else "Not found")
    except Exception as e:
        add("Uniswap V3 Quoter", "fail", str(e)[:60])

    failed  = sum(1 for c in checks if c["status"] == "fail")
    warned  = sum(1 for c in checks if c["status"] == "warn")
    summary = "fail" if failed else "warn" if warned else "ok"
    return jsonify({"checks": checks, "summary": summary, "failed": failed, "warned": warned})


# ── API: analytics ────────────────────────────────────────────────────────────
@app.route("/api/analytics")
def api_analytics():
    """Full analytics breakdown by protocol, collateral, and time period."""
    try:
        days = int(request.args.get("days", 30))
        since = int(time.time()) - (days * 86400)
        con   = sqlite3.connect(str(ROOT / "logs" / "bot.db"))
        con.row_factory = sqlite3.Row

        # All-time stats
        all_total  = con.execute("SELECT COUNT(*), COALESCE(SUM(estimated_profit),0) FROM liquidations").fetchone()
        # Period stats
        period     = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(estimated_profit),0) FROM liquidations WHERE timestamp > ?",
            (since,)
        ).fetchone()
        # By protocol
        by_proto   = con.execute(
            """SELECT protocol, COUNT(*) as count, COALESCE(SUM(estimated_profit),0) as profit
               FROM liquidations WHERE timestamp > ? GROUP BY protocol""", (since,)
        ).fetchall()
        # By collateral
        by_col     = con.execute(
            """SELECT collateral_token, COUNT(*) as count, COALESCE(SUM(estimated_profit),0) as profit
               FROM liquidations WHERE timestamp > ? GROUP BY collateral_token ORDER BY profit DESC""",
            (since,)
        ).fetchall()
        # Daily
        daily      = con.execute(
            """SELECT date(timestamp,'unixepoch') as day, COUNT(*), COALESCE(SUM(estimated_profit),0)
               FROM liquidations WHERE timestamp > ? GROUP BY day ORDER BY day""", (since,)
        ).fetchall()
        con.close()

        return jsonify({
            "days":        days,
            "all_time":    {"count": all_total[0], "profit": round(float(all_total[1]),2)},
            "period":      {"count": period[0],    "profit": round(float(period[1]),2)},
            "by_protocol": [{"protocol": r[0], "count": r[1], "profit": round(float(r[2]),2)} for r in by_proto],
            "by_collateral":[{"token": r[0], "count": r[1], "profit": round(float(r[2]),2)} for r in by_col],
            "daily":       [{"day": r[0], "count": r[1], "profit": round(float(r[2]),2)} for r in daily],
        })
    except Exception as e:
        return jsonify({"error": str(e)})


# ── Real-time SocketIO push ───────────────────────────────────────────────────
def _push_loop():
    while True:
        with app.app_context():
            try:
                stats = db_get_stats()
                socketio.emit("stats",      stats)
                socketio.emit("bot_status", {
                    "running":   bot_is_running(),
                    "mode":      (load_config() or {}).get("mode", "simulate"),
                    "cycle":     _bot_stats["cycle"],
                    "last_scan": _bot_stats["last_scan"],
                })
                socketio.emit("positions",  get_merged_positions()[:20])
            except Exception:
                pass
        time.sleep(3)


def _log_tail():
    """Tail the log file and push new lines to connected browsers."""
    pos = 0
    while True:
        try:
            if LOG_PATH.exists():
                with open(LOG_PATH, errors="replace") as f:
                    f.seek(pos)
                    new = f.read()
                    pos = f.tell()
                    if new:
                        for line in new.splitlines():
                            if line.strip():
                                socketio.emit("log_line", {"line": line})
        except Exception:
            pass
        time.sleep(1)


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="LiqBot — Arbitrum Flash Loan Liquidation Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Terminal scan tool: python scan_test.py"
    )
    parser.add_argument("--port",   type=int, default=5000,    help="Web UI port (default 5000)")
    parser.add_argument("--host",   type=str, default="0.0.0.0", help="Bind address")
    parser.add_argument("--no-bot", action="store_true",        help="Start UI only, use dashboard to start bot")
    args = parser.parse_args()

    # Bot status strings
    pk           = cfg("wallet", "private_key")
    has_pk       = bool(pk) and pk != "YOUR_PRIVATE_KEY_HERE"
    contract     = cfg("wallet", "liquidator_contract")
    has_contract = bool(contract) and contract != "DEPLOY_CONTRACT_ADDRESS_HERE"

    # Start background threads
    threading.Thread(target=_push_loop, daemon=True, name="push").start()
    threading.Thread(target=_log_tail,  daemon=True, name="log-tail").start()

    # Auto-start bot engine if contract is configured
    contract     = cfg("wallet", "liquidator_contract")
    has_contract = bool(contract) and contract != "DEPLOY_CONTRACT_ADDRESS_HERE"

    if not args.no_bot and has_pk and has_contract:
        threading.Thread(target=start_bot_engine, daemon=True).start()
        bot_status = "auto-starting — scanning 24/7"
    else:
        if args.no_bot:
            reason = "--no-bot flag"
        elif not has_pk:
            reason = "private_key not set"
        else:
            reason = "liquidator_contract not set"
        bot_status = f"manual start required ({reason})"
        logger.info(f"Bot not auto-started: {reason}")

    mode = cfg("mode") or "simulate"

    print(f"\n{'═'*56}")
    print(f"  LiqBot  →  http://localhost:{args.port}")
    print(f"  Mode:       {mode.upper()}")
    print(f"  Bot:        {bot_status}")
    print(f"{'═'*56}\n")

    socketio.run(app, host=args.host, port=args.port, debug=False, log_output=False, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
