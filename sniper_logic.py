# =============================
# sniper_logic.py — Final Stable Version
# =============================

import os
import asyncio
import json
import websockets
from dotenv import load_dotenv

from utils import (
    send_telegram_alert,
    is_valid_mint,
    buy_token,
    start_command_bot,
    log_skipped_token
)

load_dotenv()

FORCE_TEST_MINT = os.getenv("FORCE_TEST_MINT", "").strip()
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")

JUPITER_URL = f"wss://rpc.helius.xyz/v0/transactions/?api-key={HELIUS_API_KEY}"
RAYDIUM_URL = f"wss://rpc.helius.xyz/v0/transactions/?api-key={HELIUS_API_KEY}"

# Jupiter Listener
async def jupiter_listener():
    await asyncio.sleep(1)
    try:
        async with websockets.connect(JUPITER_URL) as ws:
            await send_telegram_alert("📡 Jupiter listener live.")
            await ws.send(json.dumps({
                "type": "subscribe",
                "programId": "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB",
                "commitment": "confirmed"
            }))
            while True:
                try:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    if data.get("type") != "transaction": continue
                    keys = data.get("accountKeys", [])
                    if not is_valid_mint(keys): continue
                    mint = data["events"]["mint"]
                    if mint:
                        await send_telegram_alert(f"🚀 New Jupiter token: `{mint}`")
                        await buy_token(mint)
                except Exception as e:
                    print(f"[JUPITER ERROR] {e}")
                    await asyncio.sleep(1)
    except Exception as e:
        print(f"[JUPITER CONNECT ERROR] {e}")
        await asyncio.sleep(5)
        await jupiter_listener()

# Raydium Listener
async def raydium_listener():
    await asyncio.sleep(1)
    try:
        async with websockets.connect(RAYDIUM_URL) as ws:
            await send_telegram_alert("📡 Raydium listener live.")
            await ws.send(json.dumps({
                "type": "subscribe",
                "programId": "RVKd61ztZW9GdKzH1fGzWJoqQ9N8mk8h7usqf9cGzKy",
                "commitment": "confirmed"
            }))
            while True:
                try:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    if data.get("type") != "transaction": continue
                    keys = data.get("accountKeys", [])
                    if not is_valid_mint(keys): continue
                    mint = data["events"]["mint"]
                    if mint:
                        await send_telegram_alert(f"🚀 New Raydium token: `{mint}`")
                        await buy_token(mint)
                except Exception as e:
                    print(f"[RAYDIUM ERROR] {e}")
                    await asyncio.sleep(1)
    except Exception as e:
        print(f"[RAYDIUM CONNECT ERROR] {e}")
        await asyncio.sleep(5)
        await raydium_listener()

# Forced Mint
async def test_force_token():
    if FORCE_TEST_MINT:
        await send_telegram_alert(f"🚨 FORCED TEST MODE: Buying test mint\n`{FORCE_TEST_MINT}`")
        success = await buy_token(FORCE_TEST_MINT)
        if not success:
            await send_telegram_alert(f"❌ Buy failed for test mint:\n`{FORCE_TEST_MINT}`")
            log_skipped_token(FORCE_TEST_MINT, "Forced test mint buy failed")
        await asyncio.sleep(2)

# Main Launch
async def start_sniper():
    await send_telegram_alert("✅ Sniper bot launching...")
    await asyncio.gather(
        start_command_bot(),
        test_force_token(),
        jupiter_listener(),
        raydium_listener()
    )
