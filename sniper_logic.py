# =========================
# sniper_logic.py — Final Elite Version
# =========================

import os
import asyncio
import json
import time
from datetime import datetime
from utils import (
    send_telegram_alert,
    snipe_token,
    is_valid_mint,
    wallet_pubkey,
    BUY_AMOUNT_SOL,
    get_token_price,
    log_trade,
)

from solders.signature import Signature
from solana.rpc.api import Client
from dotenv import load_dotenv

load_dotenv()

FORCE_TEST_MINT = os.getenv("FORCE_TEST_MINT")
RPC_URL = os.getenv("RPC_URL")
rpc_client = Client(RPC_URL)

# ✅ Real Sell Function (Jupiter Placeholder)
async def sell_token(mint, amount_out):
    try:
        await send_telegram_alert(f"📤 Selling {mint}... [placeholder]")
        log_trade(mint, "SELL", 0, amount_out)
        return True
    except Exception as e:
        await send_telegram_alert(f"❌ Sell failed for {mint}: {e}")
        return False

# 📈 Price Monitor for 2x/5x/10x Profit Taking
async def monitor_price_and_sell(mint, entry_price):
    try:
        checkpoints = [2, 5, 10]
        hit = set()
        start = time.time()
        timeout = 300  # 5 min fallback

        while True:
            await asyncio.sleep(5)
            current_price = await get_token_price(mint)
            if not current_price:
                continue

            for x in checkpoints:
                if x not in hit and current_price >= entry_price * x:
                    await send_telegram_alert(f"💰 {x}x profit hit for {mint} — selling!")
                    await sell_token(mint, current_price * BUY_AMOUNT_SOL)
                    hit.add(x)

            if time.time() - start > timeout:
                await send_telegram_alert(f"⏱ Timeout hit for {mint}, selling...")
                await sell_token(mint, current_price * BUY_AMOUNT_SOL)
                return
    except Exception as e:
        await send_telegram_alert(f"⚠️ Monitor error for {mint}: {e}")

# 🧠 Live Forced Buy for Testing
async def force_test_buy():
    try:
        if not FORCE_TEST_MINT or len(FORCE_TEST_MINT) != 44:
            await send_telegram_alert("❌ Invalid *FORCETESTMINT* format.")
            return

        await send_telegram_alert(f"TEST MODE 🧪 FORCETESTMINT detected: {FORCE_TEST_MINT}")
        await asyncio.sleep(1)

        await send_telegram_alert("TEST MODE ✅ Mint is valid. Attempting forced buy...")
        success = await snipe_token(FORCE_TEST_MINT)

        if success:
            price = await get_token_price(FORCE_TEST_MINT)
            await monitor_price_and_sell(FORCE_TEST_MINT, price)
        else:
            await send_telegram_alert("❌ Forced buy failed.")

    except Exception as e:
        await send_telegram_alert(f"‼️ Forced buy error: {e}")

# 🚀 WebSocket Listeners (Raydium + Jupiter)
async def raydium_listener():
    import websockets
    url = "wss://api.helius.xyz/v0/addresses/raydium/logs?api-key=" + os.getenv("HELIUS_API")
    async with websockets.connect(url) as ws:
        await send_telegram_alert("📡 RAYDIUM listener active.")
        while True:
            data = json.loads(await ws.recv())
            if is_valid_mint(data.get("logs", [])):
                mint = data.get("mint", "")
                if await snipe_token(mint):
                    price = await get_token_price(mint)
                    await monitor_price_and_sell(mint, price)

async def jupiter_listener():
    import websockets
    url = "wss://api.helius.xyz/v0/addresses/jupiter/logs?api-key=" + os.getenv("HELIUS_API")
    async with websockets.connect(url) as ws:
        await send_telegram_alert("📡 JUPITER listener active.")
        while True:
            data = json.loads(await ws.recv())
            if is_valid_mint(data.get("logs", [])):
                mint = data.get("mint", "")
                if await snipe_token(mint):
                    price = await get_token_price(mint)
                    await monitor_price_and_sell(mint, price)

# 🧠 Main Entrypoint
async def start_sniper():
    await send_telegram_alert("✅ Sniper bot is now live and scanning the mempool...")

    await asyncio.gather(
        force_test_buy(),
        raydium_listener(),
        jupiter_listener(),
        asyncio.to_thread(start_command_bot)
    )

from utils import start_command_bot
