import asyncio
import json
import websockets

from utils import (
    send_telegram_alert,
    check_token_safety,
    has_blacklist_or_mint_functions,
    is_lp_locked_or_burned,
    get_token_price,
)
from sniper import buy_token
from trade_logic import auto_sell_if_profit

sniped_tokens = set()

async def mempool_listener():
    uri = "wss://mainnet.helius-rpc.com/?api-key=YOUR_KEY"
    async with websockets.connect(uri) as ws:
        sub_msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsSubscribe",
            "params": [
                {
                    "mentions": [],
                    "filter": {
                        "mentions": [],
                        "programId": "5quBvUMpFwVkJUzfRkRQ1vYXbLZ6yUJvH1ViZ8qxp9wW"  # Jupiter
                    }
                },
                {"encoding": "jsonParsed"}
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
                            # === FORCE BONK SNIPE TEST ===
                            token_mint = "DezX1x5C6AvPSLqR7EDHkPZgRbWvBvGJx5JXqt6Zt9V9"
                            if token_mint in sniped_tokens:
                                continue

                            # 🧠 Run filters
                            safety = await check_token_safety(token_mint)
                            if "❌" in safety or "⚠️" in safety:
                                continue
                            if await has_blacklist_or_mint_functions(token_mint):
                                continue
                            if not await is_lp_locked_or_burned(token_mint):
                                continue

                            await send_telegram_alert(f"🆕 New token: {token_mint}\n{safety}\nAuto-sniping...")

                            entry_price = await get_token_price(token_mint)
                            if not entry_price:
                                continue

                            await buy_token(token_mint)
                            sniped_tokens.add(token_mint)
                            await asyncio.sleep(1)
                            await auto_sell_if_profit(token_mint, entry_price)

            except Exception as e:
                await send_telegram_alert(f"[‼️] Mempool listener error: {e}")
