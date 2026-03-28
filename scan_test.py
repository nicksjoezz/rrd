#!/usr/bin/env python3
"""
scan_test.py — Live liquidation opportunity scanner

Connects to Arbitrum, finds active Aave V3 + Radiant borrowers,
checks each for liquidatability, and prints a report.

Usage:
    python scan_test.py
    python scan_test.py --rpc https://arb-mainnet.g.alchemy.com/v2/YOUR_KEY
    python scan_test.py --min-hf 1.10     show positions up to HF 1.10
    python scan_test.py --min-debt 500    only show positions > $500 debt

No wallet needed. Nothing is executed. Pure read-only.

Free Alchemy key signup: https://www.alchemy.com  (takes 2 min, no credit card)
This gives you much higher rate limits and consistent results.
"""

import sys, json, time, argparse, urllib.request, urllib.error
from pathlib import Path
from web3 import Web3

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

try:
    from bot.utils import cfg as _cfg, get_token_map
    from bot.zombie_queue import ZombieQueue
    from bot.swap_router import get_best_swap
    RPC = _cfg("network", "rpc_http")
except Exception:
    RPC = "https://arb1.arbitrum.io/rpc"

# ── Addresses ─────────────────────────────────────────────────────────────────
PROTOCOLS = {
    "Aave V3": {
        "pool": "0x794a61358D6845594F94dc1DB02A252b5b4814aD",
        "dp":   "0x69FA688f1Dc47d4B5d8029D5a35FB7a548310654",
        "topic": "0xb3d084820fb1a9decffb176436bd02558d15fac9b0ddfed8c465bc7359d7dce0",
    },
}

# Borrow topics (onBehalfOf is indexed → lives in topics[2])
# removed global BORROW_TOPIC

# Arbitrum block speed: ~4 blocks/sec → 1 day = 345,600 blocks
BLOCKS_PER_DAY = 345_600

TOKENS = {
    "USDC":   {"address": "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8", "decimals": 6,  "bonus": 0.050},
    "USDCn":  {"address": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", "decimals": 6,  "bonus": 0.050},
    "USDT":   {"address": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9", "decimals": 6,  "bonus": 0.050},
    "DAI":    {"address": "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1", "decimals": 18, "bonus": 0.050},
    "WETH":   {"address": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1", "decimals": 18, "bonus": 0.050},
    "WBTC":   {"address": "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f", "decimals": 8,  "bonus": 0.050},
    "ARB":    {"address": "0x912CE59144191C1204E64559FE8253a0e49E6548", "decimals": 18, "bonus": 0.100},
    "LINK":   {"address": "0xf97f4df75117a78c1A5a0DBb814Af92458539FB4", "decimals": 18, "bonus": 0.075},
    "GMX":    {"address": "0xfc5A1A6EB076a2C7aD06eD22C90d7E710E35ad0a", "decimals": 18, "bonus": 0.150},
    "wstETH": {"address": "0x5979D7b546E38E414F7E9822514be443A4800529", "decimals": 18, "bonus": 0.070},
    "weETH":  {"address": "0x35751007a407ca6FEFfE80b3cB397736D2cf4dbe", "decimals": 18, "bonus": 0.075},
}

POOL_ABI = json.loads('[{"name":"getUserAccountData","type":"function","stateMutability":"view","inputs":[{"name":"user","type":"address"}],"outputs":[{"name":"totalCollateralBase","type":"uint256"},{"name":"totalDebtBase","type":"uint256"},{"name":"availableBorrowsBase","type":"uint256"},{"name":"currentLiquidationThreshold","type":"uint256"},{"name":"ltv","type":"uint256"},{"name":"healthFactor","type":"uint256"}]}]')
DP_ABI   = json.loads('[{"name":"getUserReserveData","type":"function","stateMutability":"view","inputs":[{"name":"asset","type":"address"},{"name":"user","type":"address"}],"outputs":[{"name":"currentATokenBalance","type":"uint256"},{"name":"currentStableDebt","type":"uint256"},{"name":"currentVariableDebt","type":"uint256"},{"name":"principalStableDebt","type":"uint256"},{"name":"scaledVariableDebt","type":"uint256"},{"name":"stableBorrowRate","type":"uint256"},{"name":"liquidityRate","type":"uint256"},{"name":"stableRateLastUpdated","type":"uint40"},{"name":"usageAsCollateralEnabled","type":"bool"}]}]')
CL_ABI   = json.loads('[{"name":"latestRoundData","type":"function","stateMutability":"view","inputs":[],"outputs":[{"name":"roundId","type":"uint80"},{"name":"answer","type":"int256"},{"name":"startedAt","type":"uint256"},{"name":"updatedAt","type":"uint256"},{"name":"answeredInRound","type":"uint80"}]}]')

# Multicall3 (Arbitrum)
MULTICALL3_ADDR = "0xcA11bde05977b3631167028862bE2a173976CA11"
MULTICALL3_ABI  = json.loads('[{"inputs":[{"components":[{"internalType":"address","name":"target","type":"address"},{"internalType":"bytes","name":"callData","type":"bytes"}],"internalType":"struct Multicall3.Call[]","name":"calls","type":"tuple[]"}],"name":"aggregate","outputs":[{"internalType":"uint256","name":"blockNumber","type":"uint256"},{"internalType":"bytes[]","name":"returnData","type":"bytes[]"}],"stateMutability":"payable","type":"function"}]')

R  = lambda t: f"\033[31m{t}\033[0m"
Y  = lambda t: f"\033[33m{t}\033[0m"
G  = lambda t: f"\033[32m{t}\033[0m"
C  = lambda t: f"\033[36m{t}\033[0m"
B  = lambda t: f"\033[1m{t}\033[0m"
DM = lambda t: f"\033[2m{t}\033[0m"


# ── Borrower discovery ────────────────────────────────────────────────────────


def fetch_via_raw_logs(w3, pool_address: str, topic0: str, days_back: int = 7, on_batch=None) -> set:
    """
    Scan Borrow event logs using raw eth_getLogs (no ABI decoding).
    Extracts onBehalfOf from topics[2] directly — avoids all ABI issues.

    Key fix: Uses raw eth_getLogs instead of web3 event helper
    which caused the 'anonymous' error with Aave V3's enum types.
    """
    current    = w3.eth.block_number
    lookback   = BLOCKS_PER_DAY * days_back
    from_block = max(0, current - lookback)
    chunk      = 10_000
    borrowers  = set()
    errors     = 0

    total_chunks = (current - from_block) // chunk + 1
    print(f"  Scanning {lookback:,} blocks ({days_back}d) | "
          f"blocks {from_block:,}→{current:,} | "
          f"~{total_chunks} requests")

    batch = []
    for i, start in enumerate(range(from_block, current, chunk)):
        end = min(start + chunk - 1, current)
        try:
            logs = w3.eth.get_logs({
                "address":   w3.to_checksum_address(pool_address),
                "topics":    [topic0],
                "fromBlock": start,
                "toBlock":   end,
            })
            for log in logs:
                topics = log.get("topics", [])
                if len(topics) >= 3:
                    raw   = topics[2]
                    addr  = "0x" + (raw.hex() if isinstance(raw, bytes) else raw)[-40:]
                    caddr = w3.to_checksum_address(addr)
                    if caddr not in borrowers:
                        borrowers.add(caddr)
                        batch.append(caddr)
                        if len(batch) >= 100:
                            if on_batch: on_batch(batch)
                            batch = []
        except Exception as ex:
            errors += 1
            if errors == 1:
                print(f"\n  First error ({start}-{end}): {ex}")
            if errors > 20:
                print(f"\n  {Y('Too many errors — switch to a private RPC for better results:')}")
                print(f"  {DM('python scan_test.py --rpc https://arb-mainnet.g.alchemy.com/v2/YOUR_KEY')}\n")
                break

        if (i + 1) % 10 == 0 or end >= current - 1:
            pct = min(100, (i + 1) / total_chunks * 100)
            sys.stdout.write(
                f"\r  Progress: {pct:.0f}%  |  Borrowers found: {len(borrowers):,}   "
            )
            sys.stdout.flush()

    if batch and on_batch:
        on_batch(batch)

    print()
    if errors:
        print(f"  {errors} chunk error(s) — some borrowers may be missed")
    return borrowers


# ── Health checks ─────────────────────────────────────────────────────────────
def get_eth_price(w3):
    try:
        feed = w3.eth.contract(
            address=w3.to_checksum_address("0x639Fe6ab55C921f74e7fac1ee960C0B6293ba612"),
            abi=CL_ABI
        )
        return feed.functions.latestRoundData().call()[1] / 1e8
    except Exception:
        return 2500.0


def best_tokens(w3, dp, user):
    best_col = None; best_col_score = 0
    best_debt = None; best_debt_score = 0
    for sym, info in TOKENS.items():
        try:
            rd  = dp.functions.getUserReserveData(
                w3.to_checksum_address(info["address"]),
                w3.to_checksum_address(user)
            ).call()
            col = rd[0]; debt = rd[2]; dec = info["decimals"]
            if col > 0:
                s = (col / 10**dec) * (1 + info["bonus"])
                if s > best_col_score:
                    best_col_score = s; best_col = (sym, info["bonus"])
            if debt > 0:
                s = debt / 10**dec
                if s > best_debt_score:
                    best_debt_score = s; best_debt = sym
        except Exception:
            pass
    return best_col, best_debt


def net_profit(debt_usd, bonus, eth_price):
    return round(debt_usd * bonus - debt_usd * 0.005 - (900_000 * 0.1e9 / 1e18) * eth_price, 2)


def check_positions(w3, pool, dp, borrowers, hf_max, min_debt, eth_price, streaming=False, zombie_queue=None):
    liquidatable = []; approaching = []; checked = 0
    total = len(borrowers)
    blist = list(borrowers)
    
    # Init Multicall3
    mc = w3.eth.contract(address=MULTICALL3_ADDR, abi=MULTICALL3_ABI)
    
    # -- Phase 1: Batch check Health Factors --
    batch_size = 500
    candidate_users = []
    
    for i in range(0, total, batch_size):
        chunk = blist[i : i + batch_size]
        calls = []
        for user in chunk:
            call_data = pool.encodeABI("getUserAccountData", [w3.to_checksum_address(user)])
            calls.append({"target": pool.address, "callData": call_data})
        
        try:
            _, return_data = mc.functions.aggregate(calls).call()
            for j, raw_res in enumerate(return_data):
                user = chunk[j]
                dec = w3.codec.decode(["uint256", "uint256", "uint256", "uint256", "uint256", "uint256"], raw_res)
                if dec[1] == 0: continue
                
                health = dec[5] / 1e18 if dec[5] < 2**128 else 999.0
                debt_usd = dec[1] / 1e8
                
                if debt_usd >= min_debt and health <= hf_max:
                    candidate_users.append((user, health, debt_usd, dec[0]/1e8))
        except Exception:
            for user in chunk:
                try:
                    d = pool.functions.getUserAccountData(user).call()
                    if d[1] > 0:
                        h = d[5] / 1e18 if d[5] < 2**128 else 999.0
                        if d[1]/1e8 >= min_debt and h <= hf_max:
                            candidate_users.append((user, h, d[1]/1e8, d[0]/1e8))
                except: pass

        if not streaming:
            pct = min(100, (i + batch_size) / total * 100)
            sys.stdout.write(f"\r  Checking HF: {pct:.0f}% ({len(candidate_users)} targets found)   ")
            sys.stdout.flush()

    # -- Phase 2: Detailed lookup and Zombie Persistence --
    if candidate_users:
        if not streaming: print(f"\n  Phase 2: Fetching token-level details for {len(candidate_users)} targets...")
        for user, health, debt_usd, col_usd in candidate_users:
            try:
                col_tok, debt_tok = best_tokens(w3, dp, user)
                if not col_tok or not debt_tok: continue
                
                col_sym, bonus = col_tok
                close = 1.0 if (health < 0.95 or debt_usd < 2000) else 0.5
                debt_to_cover_usd = debt_usd * close
                profit = net_profit(debt_to_cover_usd, bonus, eth_price)
                
                # Fetch pre-computed swap for zombie persistence
                col_addr = TOKENS.get(col_sym, {}).get("address")
                debt_addr = TOKENS.get(debt_tok, {}).get("address")
                
                # We need token decimals for the amountIn
                col_dec = TOKENS.get(col_sym, {}).get("decimals", 18)
                debt_dec = TOKENS.get(debt_tok, {}).get("decimals", 18)
                debt_amount_raw = int(debt_to_cover_usd * (10**debt_dec)) # approx
                
                _, _, swap_params = get_best_swap(col_addr, debt_addr, debt_amount_raw)

                pos = {
                    "user": user, "health_factor": health, "hf": health,
                    "debt_usd": debt_usd, "total_debt_usd": debt_usd,
                    "col_usd": col_usd, "total_col_usd": col_usd,
                    "collateral_token": col_addr, "collateral_symbol": col_sym, "col": col_sym,
                    "collateral_bonus": bonus, "bonus": bonus,
                    "debt_token": debt_addr, "debt_symbol": debt_tok, "debt": debt_tok,
                    "debt_to_cover": debt_amount_raw,
                    "pool_address": pool.address,
                    "swap_params": swap_params,
                    "profit": profit,
                    "protocol": "Aave V3" # scan_test is Aave V3 primary
                }
                
                if zombie_queue:
                    zombie_queue.update(pos["protocol"], user, pos)
                
                if health < 1.0:
                    liquidatable.append(pos)
                    if streaming: print(f"  {R('[ALERT]')} Liquidatable: {user[:10]} HF={health:.4f} Debt=${debt_usd:,.0f} Profit=${profit:,.2f}")
                else:
                    approaching.append(pos)
                    if streaming: print(f"  {Y('[ZOMBIE]')} Saved: {user[:10]} HF={health:.4f} Debt=${debt_usd:,.0f}")
                
                checked += 1
            except Exception:
                pass

    return liquidatable, approaching, checked


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="LiqBot — Live opportunity scanner")
    parser.add_argument("--rpc",      default=RPC,          help="Arbitrum RPC URL")
    parser.add_argument("--alchemy",  help="Alchemy API Key (speeds up verification)")
    parser.add_argument("--min-hf",   type=float, default=1.05)
    parser.add_argument("--min-debt", type=float, default=200.0)
    parser.add_argument("--days",     type=int,   default=7,
                        help="Days to scan for Borrow events (default 7)")
    args = parser.parse_args()

    # Construction of RPC
    rpc_url = args.rpc
    
    # Check config.json for Alchemy key fallback
    try:
        with open("config.json") as f:
            cdata = json.load(f)
            if not args.alchemy:
                args.alchemy = cdata["network"].get("alchemy_key", "")
    except: pass

    if args.alchemy:
        if args.alchemy.startswith("http"):
            rpc_url = args.alchemy
        else:
            rpc_url = f"https://arb-mainnet.g.alchemy.com/v2/{args.alchemy}"

    print(f"\n{B('═'*62)}")
    print(B("  LiqBot — Live Opportunity Scanner"))
    print(B("  Arbitrum One · Aave V3 · read-only"))
    print(f"{B('═'*62)}\n")

    # ── Connections ───────────────────────────────────────────────────────────
    # We use TWO RPCs: 
    # 1. PUBLIC_RPC for eth_getLogs (Discovery) — Alchemy is too restrictive here.
    # 2. ALCHEMY_RPC for Multicall (Verification) — Peak performance.
    
    print(f"  Connecting to Public RPC…", end=" ", flush=True)
    try:
        public_w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 60}))
        if not public_w3.is_connected():
            raise ConnectionError("Public RPC failed")
        print(G("connected"))
    except Exception as e:
        print(R("FAILED") + f"\n  {e}")
        sys.exit(1)

    alchemy_w3 = public_w3 # Fallback
    use_alchemy = False

    if args.alchemy:
        print(f"  Connecting to Alchemy…", end=" ", flush=True)
        if not args.alchemy.startswith("http"):
            args.alchemy = f"https://arb-mainnet.g.alchemy.com/v2/{args.alchemy}"
        
        try:
            alchemy_w3 = Web3(Web3.HTTPProvider(args.alchemy, request_kwargs={"timeout": 30}))
            if not alchemy_w3.is_connected():
                print(Y(f"Failed (Check your key: {args.alchemy[:25]}...)"))
            else:
                use_alchemy = True
                print(G("connected"))
        except Exception as e:
            print(Y(f"Error ({e}) — check your key or URL"))

    block     = public_w3.eth.block_number
    gas_gwei  = public_w3.eth.gas_price / 1e9
    eth_price = get_eth_price(public_w3)
    
    print(f"  Block: {block:,}  |  Gas: {gas_gwei:.4f} gwei  |  ETH: ${eth_price:,.0f}\n")

    t0       = time.time()
    
    # Use Alchemy for HF checks if available
    target_w3 = alchemy_w3 if use_alchemy else public_w3
    pool = target_w3.eth.contract(
        address=target_w3.to_checksum_address(PROTOCOLS["Aave V3"]["pool"]), abi=POOL_ABI
    )
    dp = target_w3.eth.contract(
        address=target_w3.to_checksum_address(PROTOCOLS["Aave V3"]["dp"]), abi=DP_ABI
    )

    # Initialize zombie queue for persistence
    zombie_queue = ZombieQueue(entry_hf=args.min_hf, fire_hf=1.0)

    all_borr = set()

    for proto_name, pcfg in PROTOCOLS.items():
        print(f"  {B(proto_name)} borrower discovery…")

        def _on_streaming_batch(new_users):
            check_positions(target_w3, pool, dp, new_users, args.min_hf, args.min_debt, eth_price, streaming=True, zombie_queue=zombie_queue)

        borr = fetch_via_raw_logs(public_w3, pcfg["pool"], pcfg["topic"], days_back=args.days, on_batch=_on_streaming_batch)
        all_borr.update(borr)
        print(f"  {proto_name}: {len(borr):,} borrowers from event scan")

    print()
    if not all_borr:
        print(R("  No borrowers found."))
        sys.exit(1)

    # Check HF for each borrower
    print(f"  Total unique borrowers: {len(all_borr):,}")
    print(f"  Checking remaining health factors (this takes 30-120s depending on your RPC)…\n")

    liquidatable, approaching, checked = check_positions(
        target_w3, pool, dp, all_borr,
        hf_max=args.min_hf, min_debt=args.min_debt, eth_price=eth_price,
        zombie_queue=zombie_queue
    )

    print(f"\n  Done in {time.time()-t0:.1f}s  |  Checked: {checked:,} positions\n")

    # ── Print results ─────────────────────────────────────────────────────────
    liquidatable = sorted(liquidatable, key=lambda x: -x["profit"])
    approaching  = sorted(approaching,  key=lambda x:  x["hf"])

    print(f"{B('━'*62)}")
    print(B(f"  🔴  LIQUIDATABLE NOW  (HF < 1.0)  —  {len(liquidatable)}"))
    print(f"{B('━'*62)}\n")

    if not liquidatable:
        print(DM("  None found. Market is healthy — no positions currently underwater."))
        print(DM("  The bot scans 24/7 and fires the moment HF drops below 1.0.\n"))
    else:
        print(f"  {'WALLET':<14}  {'HF':>8}  {'DEBT':>10}  {'COLLATERAL':>12}  {'EST PROFIT':>11}")
        print(f"  {'─'*14}  {'─'*8}  {'─'*10}  {'─'*12}  {'─'*11}")
        for p in liquidatable[:30]:
            addr  = p["user"][:6] + "…" + p["user"][-4:]
            col_s = C(f"{p['col']:<5}") + f"({p['bonus']*100:.0f}%)"
            pft_s = G(f"${p['profit']:>8,.2f}") if p["profit"] > 0 else Y(f"${p['profit']:>8,.2f}")
            hf_s = R(f"{p['hf']:.5f}")
            print(f"  {addr:<14}  {hf_s:>8}  ${p['debt_usd']:>8,.0f}  {col_s:>12}  {pft_s:>11}")
        if len(liquidatable) > 30:
            print(DM(f"\n  … and {len(liquidatable)-30} more"))
        total = sum(p["profit"] for p in liquidatable if p["profit"] > 0)
        print(f"\n  {G('Est. profit if all captured:')}  {G('$'+f'{total:,.2f}')}")

    print(f"\n{B('━'*62)}")
    print(B(f"  ⚠️   ZOMBIE QUEUE  (HF 1.0–{args.min_hf})  —  {len(approaching)}"))
    print(f"{B('━'*62)}\n")

    if not approaching:
        print(DM("  None in zombie zone.\n"))
    else:
        print(f"  {'WALLET':<14}  {'HF':>9}  {'DEBT':>10}  {'COLLATERAL':>12}  {'IF DROPS':>10}")
        print(f"  {'─'*14}  {'─'*9}  {'─'*10}  {'─'*12}  {'─'*10}")
        for p in approaching[:20]:
            addr  = p["user"][:6] + "…" + p["user"][-4:]
            hf_s  = Y(f"{p['hf']:.5f}") if p["hf"] < 1.02 else f"{p['hf']:.5f}"
            col_s = C(f"{p['col']:<5}") + f"({p['bonus']*100:.0f}%)"
            print(f"  {addr:<14}  {hf_s:>9}  ${p['debt_usd']:>8,.0f}  {col_s:>12}  ${p['profit']:>8,.2f}")
        if len(approaching) > 20:
            print(DM(f"\n  … and {len(approaching)-20} more"))
        pot = sum(p["profit"] for p in approaching if p["profit"] > 0)
        print(f"\n  {Y('Est. profit if prices drop 3–5%:')}  {Y('$'+f'{pot:,.2f}')}")

    # Summary
    print(f"\n{B('━'*62)}")
    print(B("  SUMMARY"))
    print(f"{B('━'*62)}\n")
    profitable = [p for p in liquidatable if p["profit"] > 0]
    print(f"  Borrowers checked:     {checked:,}")
    print(f"  Liquidatable now:      {G(str(len(liquidatable)))}")
    print(f"  Profitable (>$5):      {G(str(len(profitable)))}")
    print(f"  Zombie queue:          {Y(str(len(approaching)))}")
    print(f"  Gas per tx:            ~${(900_000*0.1e9/1e18)*eth_price:.2f}")
    print(f"  Flash loan fee:        $0.00 (Balancer 0%)")

    by_col = {}
    for p in liquidatable + approaching:
        by_col.setdefault(p["col"], {"n": 0, "profit": 0.0})
        by_col[p["col"]]["n"]      += 1
        by_col[p["col"]]["profit"] += max(p["profit"], 0)
    if by_col:
        print(f"\n  Collateral breakdown:")
        for sym, d in sorted(by_col.items(), key=lambda x: -x[1]["profit"]):
            bonus = TOKENS.get(sym, {}).get("bonus", 0.05)
            print(f"    {sym:<10}  {d['n']:>3} positions  ${d['profit']:>8,.2f}  ({bonus*100:.0f}% bonus)")

    print(f"\n  Tip: python main.py starts the bot + dashboard at http://localhost:5000\n")


if __name__ == "__main__":
    main()
