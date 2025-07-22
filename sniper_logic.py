# =========================
# sniper_logic.py — Elite (Free Helius WebSocket)
# =========================

import asyncio
import json
import os
import websockets
from dotenv import load_dotenv

from utils import (
    is_valid_mint,
    wait_and_auto_sell,
    buy_token,
    send_telegram_alert,
    start_command_bot
)

load_dotenv()
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
HELIUS_API = os.getenv("HELIUS_API")
seen_tokens = set()

# ✅ Raydium Listener
async def raydium_listener():
    url = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API}"  # Free-tier compatible
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
                            print(f"[🔍] Token found: {key}")

                            if is_valid_mint([{ 'pubkey': key }]):
                                await send_telegram_alert(f"[🟡] Valid token found: {key}")
                                success = await buy_token(key)
                                if success:
                                    await wait_and_auto_sell(key)
                            else:
                                await send_telegram_alert(f"⛔ Skipped token (invalid mint): {key}")
            except Exception as e:
                print(f"[RAYDIUM ERROR] {e}")
                await asyncio.sleep(1)

# ✅ Jupiter Listener
async def jupiter_listener():
    url = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API}"  # Free-tier compatible
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
                            print(f"[🔍] Token found: {key}")

                            if is_valid_mint([{ 'pubkey': key }]):
                                await send_telegram_alert(f"[🟡] Valid token found: {key}")
                                success = await buy_token(key)
                                if success:
                                    await wait_and_auto_sell(key)
                            else:
                                await send_telegram_alert(f"⛔ Skipped token (invalid mint): {key}")
            except Exception as e:
                print(f"[JUPITER ERROR] {e}")
                await asyncio.sleep(1)

# ✅ Entry
async def start_sniper():
    await send_telegram_alert("✅ Sniper bot launching...")
    await asyncio.gather(
        start_command_bot(),  # <- this line is now CORRECT
        jupiter_listener(),
        raydium_listener()
    )
    )
