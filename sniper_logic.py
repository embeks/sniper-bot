# =========================
# sniper_logic.py — Final Version (Smart Auto-Sell by Profit)
# =========================

import asyncio
import json
import os
from solders.pubkey import Pubkey
from dotenv import load_dotenv
import websockets

from utils import (
    send_telegram_alert,
    is_valid_mint,
    buy_token,
    auto_sell_if_profit,
    start_command_bot
)

load_dotenv()

TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
seen_tokens = set()

# ✅ Raydium Listener
async def raydium_listener():
    url = f"wss://api.helius.xyz/v0/addresses/raydium/logs?api-key={os.getenv('HELIUS_API')}"
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [TOKEN_PROGRAM_ID]},
                {"commitment": "processed"}
            ]
        }))
        print("[🔁] Raydium listener subscribed.")
        await send_telegram_alert("📡 Raydium listener live.")

        while True:
            try:
                msg = await ws.recv()
                data = json.loads(msg)
                logs = data.get("params", {}).get("result", {}).get("value", {}).get("logs", [])
                for log in logs:
                    if "Instruction: MintTo" in log or "Instruction: InitializeMint" in log:
                        keys = data["params"]["result"]["value"].get("accountKeys", [])
                        for key in keys:
                            if key in seen_tokens:
                                continue
                            seen_tokens.add(key)
                            print(f"[🔍] Scanning token: {key}")
                            if is_valid_mint([{ 'pubkey': key }]):
                                await send_telegram_alert(f"[🟡] New token: {key}")
                                success, buy_price = await buy_token(key)
                                if success:
                                    await auto_sell_if_profit(key, buy_price)
            except Exception as e:
                print(f"[RAYDIUM ERROR] {e}")
                await asyncio.sleep(1)

# ✅ Jupiter Listener
async def jupiter_listener():
    url = f"wss://api.helius.xyz/v0/addresses/jupiter/logs?api-key={os.getenv('HELIUS_API')}"
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [TOKEN_PROGRAM_ID]},
                {"commitment": "processed"}
            ]
        }))
        print("[🔁] Jupiter listener subscribed.")
        await send_telegram_alert("📡 Jupiter listener live.")

        while True:
            try:
                msg = await ws.recv()
                data = json.loads(msg)
                logs = data.get("params", {}).get("result", {}).get("value", {}).get("logs", [])
                for log in logs:
                    if "Instruction: MintTo" in log or "Instruction: InitializeMint" in log:
                        keys = data["params"]["result"]["value"].get("accountKeys", [])
                        for key in keys:
                            if key in seen_tokens:
                                continue
                            seen_tokens.add(key)
                            print(f"[🔍] Scanning token: {key}")
                            if is_valid_mint([{ 'pubkey': key }]):
                                await send_telegram_alert(f"[🟡] New token: {key}")
                                success, buy_price = await buy_token(key)
                                if success:
                                    await auto_sell_if_profit(key, buy_price)
            except Exception as e:
                print(f"[JUPITER ERROR] {e}")
                await asyncio.sleep(1)

# ✅ Main Runner
async def start_sniper():
    await send_telegram_alert("✅ Sniper bot starting with Raydium + Jupiter...")
    await asyncio.gather(
        asyncio.to_thread(start_command_bot),
        jupiter_listener(),
        raydium_listener()
    )
