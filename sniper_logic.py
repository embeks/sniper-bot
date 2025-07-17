# =========================
# sniper_logic.py (FINAL VERSION)
# =========================
import asyncio
import json
import websockets
import os
from dotenv import load_dotenv

from utils import send_telegram_alert
from jupiter_trade import buy_token

# ✅ Load env variables
load_dotenv()
WS_URL = os.getenv("SOLANA_MEMPOOL_WS")

# ✅ Raydium + Jupiter program IDs
JUPITER_PROGRAM_ID = "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB"
RAYDIUM_PROGRAM_ID = "RVKd61ztZW9DQzGwVZvzESLZDJrdP9mYDTu7EieiqfF"

# ✅ Local file to track previously sniped tokens
SNIPED_TOKENS_FILE = "sniped_tokens.txt"
sniped_tokens = set()

def load_sniped_tokens():
    try:
        with open(SNIPED_TOKENS_FILE, "r") as f:
            for line in f:
                sniped_tokens.add(line.strip())
    except FileNotFoundError:
        pass

def mark_token_sniped(mint: str):
    if mint not in sniped_tokens:
        with open(SNIPED_TOKENS_FILE, "a") as f:
            f.write(mint + "\n")
        sniped_tokens.add(mint)

# 🔁 Core log listener
async def listen_to_program(program_id: str):
    async with websockets.connect(WS_URL) as ws:
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
        await send_telegram_alert(f"🔌 Listening to mempool logs from {program_id}")

        while True:
            try:
                raw = await ws.recv()
                data = json.loads(raw)
                logs = data.get("params", {}).get("result", {}).get("value", {})
                tx_log = logs.get("logMessages", [])

                # Scan log messages for mint addresses
                for line in tx_log:
                    if "mint" in line.lower() and "address" in line.lower():
                        parts = line.split()
                        for part in parts:
                            if len(part) == 44 and not part.startswith("So"):
                                mint_address = part
                                if mint_address in sniped_tokens:
                                    return
                                mark_token_sniped(mint_address)
                                await send_telegram_alert(f"🚀 Detected token: {mint_address}")
                                await buy_token(mint_address)
                                return
            except Exception as e:
                print(f"[‼️] Listener error: {e}")
                await asyncio.sleep(2)

# 🔁 Exported async tasks
async def mempool_listener_jupiter():
    await listen_to_program(JUPITER_PROGRAM_ID)

async def mempool_listener_raydium():
    await listen_to_program(RAYDIUM_PROGRAM_ID)

# ✅ Load sniped tokens on startup
load_sniped_tokens()
