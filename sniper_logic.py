import asyncio
import json
import os
from utils import send_telegram_alert, is_valid_mint, snipe_token
from solders.pubkey import Pubkey

TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
seen_tokens = set()

async def try_force_test_snipe():
    force_mint = os.getenv("FORCE_TEST_MINT")
    if force_mint:
        await send_telegram_alert(f"🧪 TEST MODE: Sniping forced mint {force_mint}")
        await snipe_token(force_mint)

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
        await send_telegram_alert("📡 JUPITER listener active... ✅ Starting sniper bot with dual sockets (Jupiter + Raydium)...")
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
                            print(f"[🔍] Scanning token: {key}")
                            if is_valid_mint(key):
                                await send_telegram_alert(f"[🟡] Detected new token mint: {key}")
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
            "params": [
                {"mentions": [TOKEN_PROGRAM_ID]},
                {"commitment": "processed"}
            ]
        }))
        await send_telegram_alert("📡 RAYDIUM listener active... ✅ Starting sniper bot with dual sockets (Jupiter + Raydium)...")
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
                            print(f"[🔍] Scanning token: {key}")
                            if is_valid_mint(key):
                                await send_telegram_alert(f"[🟡] Detected new token mint: {key}")
                                await snipe_token(key)
            except Exception as e:
                print(f"[RAYDIUM ERROR] {e}")
                await asyncio.sleep(1)

async def run_sniper():
    await send_telegram_alert("✅ Sniper bot is now live and scanning the mempool...")
    await try_force_test_snipe()
    await asyncio.gather(
        mempool_listener_jupiter(),
        mempool_listener_raydium()
    )

if __name__ == "__main__":
    asyncio.run(run_sniper())
