# main.py
import asyncio
from solana_sniper import buy_token, sell_token
from utils import send_telegram_alert, get_token_balance, get_token_price
import time

# 🔧 Config
TOKEN_MINT_ADDRESS = "TOKEN_MINT_ADDRESS_HERE"
AMOUNT_SOL_TO_SPEND = 0.01
TARGET_MULTIPLIERS = [2, 5, 10]  # x2, x5, x10
TIMEOUT_SECONDS = 300  # 5 minutes

# ✅ Called once on start
async def startup():
    await send_telegram_alert("✅ Sniper bot is now live and ready to snipe")

# 🚨 Auto-sell if profit or timeout
async def auto_sell_if_profit(token_mint, entry_price, wallet):
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

# 🌀 Main loop (placeholder for real detection logic)
async def main():
    await startup()

    while True:
        try:
            token_to_snipe = TOKEN_MINT_ADDRESS
            entry_price = await get_token_price(token_to_snipe)
            if not entry_price:
                await send_telegram_alert("❌ Could not fetch entry price")
                await asyncio.sleep(60)
                continue

            await buy_token(token_to_snipe, AMOUNT_SOL_TO_SPEND)
            await auto_sell_if_profit(token_to_snipe, entry_price, None)

            print("Waiting for next snipe...")
            await asyncio.sleep(60)

        except Exception as e:
            print(f"[!] Loop error: {e}")
            await asyncio.sleep(30)

if __name__ == "__main__":
    asyncio.run(main())
