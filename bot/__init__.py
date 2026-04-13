"""
Arbitrum Flash Loan Liquidation Bot — Module Index

Core pipeline:
  utils            config, web3, ABIs, logging, Telegram
  monitor          multi-protocol scanner (Aave V3, Radiant...)
  liquidator       flash loan execution via deployed contract
  profitability    P&L estimator (bonus, gas, slippage, flash fee)

Edge modules:
  zombie_queue     pre-queues HF 0.95–1.05 positions
  velocity         detects fast-falling health factors
  risk_scorer      7-factor composite opportunity score
  oracle_watcher   Chainlink price-drop triggered scans
  mempool_watcher  pending oracle tx detection
  ws_prices        real-time price streaming (WebSocket)
  realtime_hf      real-time health factor tracking
  emode_detector   Aave V3 E-Mode position awareness

Infrastructure:
  database         SQLite: borrowers, positions, liquidation history
  swap_router      best Uniswap V3 path (single + multi-hop)
  gas_manager      dynamic gas pricing based on base fee + profit
  auto_tuner       adaptive parameter adjustment from live data
  competitor_watcher on-chain bot intelligence
"""
from .utils              import load_config, setup_logging, get_web3, get_account
from .monitor            import MultiProtocolMonitor
from .liquidator         import LiquidationExecutor
from .profitability      import estimate_profit_usd, rank_positions
from .oracle_watcher     import OracleWatcher
from .zombie_queue       import ZombieQueue
from .database           import get_stats, init_db
from .velocity           import VelocityTracker
from .swap_router        import get_best_swap
from .gas_manager        import get_gas_params, is_gas_spike, log_gas_summary
from .competitor_watcher import fetch_recent_liquidations, analyze_competitors
from .mempool_watcher    import start_mempool_watcher_thread
from .emode_detector     import EModeDetector, flag_emode_risk_positions
from .risk_scorer        import score_position, rank_by_score
from .auto_tuner         import get_tuner, AutoTuner
from .ws_prices          import get_price_streamer, RealTimePriceStreamer
from .realtime_hf        import RealTimeHFTracker
