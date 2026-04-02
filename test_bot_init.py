import sys
import logging
from pathlib import Path

# Setup dummy logging to avoid noise
logging.basicConfig(level=logging.CRITICAL)

sys.path.insert(0, str(Path.cwd()))

def test_imports():
    modules = [
        'bot.utils',
        'bot.persistence',
        'bot.database',
        'bot.profitability',
        'bot.gas_manager',
        'bot.swap_router',
        'bot.auto_tuner',
        'bot.liquidator',
        'bot.monitor',
        'bot.realtime_hf',
        'bot.ws_prices'
    ]
    for m in modules:
        try:
            __import__(m)
            print(f"✅ {m}")
        except Exception as e:
            print(f"❌ {m}: {e}")
            import traceback
            traceback.print_exc()

def test_executor_init():
    try:
        from bot.liquidator import LiquidationExecutor
        ex = LiquidationExecutor()
        print("✅ LiquidationExecutor init")
    except Exception as e:
        print(f"❌ LiquidationExecutor init: {e}")
        import traceback
        traceback.print_exc()

def test_monitor_init():
    try:
        from bot.monitor import MultiProtocolMonitor
        mpm = MultiProtocolMonitor()
        print("✅ MultiProtocolMonitor init")
    except Exception as e:
        print(f"❌ MultiProtocolMonitor init: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_imports()
    test_executor_init()
    test_monitor_init()
