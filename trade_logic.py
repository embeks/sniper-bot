import asyncio
import time
from utils import (
    send_telegram_alert,
    get_token_price,
    get_token_balance
)
from jupiter_trade import sell_token

# 🔧 Config
TARGET_MULTIPLIERS = [2, 5, 10]     # Profit checkpoints
TIMEOUT_SECONDS = 300              # Max hold time (in seconds)

# ✅ Optional: Startup notifier
async def startup():
    await send_telegram_alert("✅ Sniper bot is now live and scanning the mempool...")

# 🚨 Auto-sell logic
async def auto_sell_if_profit(token_mint: str, entry_price: float):
    try:
        await send_telegram_alert(f"🧠 Auto-sell activated for {token_mint} @ {entry_price:.6f} SOL")

        start_time = time.time()
        last_multiplier_hit = None

        while time.time() - start_time < TIMEOUT_SECONDS:
            current_price = await get_token_price(token_mint)

            if current_price is None:
                print(f"[!] Could not fetch price for {token_mint}")
                await asyncio.sleep(5)
                continue

            for mult in TARGET_MULTIPLIERS:
                target_price = entry_price * mult
                if current_price >= target_price and last_multiplier_hit != mult:
                    amount_token = await get_token_balance(token_mint)
                    if amount_token == 0:
                        await send_telegram_alert(f"⚠️ No tokens found for {token_mint}, skipping sell")
                        return

                    await send_telegram_alert(f"🚀 {mult}x target hit ({current_price:.6f} SOL)! Selling {amount_token} of {token_mint}")
                    await sell_token(token_mint, amount_token)
                    return  # Exit after selling
                    last_multiplier_hit = mult

            await asyncio.sleep(5)

        # ⏱ Timeout reached
        amount_token = await get_token_balance(token_mint)
        if amount_token > 0:
            await send_telegram_alert(f"⏱ Timeout reached. Selling {amount_token} of {token_mint}")
            await sell_token(token_mint, amount_token)
        else:
            await send_telegram_alert(f"⏱ Timeout hit — but no tokens found for {token_mint}")

    except Exception as e:
        await send_telegram_alert(f"[‼️] Auto-sell error for {token_mint}:\n{str(e)}")
        print(f"[‼️] Auto-sell error for {token_mint}:", e)
