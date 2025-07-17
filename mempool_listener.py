# ====================================
# mempool_listener.py (FINAL VERSION)
# ====================================

import os
import asyncio
import json
import websockets
from dotenv import load_dotenv
from utils import (
    is_token_blacklisted,
    check_token_validity,
    already_sniped,
    log_sniped_token,
    send_telegram_message,
    buy_token
)

load_dotenv()

WS_URL = os.getenv("SOLANA_MEMPOOL_WS")

if not WS_URL:
    raise ValueError("❌ Missing SOLANA_MEMPOOL_WS in .env file")

# Program IDs
RAYDIUM_PROGRAM_ID = "RVKd61ztZW9GdKzYcJ1RMzUWx3o6SLFdBq3v5uQDPmD"
JUPITER_PROGRAM_ID = "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB"

async def handle_mempool_log(log):
    try:
        if log.get("type") != "log":
            return

        inner = log.get("innerInstructions", [])
        if not inner:
            return

        account_keys = log.get("accountKeys", [])
        instructions = inner[0].get("instructions", [])

        for ix in instructions:
            program_id_index = ix.get("programIdIndex")
            if program_id_index is None or program_id_index >= len(account_keys):
                continue

            program_id = account_keys[program_id_index]
            if program_id not in [RAYDIUM_PROGRAM_ID, JUPITER_PROGRAM_ID]:
                continue

            data = ix.get("data")
            if not data:
                continue

            token_address = account_keys[-1]  # Most often last key is token

            if already_sniped(token_address):
                return

            if is_token_blacklisted(token_address):
                print(f"❌ Skipped blacklisted token: {token_address}")
                return

            is_valid, reason = await check_token_validity(token_address)
            if not is_valid:
                print(f"❌ Invalid token ({reason}): {token_address}")
                return

            send_telegram_message(f"🎯 Sniping detected token: {token_address}")
            tx_sig = await buy_token(token_address)
            print(f"✅ Buy tx sent: {tx_sig}")
            log_sniped_token(token_address)

    except Exception as e:
        print(f"❌ Error processing log: {e}")

async def listen():
    try:
        async with websockets.connect(WS_URL) as ws:
            print("✅ Connected to Helius WebSocket")
            sub_msg = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "logsSubscribe",
                "params": [
                    {"mentions": [RAYDIUM_PROGRAM_ID, JUPITER_PROGRAM_ID]},
                    {"commitment": "confirmed"}
                ]
            }
            await ws.send(json.dumps(sub_msg))
            print("📡 Subscribed to Jupiter + Raydium logs")

            while True:
                msg = await ws.recv()
                event = json.loads(msg)
                if "result" in event:
                    continue  # subscription ack
                log_data = event.get("params", {}).get("result", {}).get("value", {})
                await handle_mempool_log(log_data)

    except websockets.exceptions.InvalidStatusCode as e:
        print(f"❌ WebSocket rejected: {e.status_code} — check your Helius key")
    except Exception as e:
        print(f"❌ WebSocket error: {e}")

if __name__ == "__main__":
    asyncio.run(listen())
