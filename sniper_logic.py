import os
import asyncio
import json
from utils import send_telegram_alert, is_valid_mint, snipe_token
from solders.pubkey import Pubkey

# ✅ Token program ID (needed for log filters)
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

# ✅ Store tokens to avoid duplicates
seen_tokens = set()

# ✅ Forced test snipe if env var is present
async def check_force_test_mint():
    test_mint = os.getenv("FORCE_TEST_MINT")
    if test_mint:
        await send_telegram_alert(f"[TEST MODE] 🔫 Forcing test snipe on mint: {test_mint}")
        if is_valid_mint(test_mint):
            await snipe_token(test_mint)
        else:
            await send_telegram_alert("❌ Invalid test mint format.")
        await asyncio.sleep(3)

# ✅ Jupiter listener
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

# ✅ Raydium listener
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

# ✅ Main function
async def run_sniper():
    await send_telegram_alert("✅ Sniper bot is now live and scanning the mempool...")

    # ✅ Trigger force test logic if test mint is set
    await check_force_test_mint()

    # ✅ Start mempool listeners
    await asyncio.gather(
        mempool_listener_jupiter(),
        mempool_listener_raydium()
    )

if __name__ == "__main__":
    asyncio.run(run_sniper())
