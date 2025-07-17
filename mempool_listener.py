import asyncio
import json
import os
import websockets
from dotenv import load_dotenv
from utils import send_telegram_alert, is_token_already_sniped, mark_token_as_sniped

load_dotenv()

HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
SOLANA_MEMPOOL_WS = "wss://mainnet.helius-rpc.com/v1/ws"
RAYDIUM_PROGRAM = "RVKd61ztZW9k39uZSBz2ZLxgGZ5VZz5tqg8dCj1djzj"
JUPITER_PROGRAM = "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB"

async def subscribe(websocket, program_id):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "logsSubscribe",
        "params": [
            {"mentions": [program_id]},
            {"commitment": "processed"}
        ]
    }
    await websocket.send(json.dumps(payload))
    print(f"Subscribed to logs for {program_id}")

async def handle_message(message):
    data = json.loads(message)
    if "result" in data and "value" in data["result"]:
        log = data["result"]["value"]
        logs = log.get("logs", [])
        for entry in logs:
            if "InitializeMint" in entry or "initialize_mint" in entry:
                token = log["account"]
                if is_token_already_sniped(token):
                    return
                mark_token_as_sniped(token)
                await send_telegram_alert(f"🚀 NEW TOKEN DETECTED: {token}")
                print(f"New token detected: {token}")

async def connect_and_listen(program_id):
    uri = SOLANA_MEMPOOL_WS
    headers = {"Authorization": f"Bearer {HELIUS_API_KEY}"}

    while True:
        try:
            async with websockets.connect(uri, extra_headers=headers) as websocket:
                await subscribe(websocket, program_id)
                async for message in websocket:
                    await handle_message(message)
        except websockets.exceptions.ConnectionClosedError as e:
            print(f"[{program_id}] Connection closed: {e}. Reconnecting in 3s...")
            await asyncio.sleep(3)
        except Exception as e:
            print(f"[{program_id}] Unexpected error: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)

async def main():
    await asyncio.gather(
        connect_and_listen(RAYDIUM_PROGRAM),
        connect_and_listen(JUPITER_PROGRAM)
    )

if __name__ == "__main__":
    asyncio.run(main())
