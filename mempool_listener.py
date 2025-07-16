import os
import asyncio
import json
import websockets
from datetime import datetime, timedelta
from dotenv import load_dotenv

from utils import send_telegram_alert, get_token_price
from jupiter_trade import buy_token
from trade_logic import auto_sell_if_profit

load_dotenv()

DEBUG = True
BUY_AMOUNT_SOL = 0.027
heartbeat_interval = timedelta(minutes=30)
sniped_tokens = set()

# Helius WS
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
JUPITER_ID = "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB"
RAYDIUM_ID = "RVKd61ztZW9GdKzvXxkzRhK21Z4LzStfgzj31EKXdYv"

# ------------------ Base Listener ------------------ #
async def run_listener(program_id, label):
    uri = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    last_heartbeat = datetime.utcnow()
    first_connection = True

    while True:
        try:
            async with websockets.connect(uri, ping_interval=30, ping_timeout=10) as ws:
                sub_msg = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "logsSubscribe",
                    "params": [
                        {"mentions": [program_id]},
                        {"commitment": "processed", "encoding": "jsonParsed"}
                    ]
                }
                await ws.send(json.dumps(sub_msg))

                if first_connection:
                    await send_telegram_alert(f"📡 {label} listener live...")
                    print(f"[{label}] Listener connected")
                    first_connection = False

                while True:
                    now = datetime.utcnow()
                    if now - last_heartbeat >= heartbeat_interval:
                        await send_telegram_alert(
                            f"❤️ {label} heartbeat @ {now.strftime('%Y-%m-%d %H:%M:%S')} UTC"
                        )
                        last_heartbeat = now

                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=60)
                        timestamp = datetime.utcnow().strftime('%H:%M:%S')
                        print(f"[{timestamp}] [{label}] Raw log: {msg}")

                        data = json.loads(msg)
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

                            if DEBUG:
                                await send_telegram_alert(f"👀 [{label}] Detected mint: {token_mint}")

                            entry_price = await get_token_price(token_mint)
                            if not entry_price:
                                if DEBUG:
                                    print(f"[{label}] No price for {token_mint}, skipping")
                                continue

                            sniped_tokens.add(token_mint)
                            await send_telegram_alert(f"🚨 [{label}] Token seen: {token_mint} — attempting buy")
                            await buy_token(token_mint, BUY_AMOUNT_SOL)
                            await auto_sell_if_profit(token_mint, entry_price)

                    except asyncio.TimeoutError:
                        print(f"[{label}] Timeout — pinging server...")
                        await ws.ping()

                    except Exception as inner_err:
                        print(f"[‼️] {label} error: {inner_err}")
                        await asyncio.sleep(2)
                        break

        except Exception as outer_err:
            print(f"[‼️] {label} WS reconnecting: {outer_err}")
            await asyncio.sleep(5)

# ------------------ Entry Points ------------------ #
async def mempool_listener_jupiter():
    await run_listener(JUPITER_ID, "JUPITER")

async def mempool_listener_raydium():
    await run_listener(RAYDIUM_ID, "RAYDIUM")
