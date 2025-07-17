import os
import json
import asyncio
import websockets
from datetime import datetime, timedelta
from dotenv import load_dotenv

from utils import send_telegram_alert, get_token_price
from jupiter_trade import buy_token
from trade_logic import auto_sell_if_profit

load_dotenv()

HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
BUY_AMOUNT_SOL = float(os.getenv("BUY_AMOUNT_SOL", 0.2))

# WebSocket setup
HELIUS_WS = f"wss://mainnet.helius-rpc.com/v1/ws?api-key={HELIUS_API_KEY}"
JUPITER_PROGRAM = "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB"
RAYDIUM_PROGRAM = "RVKd61ztZW9GdKzvXxkzRhK21Z4LzStfgzj31EKXdYv"

sniped_tokens = set()
heartbeat_interval = timedelta(hours=4)

# Load already-sniped tokens
if os.path.exists("sniped_tokens.txt"):
    with open("sniped_tokens.txt", "r") as f:
        sniped_tokens = set(line.strip() for line in f)

# 🔁 Handle incoming log data
async def handle_log(message, listener_name):
    try:
        data = json.loads(message)
        result = data.get("result")
        if not isinstance(result, dict):
            return

        log = result.get("value", {})
        accounts = log.get("accountKeys", [])
        if not isinstance(accounts, list):
            return

        for token_mint in accounts:
            if len(token_mint) != 44 or token_mint.startswith("So111"):
                continue
            if token_mint in sniped_tokens:
                continue

            sniped_tokens.add(token_mint)
            with open("sniped_tokens.txt", "a") as f:
                f.write(f"{token_mint}\n")

            await send_telegram_alert(f"👀 [{listener_name}] Detected mint: {token_mint}")

            entry_price = await get_token_price(token_mint)
            if not entry_price:
                await send_telegram_alert(f"⚠️ [{listener_name}] Couldn't fetch price, skipping buy")
                continue

            await send_telegram_alert(f"🚨 [{listener_name}] Attempting snipe: {token_mint}")
            await buy_token(token_mint, BUY_AMOUNT_SOL)
            await auto_sell_if_profit(token_mint, entry_price)

    except Exception as e:
        print(f"[‼️] {listener_name} error: {e}")
        await send_telegram_alert(f"[‼️] {listener_name} log error:\n{e}")

# 🌐 Listener function with heartbeat + reconnect
async def listen_to_program(program_id, listener_name):
    last_heartbeat = datetime.utcnow()
    while True:
        try:
            async with websockets.connect(HELIUS_WS, ping_interval=30, ping_timeout=10) as ws:
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
                await send_telegram_alert(f"📡 {listener_name} connected — ✅ Starting sniper bot with dual sockets...")
                print(f"[📡] Subscribed to {listener_name}")

                while True:
                    now = datetime.utcnow()
                    if now - last_heartbeat >= heartbeat_interval:
                        await send_telegram_alert(f"❤️ {listener_name} heartbeat @ {now.strftime('%H:%M:%S')} UTC")
                        last_heartbeat = now

                    try:
                        message = await asyncio.wait_for(ws.recv(), timeout=60)
                        await handle_log(message, listener_name)
                    except asyncio.TimeoutError:
                        print(f"[{listener_name}] Timeout, pinging server...")
                        await ws.ping()
        except Exception as e:
            print(f"[‼️] {listener_name} WS error: {e}")
            await send_telegram_alert(f"[‼️] {listener_name} WS crashed. Retrying in 10s...\n{e}")
            await asyncio.sleep(10)

# 🎯 Entry points
async def mempool_listener_jupiter():
    await listen_to_program(JUPITER_PROGRAM, "JUPITER")

async def mempool_listener_raydium():
    await listen_to_program(RAYDIUM_PROGRAM, "RAYDIUM")
