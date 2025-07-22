# =========================
# main.py — Launch Bot
# =========================

import asyncio
from sniper_logic import start_sniper

if __name__ == "__main__":
    asyncio.run(start_sniper())
