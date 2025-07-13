import asyncio
import time

from utils import (
    send_telegram_alert,
    get_token_price,
    get_token_balance
)
from jupiter_trade import sell_token

# 🔧 Auto-sell config
TARGET_MULTIPLIERS = [2, 5, 10]     # e.g., sell at 2x, 5x, 10x
TIMEOUT_SECONDS = 300              # Sell after 5 minutes max hold

# ✅ Startup alert (optional)
async def startup():
    await send_telegram_alert("✅ Sniper bot is now live and scanning the mempool...")

# 🚨 Auto-sell handler
async def auto_sell_if_profit(token_mint: str, entry_price: float, wallet: str = None):
    try:
        await send_telegram_alert(f"🧠 Auto-sell logic activated for {token_mint} at entry {entry_price:.6f} SOL")

        start_time = time.time()
        last_multiplier_hit = None

        while time.time() - start_time < TIMEOUT_SECONDS:
            current_price = await get_token_price(token_mint)

            if not current_price:
                print(f"[!] Failed to fetch price for {token_mint}")
                await asyncio.sleep(5)
                continue

            for mult in TARGET_MULTIPLIERS:
                if current_price >= entry_price * mult:
                    if last_multiplier_hit != mult:
                        amount_token = await get_token_balance(token_mint)
                        await send_telegram_alert(f"🚀 {mult}x hit on {token_mint}! Selling now...")
                        await sell_token(token_mint, amount_token)
                        return
                    last_multiplier_hit = mult

            await asyncio.sleep(5)

        # ⏱ Timeout reached, force sell
        await send_telegram_alert(f"⏱ Timeout hit on {token_mint}. Selling now...")
        amount_token = await get_token_balance(token_mint)
        await sell_token(token_mint, amount_token)

    except Exception as e:
        await send_telegram_alert(f"[‼️] Auto-sell logic failed for {token_mint}:\n{e}")
