import asyncio
import time

from solana_sniper import buy_token, sell_token
from mempool_listener import mempool_listener
from utils import send_telegram_alert, get_token_balance, get_token_price

# 🔧 Config
TARGET_MULTIPLIERS = [2, 5, 10]  # x2, x5, x10
TIMEOUT_SECONDS = 300  # 5 minutes

# ✅ Startup alert
async def startup():
    await send_telegram_alert("✅ Sniper bot is now live and monitoring mempool")

# 🚨 Auto-sell logic
async def auto_sell_if_profit(token_mint, entry_price, wallet=None):
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

        # Timeout fallback
        await send_telegram_alert(f"⏱ Timeout hit on {token_mint}. Selling now...")
        amount_token = await get_token_balance(token_mint)
        await sell_token(token_mint, amount_token)

    except Exception as e:
        await send_telegram_alert(f"[‼️] Auto-sell failed: {e}")

from sniper import start_sniper

if __name__ == "__main__":
    asyncio.run(start_sniper())
