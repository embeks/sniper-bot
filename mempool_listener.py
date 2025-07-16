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

# Separate program IDs
JUPITER_ID = "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB"
RAYDIUM_ID = "RVKd61ztZW9GdKzvXxkzRhK21Z4LzStfgzj31EKXdYv"

async def subscribe_logs(uri, program_id, name):
    global last_heartbeat
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
            await send_telegram_alert(f"📡 Mempool listener active on {name}...")
            if DEBUG:
                print(f"[📡] Listening to {name} logs...")

            while True:
                now = datetime.utcnow()
                if now - last_heartbeat >= heartbeat_interval:
                    await send_telegram_alert(
                        f"❤️ Bot still alive [Heartbeat @ {now.strftime('%Y-%m-%d %H:%M:%S')} UTC]"
                    )
                    last_heartbeat = now

                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=60)
                    timestamp = datetime.utcnow().strftime('%H:%M:%S')
                    print(f"[{timestamp}] Raw log ({name}): {message[:150]}...")

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
                            await send_telegram_alert(f"👀 Detected mint on {name}: {token_mint}")

                        entry_price = await get_token_price(token_mint)
                        if not entry_price:
                            if DEBUG:
                                print(f"[DEBUG] {token_mint} has no price, skipping")
                            continue

                        sniped_tokens.add(token_mint)
                        await send_telegram_alert(f"🚨 Token from {name}: {token_mint} — attempting buy")
                        await buy_token(token_mint, BUY_AMOUNT_SOL)
                        await auto_sell_if_profit(token_mint, entry_price)

                except asyncio.TimeoutError:
                    print(f"[⚠️] Timeout on {name}, pinging...")
                    await ws.ping()

                except Exception as err:
                    print(f"[‼️] WS recv error on {name}: {err}")
                    await asyncio.sleep(2)
                    break  # reconnect

    except Exception as outer:
        print(f"[‼️] WS connection failed on {name}: {outer}")
        await asyncio.sleep(5)

async def mempool_listener():
    helius_api_key = os.getenv("HELIUS_API_KEY")
    if not helius_api_key:
        print("[‼️] No Helius API Key found.")
        return

    uri = f"wss://mainnet.helius-rpc.com/?api-key={helius_api_key}"

    await asyncio.gather(
        subscribe_logs(uri, JUPITER_ID, "Jupiter"),
        subscribe_logs(uri, RAYDIUM_ID, "Raydium")
    )

if __name__ == "__main__":
    asyncio.run(mempool_listener())
