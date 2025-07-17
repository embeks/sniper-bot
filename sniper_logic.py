# =========================
# mempool_listener.py
# =========================
import asyncio
import json
import websockets
from utils import send_telegram_alert
from jupiter_trade import buy_token
# Replace with actual Jupiter and Raydium program IDs
JUPITER_PROGRAM_ID = "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB"
RAYDIUM_PROGRAM_ID = "RVKd61ztZW9DQzGwVZvzESLZDJrdP9mYDTu7EieiqfF"

sniped_tokens_file = "sniped_tokens.txt"

# ------------------------
# Internal Tracker
# ------------------------
sniped_tokens = set()

def load_sniped_tokens():
    try:
        with open(sniped_tokens_file, "r") as f:
            for line in f:
                sniped_tokens.add(line.strip())
    except FileNotFoundError:
        pass

def mark_token_sniped(mint):
    if mint not in sniped_tokens:
        with open(sniped_tokens_file, "a") as f:
            f.write(mint + "\n")
        sniped_tokens.add(mint)

# ------------------------
# Core Listener
# ------------------------

async def listen_to_program(program_id: str):
    url = "wss://rpc.helius.xyz/?api-key=YOUR_API_KEY"
    async with websockets.connect(url) as ws:
        sub = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [program_id]},
                {"commitment": "processed", "encoding": "jsonParsed"}
            ]
        }
        await ws.send(json.dumps(sub))
        await send_telegram_alert(f"🔌 Listening to {program_id} mempool")

        while True:
            try:
                raw = await ws.recv()
                data = json.loads(raw)
                logs = data.get("params", {}).get("result", {}).get("value", {})
                tx_log = logs.get("logMessages", [])

                # Scan for mint address
                for msg in tx_log:
                    if "initialize_mint" in msg.lower():
                        for line in tx_log:
                            if "mint" in line and "address" in line:
                                parts = line.split()
                                for part in parts:
                                    if len(part) == 44 and part.startswith("So") == False:
                                        mint_address = part
                                        if mint_address in sniped_tokens:
                                            return
                                        mark_token_sniped(mint_address)
                                        await send_telegram_alert(f"🚀 Token detected: {mint_address}")
                                        await buy_token(mint_address)
            except Exception as e:
                print(f"[!] WS error: {e}")
                await asyncio.sleep(2)
                continue

# ------------------------
# Entry Points
# ------------------------

async def mempool_listener_jupiter():
    await listen_to_program(JUPITER_PROGRAM_ID)

async def mempool_listener_raydium():
    await listen_to_program(RAYDIUM_PROGRAM_ID)

load_sniped_tokens()
