# =========================
# main.py
# =========================
import asyncio
from sniper_logic import mempool_listener_jupiter, mempool_listener_raydium
from trade_logic import startup

async def start_sniper():
    await startup()  # ✅ Telegram alert when bot starts
    await asyncio.gather(
        mempool_listener_jupiter(),
        mempool_listener_raydium()
    )

if __name__ == "__main__":
    asyncio.run(start_sniper())
