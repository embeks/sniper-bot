# =========================
# sniper_logic.py — Final Elite Version (Real Buys, Telegram Alerts, Rug Protection)
# =========================

import asyncio
import json
import os
from dotenv import load_dotenv
from solders.pubkey import Pubkey

from utils import (
    send_telegram_alert,
    is_valid_mint,
    snipe_token
)

load_dotenv()

TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
seen_tokens = set()

# ✅ One-time forced test buy
async def force_test_buy_if_present():
    mint = os.getenv("FORCE_TEST_MINT")
    if mint:
        await send_telegram_alert(f"[TEST MODE] 🧪 FORCE_TEST_MINT detected: {mint}")
        try:
            _ = Pubkey.from_string(mint)
        except Exception:
            await send_telegram_alert("❌ Invalid FORCE_TEST_MINT format.")
            return
        await send_telegram_alert(f"[TEST MODE] ✅ Mint is valid. Attempting forced buy...")
        await snipe_token(mint)
        await send_telegram_alert(f"[TEST MODE] 🟢 Forced buy attempt complete.")

# ✅ Mempool listener base (shared logic)
async def mempool_listener(name: str):
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
        print(f"[🔁] {name} listener subscribed.")
        await send_telegram_alert(f"📡 {name.upper()} listener active... ✅ Starting bot...")

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
                            if is_valid_mint([{ 'pubkey': key }]):
                                await send_telegram_alert(f"[🟡] New token mint on {name}: {key}")
                                await snipe_token(key)
            except Exception as e:
                print(f"[{name.upper()} ERROR] {e}")
                await asyncio.sleep(1)

# ✅ Combined runner
async def start_sniper():
    await force_test_buy_if_present()  # ← this runs BEFORE listeners
    await send_telegram_alert("✅ Sniper bot is now live and scanning the mempool...")
    await asyncio.gather(
        mempool_listener("Jupiter"),
        mempool_listener("Raydium")
    )

if __name__ == "__main__":
    asyncio.run(start_sniper())
