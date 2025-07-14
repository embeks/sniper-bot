import os
import asyncio
import json
import websockets
from dotenv import load_dotenv

from utils import (
    send_telegram_alert,
    check_token_safety,
    has_blacklist_or_mint_functions,
    is_lp_locked_or_burned,
    get_token_price
)
from jupiter_trade import buy_token
from trade_logic import auto_sell_if_profit

load_dotenv()

RAYDIUM_PROGRAM_ID = "RVKd61ztZW9BvU4wjf3GGN2TjK5uAAgnk99bQzVJ8zU"
BUY_AMOUNT_SOL = 0.027
sniped_tokens = set()

async def process_log(log):
    try:
        if "accountKeys" not in log:
            return

        accounts = log["accountKeys"]
        for acc in accounts:
            token_mint = str(acc)

            if (
                token_mint in sniped_tokens or
                token_mint.startswith("So111") or
                len(token_mint) != 44
            ):
                continue

            print(f"[🔎] Found token: {token_mint}")

            # Step 1: Safety filter
            safety = await check_token_safety(token_mint)
            if "❌" in safety or "⚠️" in safety:
                print(f"[⛔] Filtered by check_token_safety: {safety}")
                continue

            # Step 2: Blacklist or mint function check
            if await has_blacklist_or_mint_functions(token_mint):
                print(f"[⛔] Filtered by blacklist/mint function")
                continue

            # Step 3: LP check
            if not await is_lp_locked_or_burned(token_mint):
                print(f"[⛔] Filtered by LP check")
                continue

            await send_telegram_alert(f"🔎 New token: {token_mint}\n{safety}\nAuto-sniping...")

            entry_price = await get_token_price(token_mint)
            if not entry_price:
                await send_telegram_alert("❌ No price found, skipping")
                return

            sniped_tokens.add(token_mint)
            await buy_token(token_mint, BUY_AMOUNT_SOL)
            await auto_sell_if_profit(token_mint, entry_price)

    except Exception as e:
        print(f"[‼️] Error processing log: {e}")
        await send_telegram_alert(f"[‼️] Log handler error:\n{e}")

async def mempool_listener():
    helius_api_key = os.getenv("HELIUS_API_KEY")
    if not helius_api_key:
        print("[‼️] No Helius API Key found in environment.")
        return

    uri = f"wss://mainnet.helius-rpc.com/?api-key={helius_api_key}"

    while True:
        try:
            print("[📡] Connecting to Helius WebSocket...")
            async with websockets.connect(uri, ping_interval=20, ping_timeout=20) as ws:
                sub_msg = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "logsSubscribe",
                    "params": [
                        {"mentions": [RAYDIUM_PROGRAM_ID]},
                        {"commitment": "processed", "encoding": "jsonParsed"}
                    ]
                }
                await ws.send(json.dumps(sub_msg))
                print("[✅] Subscribed to Raydium logs")
                await send_telegram_alert("📡 Mempool listener active...")

                while True:
                    try:
                        message = await asyncio.wait_for(ws.recv(), timeout=60)
                        data = json.loads(message)

                        if "result" in data and "value" in data["result"]:
                            await process_log(data["result"]["value"])

                    except asyncio.TimeoutError:
                        print("[⚠️] Timeout reached, sending ping to keep alive...")
                        await ws.ping()

                    except Exception as inner_e:
                        print(f"[!] Inner loop error: {inner_e}")
                        await asyncio.sleep(3)
                        break  # exit inner loop to reconnect

        except Exception as outer_e:
            print(f"[‼️] WebSocket connection failed: {outer_e}")
            await send_telegram_alert("‼️ Mempool listener crashed. Reconnecting...")
            await asyncio.sleep(5)
