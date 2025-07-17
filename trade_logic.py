# =========================
# trade_logic.py
# =========================
import asyncio
import time

from utils import (
    send_telegram_alert,
    get_token_price,
    get_token_balance,
    get_token_data
)
from jupiter_trade import sell_token

# 🔧 Sell Configuration
TARGET_MULTIPLIERS = [2, 5, 10]     # Profit checkpoints
TIMEOUT_SECONDS = 300              # Max hold time (5 minutes)
RUG_THRESHOLD = 0.75               # Sell if liquidity drops >25%

# ✅ Notify on startup
async def startup():
    await send_telegram_alert("✅ Sniper bot is now live and scanning the mempool...")

# 🚨 Auto-sell handler
async def auto_sell_if_profit(token_mint: str, entry_price: float):
    try:
        await send_telegram_alert(f"🧠 Auto-sell activated for {token_mint} @ {entry_price:.6f} SOL")

        start_time = time.time()
        last_multiplier_hit = None

        token_data_start = await get_token_data(token_mint)
        initial_liquidity = token_data_start.get("liquidity", 0)

        while time.time() - start_time < TIMEOUT_SECONDS:
            current_price = await get_token_price(token_mint)
            token_data = await get_token_data(token_mint)
            current_liquidity = token_data.get("liquidity", 0)

            # 🛑 Rug detection
            if current_liquidity and initial_liquidity:
                if current_liquidity < initial_liquidity * RUG_THRESHOLD:
                    await send_telegram_alert(
                        f"🛑 Rug Detected for {token_mint}!\n"
                        f"Liquidity dropped >25%\n"
                        f"Initial: {initial_liquidity}\nCurrent: {current_liquidity}"
                    )
                    amount_token = await get_token_balance(token_mint)
                    if amount_token > 0:
                        await sell_token(token_mint, amount_token)
                    return

            # 🚀 Multiplier-based sell
            if current_price:
                for mult in TARGET_MULTIPLIERS:
                    target = entry_price * mult
                    if current_price >= target and last_multiplier_hit != mult:
                        amount_token = await get_token_balance(token_mint)
                        if amount_token == 0:
                            await send_telegram_alert(f"⚠️ No tokens held for {token_mint}, skipping sell")
                            return

                        last_multiplier_hit = mult
                        await send_telegram_alert(
                            f"🚀 {mult}x target hit!\n"
                            f"{token_mint}\nCurrent: {current_price:.6f} SOL\nSelling {amount_token} tokens..."
                        )
                        await sell_token(token_mint, amount_token)
                        return
            else:
                print(f"[!] Failed to fetch price for {token_mint}")

            await asyncio.sleep(5)

        # ⏱ Timeout fallback
        amount_token = await get_token_balance(token_mint)
        if amount_token > 0:
            await send_telegram_alert(f"⏱ Timeout reached. Selling {amount_token} of {token_mint}")
            await sell_token(token_mint, amount_token)
        else:
            await send_telegram_alert(f"⏱ Timeout hit — but no tokens held for {token_mint}")

    except Exception as e:
        await send_telegram_alert(f"[‼️] Auto-sell error for {token_mint}:\n{str(e)}")
        print(f"[‼️] Auto-sell error for {token_mint}:", e)
