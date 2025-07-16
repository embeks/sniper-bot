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
BUY_AMOUNT_SOL = 0.2  # Adjust based on how much SOL you want to spend
heartbeat_interval = timedelta(minutes=30)

# Helius RPC WebSocket URL
HELIUS_WS = f"wss://mainnet.helius-rpc.com/?api-key={os.getenv('HELIUS_API_KEY')}"

# Jupiter and Raydium Program IDs
JUPITER_PROGRAM = "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB"
RAYDIUM_PROGRAM = "RVKd61ztZW9GdKzvXxkzRhK21Z4LzStfgzj31EKXdYv"

# Track already-sniped tokens
sniped_tokens = set()

# 🔁 Load previously sniped tokens from file
if os.path.exists("sniped_tokens.txt"):
    with open("sniped_tokens.txt", "r") as f:
        sniped_tokens = set(line.strip() for line in f)

# ========================= 🔁 Shared Log Handler =========================
async def handle_log(message, listener_name):
    global sniped_tokens

    try:
        data = json.loads(message)
        result = data.get("result")

        if not isinstance(result, dict):
            return

        log = result.get("value", {})
        accounts = log.get("accountKeys", [])
        if not isinstance(accounts, list):
            return

        timestamp = datetime.utcnow().strftime('%H:%M:%S')
        print(f"[{timestamp}] {listener_name} Raw log: {message}")

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
                if DEBUG:
                    print(f"[DEBUG] {token_mint} has no price, skipping")
                continue

            await send_telegram_alert(f"🚨 [{listener_name}] Attempting buy: {token_mint}")
            await buy_token(token_mint, BUY_AMOUNT_SOL)
            await auto_sell_if_profit(token_mint, entry_price)

    except Exception as e:
        print(f"[‼️] {listener_name} error: {e}")

# ========================= 🌐 Dual WebSocket Listeners =========================

async def mempool_listener_jupiter():
    last_heartbeat = datetime.utcnow()
    while True:
        try:
            async with websockets.connect(HELIUS_WS, ping_interval=30, ping_timeout=10) as ws:
                sub_msg = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "logsSubscribe",
                    "params": [
                        {"mentions": [JUPITER_PROGRAM]},
                        {"commitment": "processed", "encoding": "jsonParsed"}
                    ]
                }
                await ws.send(json.dumps(sub_msg))
                await send_telegram_alert("📡 JUPITER listener active...")
                if DEBUG:
                    print("[📡] Subscribed to Jupiter logs")

                while True:
                    now = datetime.utcnow()
                    if now - last_heartbeat >= heartbeat_interval:
                        await send_telegram_alert(f"❤️ JUPITER heartbeat @ {now.strftime('%H:%M:%S')} UTC")
                        last_heartbeat = now

                    try:
                        message = await asyncio.wait_for(ws.recv(), timeout=60)
                        await handle_log(message, "JUPITER")
                    except asyncio.TimeoutError:
                        print("[JUPITER] Timeout, pinging server...")
                        await ws.ping()
        except Exception as e:
            print(f"[‼️] JUPITER WS error: {e}")
            await asyncio.sleep(10)

async def mempool_listener_raydium():
    last_heartbeat = datetime.utcnow()
    while True:
        try:
            async with websockets.connect(HELIUS_WS, ping_interval=30, ping_timeout=10) as ws:
                sub_msg = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "logsSubscribe",
                    "params": [
                        {"mentions": [RAYDIUM_PROGRAM]},
                        {"commitment": "processed", "encoding": "jsonParsed"}
                    ]
                }
                await ws.send(json.dumps(sub_msg))
                await send_telegram_alert("📡 RAYDIUM listener active...")
                if DEBUG:
                    print("[📡] Subscribed to Raydium logs")

                while True:
                    now = datetime.utcnow()
                    if now - last_heartbeat >= heartbeat_interval:
                        await send_telegram_alert(f"❤️ RAYDIUM heartbeat @ {now.strftime('%H:%M:%S')} UTC")
                        last_heartbeat = now

                    try:
                        message = await asyncio.wait_for(ws.recv(), timeout=60)
                        await handle_log(message, "RAYDIUM")
                    except asyncio.TimeoutError:
                        print("[RAYDIUM] Timeout, pinging server...")
                        await ws.ping()
        except Exception as e:
            print(f"[‼️] RAYDIUM WS error: {e}")
            await asyncio.sleep(10)
