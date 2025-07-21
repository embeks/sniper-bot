# =========================
# sniper_logic.py — Clean Final Version (Live Buys + Telegram Alerts)
# =========================

import asyncio
import json
import os
from dotenv import load_dotenv
from solders.pubkey import Pubkey
from utils import send_telegram_alert, force_buy_token

load_dotenv()

TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
seen_tokens = set()

# ✅ One-time forced test buy
async def force_test_buy_if_present():
    mint = os.getenv("FORCE_TEST_MINT")
    if mint:
        await send_telegram_alert(f"🧪 FORCE_TEST_MINT detected: {mint}")
        try:
            _ = Pubkey.from_string(mint)
        except Exception:
            await send_telegram_alert("❌ Invalid FORCE_TEST_MINT format.")
            return
        await force_buy_token(mint)
        await send_telegram_alert("✅ Force buy attempt complete.")

# ✅ Jupiter mempool listener
async def mempool_listener_jupiter():
    import websockets

    url = os.getenv("SOLANA_MEMPOOL_WS")
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
        await send_telegram_alert("📡 Jupiter listener active...")

        while True:
            try:
                msg = await ws.recv()
                data = json.loads(msg)
                logs = data.get("params", {}).get("result", {}).get("value", {}).get("logs", [])
                for log in logs:
                    if "Instruction: MintTo" in log or "Instruction: InitializeMint" in log:
                        account_keys = data["params"]["result"]["value"].get("accountKeys", [])
                        for key in account_keys:
                            if key in seen_tokens:
                                continue
                            seen_tokens.add(key)
                            print(f"[MINT DETECTED] {key}")
                            await send_telegram_alert(f"🟡 New token: {key}")
                            await force_buy_token(key)
            except Exception as e:
                print(f"[JUPITER ERROR] {e}")
                await asyncio.sleep(1)

# ✅ Raydium mempool listener
async def mempool_listener_raydium():
    import websockets

    url = os.getenv("SOLANA_MEMPOOL_WS")
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
        await send_telegram_alert("📡 Raydium listener active...")

        while True:
            try:
                msg = await ws.recv()
                data = json.loads(msg)
                logs = data.get("params", {}).get("result", {}).get("value", {}).get("logs", [])
                for log in logs:
                    if "Instruction: MintTo" in log or "Instruction: InitializeMint" in log:
                        account_keys = data["params"]["result"]["value"].get("accountKeys", [])
                        for key in account_keys:
                            if key in seen_tokens:
                                continue
                            seen_tokens.add(key)
                            print(f"[MINT DETECTED] {key}")
                            await send_telegram_alert(f"🟡 New token: {key}")
                            await force_buy_token(key)
            except Exception as e:
                print(f"[RAYDIUM ERROR] {e}")
                await asyncio.sleep(1)

# ✅ Main runner
async def start_sniper():
    await force_test_buy_if_present()
    await send_telegram_alert("✅ Sniper bot LIVE and listening...")
    await asyncio.gather(
        mempool_listener_jupiter(),
        mempool_listener_raydium()
    )

if __name__ == "__main__":
    asyncio.run(start_sniper())
