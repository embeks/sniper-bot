# =========================
# trade_logic.py (Final Optimized Version)
# =========================
import asyncio
import time

from utils import (
    send_telegram_alert,
    get_token_price,
    get_token_balance,
    get_token_data,
    detect_rug_conditions,
    log_trade_to_csv
)
from jupiter_trade import sell_token

# 🔧 Config
TARGET_MULTIPLIERS = [2, 5, 10]     # Profit checkpoints
TIMEOUT_SECONDS = 300              # Max hold time (in seconds)
RUG_THRESHOLD = 0.75               # Trigger sell if liquidity drops by 25%
VOLUME_SURGE_THRESHOLD = 5         # 500% candle

# ✅ Optional: Startup notifier
async def startup():
    await send_telegram_alert("✅ Sniper bot is now live and scanning the mempool...")

# 🚨 Auto-sell logic with rug protection, multiplier checks, timeout, and pump exits
async def auto_sell_if_profit(token_mint: str, entry_price: float):
    try:
        await send_telegram_alert(f"🧠 Auto-sell activated for {token_mint} @ {entry_price:.6f} SOL")

        start_time = time.time()
        last_multiplier_hit = None

        # 🔒 Initial liquidity + volume snapshot
        token_data_start = await get_token_data(token_mint)
        initial_liquidity = token_data_start.get("liquidity", 0)
        initial_volume = token_data_start.get("volume1h", 0)

        while time.time() - start_time < TIMEOUT_SECONDS:
            current_price = await get_token_price(token_mint)
            token_data = await get_token_data(token_mint)
            current_liquidity = token_data.get("liquidity", 0)
            current_volume = token_data.get("volume1h", 0)

            # 🛑 Rug protection: auto-sell if >25% liquidity drop
            if current_liquidity and initial_liquidity and current_liquidity < initial_liquidity * RUG_THRESHOLD:
                await send_telegram_alert(f"🛑 Rug Alert: Liquidity dropped >25%\nInitial: {initial_liquidity} | Now: {current_liquidity}")
                amount_token = await get_token_balance(token_mint)
                if amount_token > 0:
                    await sell_token(token_mint, amount_token)
                    log_trade_to_csv(token_mint, "sell_rug", amount_token, current_price)
                return

            # 📈 Pre-pump trigger: if volume >5x in <1m
            if initial_volume and current_volume > initial_volume * VOLUME_SURGE_THRESHOLD:
                await send_telegram_alert(f"📈 Volume Pump Trigger: 5x surge detected. Selling early for {token_mint}")
                amount_token = await get_token_balance(token_mint)
                if amount_token > 0:
                    await sell_token(token_mint, amount_token)
                    log_trade_to_csv(token_mint, "sell_volume_surge", amount_token, current_price)
                return

            # ✅ Profit-based sell
            if current_price is not None:
                for mult in TARGET_MULTIPLIERS:
                    target_price = entry_price * mult
                    if current_price >= target_price and last_multiplier_hit != mult:
                        amount_token = await get_token_balance(token_mint)
                        if amount_token == 0:
                            await send_telegram_alert(f"⚠️ No tokens found for {token_mint}, skipping sell")
                            return

                        last_multiplier_hit = mult
                        await send_telegram_alert(f"🚀 {mult}x target hit @ {current_price:.6f} SOL! Selling {amount_token} of {token_mint}")
                        await sell_token(token_mint, amount_token)
                        log_trade_to_csv(token_mint, f"sell_{mult}x", amount_token, current_price)
                        return
            else:
                print(f"[!] Could not fetch price for {token_mint}")

            await asyncio.sleep(5)

        # ⏱ Timeout reached
        amount_token = await get_token_balance(token_mint)
        if amount_token > 0:
            await send_telegram_alert(f"⏱ Timeout reached. Selling {amount_token} of {token_mint}")
            await sell_token(token_mint, amount_token)
            log_trade_to_csv(token_mint, "sell_timeout", amount_token, current_price or 0)
        else:
            await send_telegram_alert(f"⏱ Timeout hit — but no tokens found for {token_mint}")

    except Exception as e:
        await send_telegram_alert(f"[‼️] Auto-sell error for {token_mint}:\n{str(e)}")
        print(f"[‼️] Auto-sell error for {token_mint}:", e)
