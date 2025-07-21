import asyncio
import json
import os
from utils import send_telegram_alert, is_valid_mint, snipe_token
from solders.pubkey import Pubkey

# ✅ Define TOKEN_PROGRAM_ID directly
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

# ✅ Track seen tokens to prevent duplicate snipes
seen_tokens = set()

# ✅ Get forced test token from env
FORCE_TEST_MINT = os.getenv("FORCE_TEST_MINT")

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

# ✅ Force a test snipe for validation
async def trigger_test_token():
    if FORCE_TEST_MINT:
        print(f"[TEST] Triggering forced snipe on: {FORCE_TEST_MINT}")
        await send_telegram_alert(f"🚨 [TEST] Forced test mint detected: {FORCE_TEST_MINT}")
        if is_valid_mint(FORCE_TEST_MINT):
            await snipe_token(FORCE_TEST_MINT)
        else:
            await send_telegram_alert("❌ [TEST] Test mint failed validation.")

# ✅ Run both listeners in parallel after test
async def run_sniper():
    await send_telegram_alert("✅ Sniper bot is now live and scanning the mempool...")

    # 👇 Force test snipe on boot
    await trigger_test_token()

    await asyncio.gather(
        mempool_listener_jupiter(),
        mempool_listener_raydium()
    )

if __name__ == "__main__":
    asyncio.run(run_sniper())
