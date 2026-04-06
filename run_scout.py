import asyncio
import sys
import os

# Set up PYTHONPATH
sys.path.append(os.getcwd())

from bot.scout import update_watchlist

async def main():
    await update_watchlist()

if __name__ == "__main__":
    asyncio.run(main())
