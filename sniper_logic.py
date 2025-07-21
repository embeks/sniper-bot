import asyncio
import json
import os
from utils import send_telegram_alert, is_valid_mint, snipe_token
from solders.pubkey import Pubkey

# Hardcoded Token Program ID (Solana SPL Token Program)
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

# Track seen tokens to avoid duplicate snipes
seen_tokens = set()

# Force test token to simulate a live launch (you can disable after test)
FORCE_TEST_MINT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"

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
        print("[🔁] Jupiter listener subscribed.")
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
        print("[🔁] Raydium listener subscribed.")
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

# ✅ One-time forced test trigger
async def trigger_test_token():
    if FORCE_TEST_MINT not in seen_tokens:
        seen_tokens.add(FORCE_TEST_MINT)
        print(f"[TEST] Forcing test snipe for {FORCE_TEST_MINT}")
        await send_telegram_alert(f"🚨 [TEST] Forcing snipe for {FORCE_TEST_MINT}")
        await snipe_token(FORCE_TEST_MINT)
    else:
        print(f"[TEST] Test token already handled.")

# ✅ Start all logic
async def run_sniper():
    await send_telegram_alert("✅ Sniper bot is now live and scanning the mempool...")
    await trigger_test_token()
    await asyncio.gather(
        mempool_listener_jupiter(),
        mempool_listener_raydium()
    )

if __name__ == "__main__":
    asyncio.run(run_sniper())
