# mempool_listener.py
import os
import asyncio
import json
import websockets
from dotenv import load_dotenv
from solana.publickey import PublicKey
from utils import (
    send_telegram_alert,
    check_token_safety,
    has_blacklist_or_mint_functions,
    is_lp_locked_or_burned
)
from solana_sniper import buy_token, auto_sell_if_profit, get_token_price

load_dotenv()

# Raydium AMM Program ID
RAYDIUM_PROGRAM_ID = "RVKd61ztZW9BvU4wjf3GGN2TjK5uAAgnk99bQzVJ8zU"

# Minimum Liquidity in USD
MIN_LIQUIDITY = 2000
# Amount to buy in SOL (approx. $5 AUD)
BUY_AMOUNT_SOL = 0.027

# Track already-sniped tokens to prevent double buys
sniped_tokens = set()

# Main mempool listener loop
async def mempool_listener():
    helius_api_key = os.getenv("HELIUS_API_KEY")
    url = f"wss://mainnet.helius-rpc.com/?api-key={helius_api_key}"
    async with websockets.connect(url) as ws:
        sub_msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsSubscribe",
            "params": [
                {
                    "mentions": [RAYDIUM_PROGRAM_ID]
                },
                {
                    "commitment": "processed",
                    "encoding": "jsonParsed"
                }
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
                            try:
                                token_mint = str(acc)
                                if token_mint in sniped_tokens:
                                    continue
                                if token_mint.startswith("So111") or len(token_mint) != 44:
                                    continue

                                # ✅ Run filters
                                safety = await check_token_safety(token_mint)
                                if "❌" in safety or "⚠️" in safety:
                                    continue

                                if await has_blacklist_or_mint_functions(token_mint):
                                    continue

                                if not await is_lp_locked_or_burned(token_mint):
                                    continue

                                await send_telegram_alert(f"🔎 New token detected: {token_mint}\n{safety}\nAuto-sniping now...")

                                entry_price = await get_token_price(token_mint)
                                if not entry_price:
                                    await send_telegram_alert("❌ No price found, skipping")
                                    continue

                                sniped_tokens.add(token_mint)
                                await buy_token(token_mint, BUY_AMOUNT_SOL)
                                await auto_sell_if_profit(token_mint, entry_price, None)

                            except Exception as inner_e:
                                print(f"[!] Error inside account loop: {inner_e}")

            except Exception as e:
                print(f"[!] Mempool error: {e}")
                await asyncio.sleep(5)
