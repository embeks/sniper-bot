import os
import asyncio
import json
import websockets
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

# Raydium AMM Program ID
RAYDIUM_PROGRAM_ID = "RVKd61ztZW9BvU4wjf3GGN2TjK5uAAgnk99bQzVJ8zU"
BUY_AMOUNT_SOL = 0.027  # ≈ $5 AUD
sniped_tokens = set()

async def mempool_listener():
    helius_api_key = os.getenv("HELIUS_API_KEY")
    if not helius_api_key:
        print("[‼️] No Helius API Key found in environment.")
        return

    # 🔥 TEST LOGIC — Force buy 1 token before real listener
    test_token_mint = "2zMMhcVQEXDtdE6vsFS7S7D5oUodfJHE8vd1gnBouauv"
    await send_telegram_alert(f"🧪 Test mode: Forcing snipe of {test_token_mint}")
    
    entry_price = await get_token_price(test_token_mint)
    if not entry_price:
        await send_telegram_alert("❌ No price found for test token.")
        return

    await buy_token(test_token_mint, BUY_AMOUNT_SOL)
    await auto_sell_if_profit(test_token_mint, entry_price)
    await send_telegram_alert("✅ Test buy completed. Skipping mempool.")
    return  # stop here for test mode

    # 📡 Real mempool listener logic
    uri = f"wss://mainnet.helius-rpc.com/?api-key={helius_api_key}"

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
            await send_telegram_alert("📡 Mempool listener active...")

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

                                # 🧠 Run pre-buy filters
                                safety = await check_token_safety(token_mint)
                                if "❌" in safety or "⚠️" in safety:
                                    continue
                                if await has_blacklist_or_mint_functions(token_mint):
                                    continue
                                if not await is_lp_locked_or_burned(token_mint):
                                    continue

                                await send_telegram_alert(f"🔎 New token: {token_mint}\n{safety}\nAuto-sniping...")

                                entry_price = await get_token_price(token_mint)
                                if not entry_price:
                                    await send_telegram_alert("❌ No price found, skipping")
                                    continue

                                sniped_tokens.add(token_mint)
                                await buy_token(token_mint, BUY_AMOUNT_SOL)
                                await auto_sell_if_profit(token_mint, entry_price)

                except Exception as inner_e:
                    print(f"[!] Inner loop error: {inner_e}")
                    await asyncio.sleep(3)

    except Exception as outer_e:
        print(f"[‼️] Mempool listener startup failed: {outer_e}")
        await send_telegram_alert("‼️ Mempool listener crashed.")
