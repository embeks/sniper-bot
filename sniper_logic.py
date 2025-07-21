# =========================
# sniper_logic.py — Final Version (Live Buy w/ Real Force Test + Mempool)
# =========================

import asyncio
import json
import os
from dotenv import load_dotenv
from utils import (
    send_telegram_alert,
    is_valid_mint,
    snipe_token
)
from solders.pubkey import Pubkey

load_dotenv()

TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
seen_tokens = set()

async def force_test_buy_if_present():
    mint = os.getenv("FORCE_TEST_MINT")
    if mint:
        await send_telegram_alert(f"TEST MODE 🧪 FORCETESTMINT detected: {mint}")
        try:
            _ = Pubkey.from_string(mint)
        except Exception:
            await send_telegram_alert("❌ Invalid FORCE_TEST_MINT format.")
            return
        await send_telegram_alert("TEST MODE ✅ Mint is valid. Attempting forced buy...")
        await snipe_token(mint)
        await send_telegram_alert("TEST MODE 🟢 Forced buy attempt complete.")

async def mempool_listener_jupiter():
    import websockets
    url = os.getenv("SOLANA_MEMPOOL_WS")
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsSubscribe",
            "params": [{"mentions": [TOKEN_PROGRAM_ID]}, {"commitment": "processed"}]
        }))
        await send_telegram_alert("📡 JUPITER listener active... ✅ Starting sniper bot with dual sockets (Jupiter + Raydium)...")
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
                            if is_valid_mint([{ "pubkey": key }]):
                                await send_telegram_alert(f"🟡 Detected new token mint: {key}")
                                await snipe_token(key)
            except Exception as e:
                print(f"[JUPITER ERROR] {e}")
                await asyncio.sleep(1)

async def mempool_listener_raydium():
    import websockets
    url = os.getenv("SOLANA_MEMPOOL_WS")
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsSubscribe",
            "params": [{"mentions": [TOKEN_PROGRAM_ID]}, {"commitment": "processed"}]
        }))
        await send_telegram_alert("📡 RAYDIUM listener active... ✅ Starting sniper bot with dual sockets (Jupiter + Raydium)...")
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
                            if is_valid_mint([{ "pubkey": key }]):
                                await send_telegram_alert(f"🟡 Detected new token mint: {key}")
                                await snipe_token(key)
            except Exception as e:
                print(f"[RAYDIUM ERROR] {e}")
                await asyncio.sleep(1)

async def start_sniper():
    await force_test_buy_if_present()
    await send_telegram_alert("✅ Sniper bot is now live and scanning the mempool...")
    await asyncio.gather(
        mempool_listener_jupiter(),
        mempool_listener_raydium()
    )

if __name__ == "__main__":
    asyncio.run(start_sniper())
