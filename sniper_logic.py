# =========================
# sniper_logic.py 
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
FORCE_TEST_MINT = os.getenv("FORCE_TEST_MINT")

seen_tokens = set()

# ✅ Mempool Listener (Raydium or Jupiter)
async def mempool_listener(source="Raydium"):
    url = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API}"
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
        print(f"[🔁] {source} listener subscribed.")
        await send_telegram_alert(f"📡 {source} listener live.")

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
                print(f"[{source} ERROR] {e}")
                await asyncio.sleep(1)

# ✅ Entry Point
async def start_sniper():
    await send_telegram_alert("✅ Sniper bot launching...")

    # Start Telegram command bot first
    await start_command_bot()

    # Force test mode
    if FORCE_TEST_MINT:
        await send_telegram_alert(f"🚨 Forced Test Mode: Buying {FORCE_TEST_MINT}")
        success = await buy_token(FORCE_TEST_MINT)
        if success:
            await wait_and_auto_sell(FORCE_TEST_MINT)
        return

    # Start mempool listeners
    await asyncio.gather(
        mempool_listener("Raydium"),
        mempool_listener("Jupiter")
    )
