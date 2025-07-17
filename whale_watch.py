# =========================
# whale_watch.py
# =========================
import asyncio
import httpx
import json
import time

from utils import (
    is_blacklisted, is_lp_locked_or_burned,
    has_blacklist_or_mint_functions, check_token_safety,
    has_whales, send_telegram_alert
)
from sniper_logic import buy_token

BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")
WALLET_LIST = "watched_wallets.txt"
SNIPED_FILE = "sniped_tokens.txt"

watchlist = set()
sniped_tokens = set()

# ------------------------
# Load Wallets
# ------------------------
def load_watched_wallets():
    try:
        with open(WALLET_LIST, "r") as f:
            for line in f:
                watchlist.add(line.strip())
    except FileNotFoundError:
        print("[!] No watched wallets file found")

def load_sniped():
    try:
        with open(SNIPED_FILE, "r") as f:
            for line in f:
                sniped_tokens.add(line.strip())
    except FileNotFoundError:
        pass

def mark_sniped(mint):
    with open(SNIPED_FILE, "a") as f:
        f.write(mint + "\n")
    sniped_tokens.add(mint)

# ------------------------
# Watch Wallets
# ------------------------
async def track_wallet_activity():
    while True:
        try:
            async with httpx.AsyncClient() as session:
                for wallet in list(watchlist):
                    url = f"https://public-api.birdeye.so/public/wallet/{wallet}/tokens"
                    headers = {"X-API-KEY": BIRDEYE_API_KEY}
                    res = await session.get(url, headers=headers)
                    tokens = res.json().get("data", [])

                    for t in tokens:
                        mint = t.get("address")
                        if not mint or mint in sniped_tokens:
                            continue

                        # Pre-check filters
                        if await is_blacklisted(mint):
                            continue
                        if not await is_lp_locked_or_burned(mint):
                            continue
                        if await has_blacklist_or_mint_functions(mint):
                            continue
                        if await has_whales(mint):
                            continue
                        safety = await check_token_safety(mint)
                        if "❌" in safety:
                            continue

                        await send_telegram_alert(f"🐳 Whale buy detected by {wallet}\nSniping {mint}")
                        await buy_token(mint)
                        mark_sniped(mint)

            await asyncio.sleep(30)
        except Exception as e:
            print(f"[!] Whale watch error: {e}")
            await asyncio.sleep(10)

# ------------------------
# Start Function
# ------------------------

async def start_whale_watch():
    load_watched_wallets()
    load_sniped()
    await send_telegram_alert("📡 Whale Watch online...")
    await track_wallet_activity()
