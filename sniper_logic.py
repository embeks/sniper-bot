import os
import asyncio
import json
from utils import send_telegram_alert, is_valid_mint, snipe_token
from solders.pubkey import Pubkey
import websockets

# Constants
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
seen_tokens = set()

async def check_force_test_mint():
    test_mint = os.getenv("FORCE_TEST_MINT")
    if test_mint:
        if test_mint in seen_tokens:
            print(f"[⚠️] Test mint already seen: {test_mint}")
        else:
            print(f"[🚨] FORCE TEST MINT active! Sniping {test_mint}...")
            seen_tokens.add(test_mint)
            await send_telegram_alert(f"[🚨] FORCE TEST MINT active! Sniping {test_mint}")
            await snipe_token(test_mint)

async def mempool_listener(label):
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
        print(f"[🔁] {label} listener subscribed.")
        await send_telegram_alert(f"📡 {label.upper()} listener active... ✅ Starting sniper bot with dual sockets (Jupiter + Raydium)...")

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
                print(f"[{label.upper()} ERROR] {e}")
                await asyncio.sleep(1)

async def run_sniper():
    await send_telegram_alert("✅ Sniper bot is now live and scanning the mempool...")
    await check_force_test_mint()
    await asyncio.gather(
        mempool_listener("JUPITER"),
        mempool_listener("RAYDIUM")
    )

if __name__ == "__main__":
    asyncio.run(run_sniper())
