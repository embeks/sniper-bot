import asyncio
import time

from utils import (
    send_telegram_alert,
    get_token_price,
    get_token_balance
)
from sniper_logic import start_sniper

# 🔧 Profit Targets and Timeout
TARGET_MULTIPLIERS = [2, 5, 10]  # Auto-sell thresholds
TIMEOUT_SECONDS = 300  # Max hold time (in seconds)

# ✅ Initial Bot Alert
async def startup():
    await send_telegram_alert("✅ Sniper bot is now live and scanning the mempool...")

# 🚨 Profit-Take Logic
async def auto_sell_if_profit(token_mint: str, entry_price: float, wallet: str = None):
    try:
        start_time = time.time()
        last_multiplier_hit = None

        while time.time() - start_time < TIMEOUT_SECONDS:
            current_price = await get_token_price(token_mint)

            if current_price:
                for mult in TARGET_MULTIPLIERS:
                    if current_price >= entry_price * mult:
                        if last_multiplier_hit != mult:
                            amount_token = await get_token_balance(token_mint)
                            await send_telegram_alert(f"🚀 {mult}x hit on {token_mint}! Selling now...")
                            await sell_token(token_mint, amount_token)
                            return
                        last_multiplier_hit = mult

            await asyncio.sleep(5)

        # ⏱ Timeout reached
        await send_telegram_alert(f"⏱ Timeout hit on {token_mint}. Selling now...")
        amount_token = await get_token_balance(token_mint)
        await sell_token(token_mint, amount_token)

    except Exception as e:
        await send_telegram_alert(f"[‼️] Auto-sell logic failed for {token_mint}:\n{e}")
