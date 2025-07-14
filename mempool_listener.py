import os
import asyncio
import json
import websockets
from datetime import datetime
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
mempool_announced = False

LOG_FILE = "sniper.log"
HEARTBEAT_INTERVAL = 4 * 60 * 60  # 4 hours in seconds

async def log_event(message: str):
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{timestamp}] {message}\n"
    with open(LOG_FILE, "a") as f:
        f.write(log_line)
    print(log_line.strip())

async def heartbeat():
    while True:
        await send_telegram_alert("❤️ Heartbeat: Mempool listener running.")
        await log_event("Heartbeat sent to Telegram")
        await asyncio.sleep(HEARTBEAT_INTERVAL)

async def mempool_listener():
    global mempool_announced
    helius_api_key = os.getenv("HELIUS_API_KEY")
    if not helius_api_key:
        await log_event("[‼️] No Helius API Key found in environment.")
        return

    uri = f"wss://mainnet.helius-rpc.com/?api-key={helius_api_key}"

    asyncio.create_task(heartbeat())

    while True:
        try:
            async with websockets.connect(uri) as ws:
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

                if not mempool_announced:
                    await send_telegram_alert("📡 Mempool listener active...")
                    await log_event("📡 Mempool listener active...")
                    mempool_announced = True

                while True:
                    try:
                        message = await ws.recv()
                        data = json.loads(message)

                        if "result" in data and "value" in data["result"]:
                            log = data["result"]["value"]
                            if "accountKeys" in log:
                                accounts = log["accountKeys"]
                                for acc in accounts:
                                    token_mint = str(acc)

                                    if (
                                        token_mint in sniped_tokens or
                                        token_mint.startswith("So111") or
                                        len(token_mint) != 44
                                    ):
                                        continue

                                    # 🧠 Pre-buy filters
                                    safety = await check_token_safety(token_mint)
                                    if "❌" in safety or "⚠️" in safety:
                                        continue
                                    if await has_blacklist_or_mint_functions(token_mint):
                                        continue
                                    if not await is_lp_locked_or_burned(token_mint):
                                        continue

                                    await send_telegram_alert(f"🔎 New token: {token_mint}\n{safety}\nAuto-sniping...")
                                    await log_event(f"Sniping token: {token_mint}")

                                    entry_price = await get_token_price(token_mint)
                                    if not entry_price:
                                        await send_telegram_alert("❌ No price found, skipping")
                                        await log_event(f"❌ No price found for {token_mint}")
                                        continue

                                    sniped_tokens.add(token_mint)
                                    await buy_token(token_mint, BUY_AMOUNT_SOL)
                                    await auto_sell_if_profit(token_mint, entry_price)

                    except Exception as inner_e:
                        await log_event(f"[!] Inner loop error: {inner_e}")
                        await asyncio.sleep(2)
                        break

        except Exception as outer_e:
            await log_event(f"[‼️] Mempool connection failed: {outer_e}")
            mempool_announced = False
            await asyncio.sleep(5)
