import os
import json
import asyncio
import websockets
import requests
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SOLANA_MEMPOOL_WS = os.getenv("SOLANA_MEMPOOL_WS")

RAYDIUM_PROGRAM = "4F5eMW7faAaLfsn5jXDzUsXXvyrRvwBxAV6jDFitZZGX"
JUPITER_PROGRAM = "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB"

def send_telegram_alert(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print(f"[!] Telegram error: {e}")

async def listen_mempool():
    async with websockets.connect(SOLANA_MEMPOOL_WS) as ws:
        await ws.send(json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [RAYDIUM_PROGRAM, JUPITER_PROGRAM]},
                {"commitment": "confirmed"}
            ]
        }))
        print("🟢 Mempool listener active...")
        while True:
            try:
                response = await ws.recv()
                data = json.loads(response)
                log_info = data.get("params", {}).get("result", {})
                signature = log_info.get("signature", "N/A")
                msg = (
                    f"🔔 Mempool Event Detected!\n"
                    f"Program: Raydium/Jupiter\n"
                    f"Tx Signature: {signature}"
                )
                send_telegram_alert(msg)
                print(f"[+] Alert sent for: {signature}")
            except Exception as e:
                print(f"[!] Mempool error: {e}")
                await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(listen_mempool())
