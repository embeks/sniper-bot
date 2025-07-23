# =========================
# sniper_logic.py — ELITE VERSION
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
    get_token_data,
    log_skipped_token,
    send_telegram_alert,
    start_command_bot
)

load_dotenv()
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
HELIUS_API = os.getenv("HELIUS_API")
RUG_LP_THRESHOLD = float(os.getenv("RUG_LP_THRESHOLD", 0.75))
seen_tokens = set()

# ✅ Rug Check Before Buy
async def rug_filter_passes(mint):
    try:
        data = await get_token_data(mint)
        if not data:
            await send_telegram_alert(f"❌ No BirdEye data for {mint}")
            log_skipped_token(mint, "Missing BirdEye data")
            return False

        lp = data.get("liquidity", 0)
        renounced = data.get("renounced", False)
        locked = data.get("lp_locked", False)

        if lp < RUG_LP_THRESHOLD:
            log_skipped_token(mint, "Low Liquidity")
            await send_telegram_alert(f"⛔ Skipped {mint} — LP too low: {lp}")
            return False

        if not renounced and not locked:
            log_skipped_token(mint, "Ownership not renounced + LP not locked")
            await send_telegram_alert(f"⛔ Skipped {mint} — Unsafe ownership/LP")
            return False

        return True
    except Exception as e:
        await send_telegram_alert(f"⚠️ Rug filter error for {mint}: {e}")
        return False

# ✅ General Listener (Raydium & Jupiter)
async def mempool_listener(name):
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
        print(f"[🔁] {name} listener subscribed.")
        await send_telegram_alert(f"📡 {name} listener live.")

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

                                # ✅ Apply Rug Filter
                                safe = await rug_filter_passes(key)
                                if not safe:
                                    continue

                                # ✅ Real Buy
                                success = await buy_token(key)
                                if success:
                                    await wait_and_auto_sell(key)
                            else:
                                await send_telegram_alert(f"⛔ Skipped token (invalid mint): {key}")
            except Exception as e:
                print(f"[{name} ERROR] {e}")
                await asyncio.sleep(1)

# ✅ Entry
async def start_sniper():
    await send_telegram_alert("✅ Sniper bot launching...")
    await asyncio.gather(
        start_command_bot(),
        mempool_listener("Raydium"),
        mempool_listener("Jupiter")
    )
