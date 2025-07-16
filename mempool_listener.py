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
sniped_tokens = set()
heartbeat_interval = timedelta(minutes=30)
last_heartbeat = datetime.utcnow()

HELIUS_URL = f"wss://mainnet.helius-rpc.com/?api-key={os.getenv('HELIUS_API_KEY')}"
JUPITER_PROGRAM = "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB"
RAYDIUM_PROGRAM = "RVKd61ztZW9GdKzvXxkzRhK21Z4LzStfgzj31EKXdYv"

async def run_listener(program_name, program_id):
    global last_heartbeat
    while True:
        try:
            async with websockets.connect(HELIUS_URL, ping_interval=30, ping_timeout=10) as ws:
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
                await send_telegram_alert(f"📡 {program_name} listener live...")
                print(f"[📡] {program_name} listener active...")

                while True:
                    now = datetime.utcnow()
                    if now - last_heartbeat >= heartbeat_interval:
                        await send_telegram_alert(
                            f"❤️ {program_name} Heartbeat [{now.strftime('%H:%M:%S')} UTC]"
                        )
                        last_heartbeat = now

                    try:
                        message = await asyncio.wait_for(ws.recv(), timeout=60)
                        timestamp = datetime.utcnow().strftime('%H:%M:%S')
                        print(f"[{timestamp}] {program_name} Raw log: {message}")

                        data = json.loads(message)
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
                                await send_telegram_alert(f"👀 {program_name} mint: {token_mint}")

                            entry_price = await get_token_price(token_mint)
                            if not entry_price:
                                if DEBUG:
                                    print(f"[DEBUG] {token_mint} has no price, skipping")
                                continue

                            sniped_tokens.add(token_mint)
                            await send_telegram_alert(f"🚨 {program_name} snipe: {token_mint}")
                            await buy_token(token_mint, BUY_AMOUNT_SOL)
                            await auto_sell_if_profit(token_mint, entry_price)

                    except asyncio.TimeoutError:
                        print(f"[⚠️] {program_name} recv timeout — pinging")
                        await ws.ping()

                    except Exception as err:
                        print(f"[‼️] {program_name} error: {err}")
                        await asyncio.sleep(2)
                        break

        except Exception as outer:
            print(f"[‼️] {program_name} connection failed: {outer}")
            await asyncio.sleep(5)

# Dual-socket exposed functions
async def mempool_listener_jupiter():
    await run_listener("JUPITER", JUPITER_PROGRAM)

async def mempool_listener_raydium():
    await run_listener("RAYDIUM", RAYDIUM_PROGRAM)
