# =========================
# sniper_logic.py (Elite Debug Patched)
# =========================
import os
import json
import asyncio
import websockets
from datetime import datetime, timedelta
from dotenv import load_dotenv

from utils import (
    send_telegram_alert,
    get_token_price,
    is_safe_token,
    is_volume_spike,
    get_holder_delta,
    get_rpc_client
)
from jupiter_trade import buy_token
from trade_logic import auto_sell_if_profit

load_dotenv()

HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
BUY_AMOUNT_SOL = float(os.getenv("BUY_AMOUNT_SOL", 0.2))

HELIUS_WS = f"wss://mainnet.helius-rpc.com/v1/ws?api-key={HELIUS_API_KEY}"
JUPITER_PROGRAM = "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB"
RAYDIUM_PROGRAM = "RVKd61ztZW9GdKzvXxkzRhK21Z4LzStfgzj31EKXdYv"

sniped_tokens = set()
heartbeat_interval = timedelta(hours=4)

if os.path.exists("sniped_tokens.txt"):
    with open("sniped_tokens.txt", "r") as f:
        sniped_tokens = set(line.strip() for line in f)

# ========================= 🔁 Enhanced Log Handler (Debug Version) =========================
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

        detected = False

        for token_mint in accounts:
            if len(token_mint) == 44:
                detected = True
                await send_telegram_alert(f"👀 {listener_name} found token: `{token_mint}`")

            if len(token_mint) != 44 or token_mint.startswith("So111"):
                continue
            if token_mint in sniped_tokens:
                continue

            sniped_tokens.add(token_mint)
            with open("sniped_tokens.txt", "a") as f:
                f.write(f"{token_mint}\n")

            await send_telegram_alert(f"🟡 [{listener_name}] Detected new token mint: {token_mint}")

            # ✅ Safety Checks
            is_safe = await is_safe_token(token_mint)
            if not is_safe:
                await send_telegram_alert(f"⚠️ Token {token_mint} failed safety checks. Skipping...")
                return

            # 📈 Volume spike (optional)
            spike = await is_volume_spike(token_mint)
            if spike:
                await send_telegram_alert(f"📈 Volume spike detected for {token_mint}")

            # ⏳ Holder momentum
            holder_delta = await get_holder_delta(token_mint, delay=60)
            await send_telegram_alert(f"👥 Holder delta after 60s: {holder_delta}")

            # 🛒 Buy
            entry_price = await get_token_price(token_mint)
            if not entry_price:
                return

            await send_telegram_alert(f"🚨 [{listener_name}] Attempting buy: {token_mint}")
            await buy_token(token_mint, BUY_AMOUNT_SOL)
            await auto_sell_if_profit(token_mint, entry_price)

        if not detected:
            await send_telegram_alert(f"🔎 [{listener_name}] No 44-char token mints in log")

    except Exception as e:
        print(f"[‼️] {listener_name} error: {e}")

# ========================= 🌐 Listener =========================
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
                await send_telegram_alert(f"📡 {listener_name} listener active... ✅ Starting sniper bot with dual sockets (Jupiter + Raydium)...")
                print(f"[📡] Subscribed to {listener_name} logs")

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
            await asyncio.sleep(10)

# ========================= 🚀 Entry Point =========================
async def mempool_listener_jupiter():
    await listen_to_program(JUPITER_PROGRAM, "JUPITER")

async def mempool_listener_raydium():
    await listen_to_program(RAYDIUM_PROGRAM, "RAYDIUM")
