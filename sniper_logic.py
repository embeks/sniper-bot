import json
import os
from datetime import datetime, timedelta
from utils import send_telegram_alert, TOKEN_PROGRAM_ID, is_valid_mint, snipe_token

sniped_tokens = set()
heartbeat_interval = timedelta(hours=4)
last_heartbeat = datetime.utcnow()

# Load sniped tokens
if os.path.exists("sniped_tokens.txt"):
    with open("sniped_tokens.txt", "r") as f:
        sniped_tokens = set(line.strip() for line in f)

async def handle_log(message, listener_name):
    global sniped_tokens, last_heartbeat
    try:
        data = json.loads(message)
        result = data.get("result")
        if not isinstance(result, dict):
            return

        log = result.get("value", {})
        accounts = log.get("accountKeys", [])
        if not isinstance(accounts, list):
            return

        # Print accounts to Render logs for dev visibility
        print(f"[{listener_name}] Scanning log with accounts: {accounts}")

        # Check if log contains a Token Program ID (usually at index 0–3)
        if TOKEN_PROGRAM_ID not in accounts:
            return

        # Detect mint address
        possible_mints = [acc for acc in accounts if acc != TOKEN_PROGRAM_ID and acc not in sniped_tokens]

        for mint in possible_mints:
            if mint in sniped_tokens:
                continue

            # Check if valid mint
            is_mint = await is_valid_mint(mint)
            if is_mint:
                sniped_tokens.add(mint)
                with open("sniped_tokens.txt", "a") as f:
                    f.write(mint + "\n")

                await send_telegram_alert(f"🎯 Valid mint detected: `{mint}`\nSniping now...")
                await snipe_token(mint)
            else:
                print(f"❌ Not a valid mint: {mint}")

        # Send heartbeat every 4 hours
        now = datetime.utcnow()
        if now - last_heartbeat > heartbeat_interval:
            await send_telegram_alert(f"❤️ {listener_name} heartbeat @ {now.strftime('%H:%M:%S UTC')}")
            last_heartbeat = now

    except Exception as e:
        print(f"⚠️ Error in handle_log: {e}")
