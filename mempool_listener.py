import os
import asyncio
import json
import websockets
from datetime import datetime, timedelta
from dotenv import load_dotenv

from utils import (
    send_telegram_alert,
    get_token_price
)
from jupiter_trade import buy_token
from trade_logic import auto_sell_if_profit

load_dotenv()

# === CONFIG ===
DEBUG = True
BUY_AMOUNT_SOL = 0.027
sniped_tokens = set()
mempool_announced = False
heartbeat_interval = timedelta(minutes=30)
last_heartbeat = datetime.utcnow()

async def mempool_listener():
    global mempool_announced, last_heartbeat
    helius_api_key = os.getenv("HELIUS_API_KEY")
    if not helius_api_key:
        print("[‼️] No Helius API Key found in environment.")
        return

    uri = f"wss://mainnet.helius-rpc.com/?api-key={helius_api_key}"

    while True:
        try:
            async with websockets.connect(uri, ping_interval=30, ping_timeout=10) as ws:
                sub_msg = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "logsSubscribe",
                    "params": [
                        {},  # RAW MODE — catch everything
                        {"commitment": "processed", "encoding": "jsonParsed"}
                    ]
                }
                await ws.send(json.dumps(sub_msg))

                await send_telegram_alert("📡 Mempool listener active (RAW MODE)...")
                print("[INFO] Subscribed to logs (raw mode)...")
                mempool_announced = True

                while True:
                    now = datetime.utcnow()
                    if now - last_heartbeat >= heartbeat_interval:
                        await send_telegram_alert(
                            f"❤️ Bot is still running [Heartbeat @ {now.strftime('%Y-%m-%d %H:%M:%S')} UTC]"
                        )
                        last_heartbeat = now

                    try:
                        message = await asyncio.wait_for(ws.recv(), timeout=60)
                        data = json.loads(message)

                        if DEBUG:
                            print("[DEBUG] Full raw message:", json.dumps(data)[:400])

                        result = data.get("result", {})
                        log = result.get("value", {})
                        accounts = log.get("accountKeys", [])

                        if not isinstance(accounts, list):
                            continue

                        for token_mint in accounts:
                            if len(token_mint) != 44 or token_mint.startswith("So111"):
                                continue
                            if token_mint in sniped_tokens:
                                continue

                            await send_telegram_alert(f"🚨 Raw token seen: {token_mint} — attempting buy")

                            entry_price = await get_token_price(token_mint)
                            if not entry_price:
                                await send_telegram_alert(f"❌ {token_mint}: No price found, skipping")
                                continue

                            sniped_tokens.add(token_mint)
                            await buy_token(token_mint, BUY_AMOUNT_SOL)
                            await auto_sell_if_profit(token_mint, entry_price)

                    except asyncio.TimeoutError:
                        print("[⚠️] Timeout waiting for ws.recv() — pinging server to keep alive...")
                        await ws.ping()
                        continue

                    except Exception as inner_e:
                        print(f"[‼️] Inner loop error: {inner_e}")
                        await asyncio.sleep(2)
                        break

        except Exception as outer_e:
            print(f"[‼️] Mempool connection failed: {outer_e}")
            mempool_announced = False
            await asyncio.sleep(5)
