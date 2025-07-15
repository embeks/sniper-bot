import os
import asyncio
import json
import websockets
from datetime import datetime, timedelta
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

# === CONFIG ===
DEBUG = True
PROGRAM_IDS = [
    "RVKd61ztZW9BvU4wjf3GGN2TjK5uAAgnk99bQzVJ8zU",  # Raydium
    "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB",  # Jupiter
    "82yxjeMsxhMF2j5BuWvFo5YzRrFdje4rj58k5DFhGcFh"   # Orca
]
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
                        {"mentions": PROGRAM_IDS},
                        {"commitment": "processed", "encoding": "jsonParsed"}
                    ]
                }
                await ws.send(json.dumps(sub_msg))

                if not mempool_announced:
                    await send_telegram_alert("📡 Mempool listener active (RAY+JUP+ORCA)...")
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

                        if DEBUG:
                            print("[DEBUG] Raw message:", data)

                        result = data.get("result")
                        if not isinstance(result, dict):
                            if DEBUG:
                                print("[DEBUG] Skipped: result not dict")
                            continue

                        log = result.get("value")
                        if not isinstance(log, dict):
                            if DEBUG:
                                print("[DEBUG] Skipped: value not dict")
                            continue

                        accounts = log.get("accountKeys")
                        if not isinstance(accounts, list):
                            if DEBUG:
                                print("[DEBUG] Skipped: accountKeys not list")
                            continue

                        for acc in accounts:
                            token_mint = str(acc)

                            if DEBUG:
                                await send_telegram_alert(f"[DEBUG] Token seen: {token_mint}")

                            if token_mint in sniped_tokens:
                                if DEBUG:
                                    await send_telegram_alert(f"[DEBUG] Skipped {token_mint}: already sniped")
                                continue
                            if token_mint.startswith("So111"):
                                if DEBUG:
                                    await send_telegram_alert(f"[DEBUG] Skipped {token_mint}: native SOL")
                                continue
                            if len(token_mint) != 44:
                                if DEBUG:
                                    await send_telegram_alert(f"[DEBUG] Skipped {token_mint}: invalid length")
                                continue

                            safety = await check_token_safety(token_mint)
                            if isinstance(safety, str):
                                if "❌ Rug Risk" in safety:
                                    if DEBUG:
                                        await send_telegram_alert(f"[DEBUG] Skipped {token_mint}: {safety}")
                                    continue
                                if "⚠️ Honeypot Risk" in safety:
                                    try:
                                        tax_str = safety.split("(")[1].split(")")[0]
                                        buy_tax, sell_tax = [int(x.strip().replace("%", "")) for x in tax_str.split("/")]
                                        if buy_tax > 20 or sell_tax > 20:
                                            if DEBUG:
                                                await send_telegram_alert(f"[DEBUG] Skipped {token_mint}: {safety}")
                                            continue
                                    except:
                                        pass
                                if "⚠️ Low Holders" in safety:
                                    if DEBUG:
                                        await send_telegram_alert(f"[DEBUG] Skipped {token_mint}: {safety}")
                                    continue

                            if await has_blacklist_or_mint_functions(token_mint):
                                if DEBUG:
                                    await send_telegram_alert(f"[DEBUG] Skipped {token_mint}: blacklist or mint authority present")
                                continue

                            # LP check skipped in relaxed mode — uncomment if needed
                            # if not await is_lp_locked_or_burned(token_mint):
                            #     if DEBUG:
                            #         await send_telegram_alert(f"[DEBUG] Skipped {token_mint}: LP not locked or burned")
                            #     continue

                            await send_telegram_alert(f"🔎 New token: {token_mint}\n{safety}\nAuto-sniping...")

                            entry_price = await get_token_price(token_mint)
                            if not entry_price:
                                await send_telegram_alert(f"❌ {token_mint}: No price found, skipping")
                                continue

                            sniped_tokens.add(token_mint)
                            await buy_token(token_mint, BUY_AMOUNT_SOL)
                            await auto_sell_if_profit(token_mint, entry_price)

                    except Exception as inner_e:
                        error_time = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                        print(f"[{error_time}] [!] Inner loop error: {inner_e}")
                        await asyncio.sleep(2)
                        break

        except Exception as outer_e:
            error_time = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{error_time}] [‼️] Mempool connection failed: {outer_e}")
            mempool_announced = False
            await asyncio.sleep(5)
