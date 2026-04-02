import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))

try:
    from bot.monitor import MULTICALL3_ADDR, MULTICALL3_ABI
    print(f"MULTICALL3_ADDR: {MULTICALL3_ADDR}")
    from bot.liquidator import get_mode
    print(f"Mode: {get_mode()}")
    from bot.swap_router import get_best_swap
    print("Swap Router OK")
    print("ALL IMPORTS SUCCESSFUL")
except Exception as e:
    print(f"IMPORT ERROR: {e}")
    import traceback
    traceback.print_exc()
