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

DEBUG = True
BUY_AMOUNT_SOL = 0.027
sniped_tokens = set()
mempool_announced = False
heartbeat_interval = timedelta(hours=4)
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
            async with websockets.connect(uri) as ws:
                sub_msg = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "logsSubscribe",
                    "params": [
                        {"filter": {"all": []}},  # ⬅️ Catch all logs
                        {"commitment": "processed", "encoding": "jsonParsed"}
                    ]
                }
                await ws.send(json.dumps(sub_msg))

                if not mempool_announced:
                    await send_telegram_alert("📡 Mempool listener active (RAW MODE)...")
                    mempool_announced = True

                while True:
                    try:
                        now = datetime.utcnow()
                        if now - last_heartbeat >= heartbeat_interval:
                            await send_telegram_alert(
                                f"❤️ Bot is still running [Heartbeat @ {now.strftime('%Y-%m-%d %H:%M:%S')} UTC]"
                            )
                            last_heartbeat = now

                        message = await ws.recv()
                        data = json.loads(message)

                        result = data.get("result")
                        if not isinstance(result, dict):
                            continue

                        log = result.get("value", {})
                        accounts = log.get("accountKeys", [])
                        if not isinstance(accounts, list):
                            continue

                        for token_mint in accounts:
                            if len(token_mint) != 44:
                                continue
                            if token_mint in sniped_tokens:
                                continue
                            if token_mint.startswith("So111"):
                                continue

                            await send_telegram_alert(f"🚨 Raw token seen: {token_mint} — attempting buy")

                            entry_price = await get_token_price(token_mint)
                            if not entry_price:
                                await send_telegram_alert(f"❌ {token_mint}: No price found, skipping")
                                continue

                            sniped_tokens.add(token_mint)
                            await buy_token(token_mint, BUY_AMOUNT_SOL)
                            await auto_sell_if_profit(token_mint, entry_price)

                    except Exception as inner_e:
                        print(f"[!] Inner loop error: {inner_e}")
                        await asyncio.sleep(2)
                        break

        except Exception as outer_e:
            print(f"[‼️] Mempool connection failed: {outer_e}")
            mempool_announced = False
            await asyncio.sleep(5)
