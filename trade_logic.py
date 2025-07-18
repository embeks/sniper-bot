# =========================
# trade_logic.py (Final Version with Volume Spike + Delta Tracker + Filters)
# =========================
import asyncio
import time
from utils import (
    send_telegram_alert,
    get_token_price,
    get_token_balance,
    get_token_data,
    is_volume_spike
    get_holder_delta,
    is_safe_token,
    preapprove_token
)
from jupiter_trade import sell_token

# 🔧 Config
TARGET_MULTIPLIERS = [2, 5, 10]     # Profit checkpoints
TIMEOUT_SECONDS = 300              # Max hold time (in seconds)
RUG_THRESHOLD = 0.75               # Trigger sell if liquidity drops by 25%
VOLUME_SPIKE_MULTIPLIER = 5.0      # 500% spike triggers early exit

# ✅ Optional: Startup notifier
async def startup():
    await send_telegram_alert("✅ Sniper bot is now live and scanning the mempool...")

# 🚨 Auto-sell logic with all protection + early exits
def get_dynamic_buy_size(liquidity, holders):
    if liquidity < 10_000 or holders < 10:
        return 0.2
    elif liquidity < 50_000 or holders < 30:
        return 0.4
    else:
        return 0.6

async def auto_sell_if_profit(token_mint: str, entry_price: float):
    try:
        await send_telegram_alert(f"🧠 Auto-sell activated for {token_mint} @ {entry_price:.6f} SOL")
        start_time = time.time()
        last_multiplier_hit = None
        token_data_start = await get_token_data(token_mint)
        initial_liquidity = token_data_start.get("liquidity", 0)

        await preapprove_token(token_mint)  # ✅ Pre-approval to avoid failure

        while time.time() - start_time < TIMEOUT_SECONDS:
            current_price = await get_token_price(token_mint)
            token_data = await get_token_data(token_mint)
            current_liquidity = token_data.get("liquidity", 0)

            # 🛑 Rug detection
            if current_liquidity and initial_liquidity and current_liquidity < initial_liquidity * RUG_THRESHOLD:
                await send_telegram_alert(f"🛑 Rug Alert: Liquidity dropped >25%\n{token_mint}\nInitial: {initial_liquidity}\nNow: {current_liquidity}")
                amount_token = await get_token_balance(token_mint)
                if amount_token > 0:
                    await sell_token(token_mint, amount_token)
                return

            # 📈 Volume spike exit
            volume_spike = await get_1m_volume_spike(token_mint)
            if volume_spike >= VOLUME_SPIKE_MULTIPLIER:
                amount_token = await get_token_balance(token_mint)
                await send_telegram_alert(f"📊 Volume Surge Detected (+{volume_spike:.1f}x in 1m), exiting early...")
                if amount_token > 0:
                    await sell_token(token_mint, amount_token)
                return

            # ✅ Multiplier targets
            if current_price is not None:
                for mult in TARGET_MULTIPLIERS:
                    target_price = entry_price * mult
                    if current_price >= target_price and last_multiplier_hit != mult:
                        amount_token = await get_token_balance(token_mint)
                        if amount_token == 0:
                            await send_telegram_alert(f"⚠️ No tokens found for {token_mint}, skipping sell")
                            return

                        last_multiplier_hit = mult
                        await send_telegram_alert(f"🚀 {mult}x target hit ({current_price:.6f} SOL)! Selling {amount_token} of {token_mint}")
                        await sell_token(token_mint, amount_token)
                        return
            else:
                print(f"[!] Could not fetch price for {token_mint}")

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
