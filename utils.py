import os
import json
import httpx
from dotenv import load_dotenv

load_dotenv()

# 🔧 Config
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")

# 📤 Send Telegram Alert (Async)
async def send_telegram_alert(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[⚠️] Telegram config not set — skipping alert.")
        return
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": message}
            )
    except Exception as e:
        print(f"[!] Telegram alert failed: {e}")

# 🧠 Simulated Sell (Still sync placeholder)
def simulate_sell_transaction(token_address):
    try:
        return True  # Placeholder logic
    except Exception as e:
        print(f"[!] Sell simulation failed: {e}")
        return False

# 🔍 Honeypot Check via Birdeye
async def check_token_safety(token_address):
    try:
        async with httpx.AsyncClient() as client:
            url = f"https://public-api.birdeye.so/public/token/{token_address}/info"
            headers = {"X-API-KEY": BIRDEYE_API_KEY}
            res = await client.get(url, headers=headers)
            data = res.json().get("data", {})

            liquidity = data.get("liquidity", 0)
            buy_tax = data.get("buyTax", 0)
            sell_tax = data.get("sellTax", 0)
            holders = data.get("holders", 0)

            if liquidity < 10000:
                return "❌ Rug Risk: Low Liquidity"
            if buy_tax > 15 or sell_tax > 15:
                return f"⚠️ Possible Honeypot: Buy/Sell Tax too high ({buy_tax}% / {sell_tax}%)"
            if holders < 20:
                return "⚠️ Low Holders: Possibly Inactive"

            return "✅ Token passed basic safety checks"
    except Exception as e:
        return f"[!] Error checking honeypot: {e}"

# 🚫 Blacklist & Mint Check
async def has_blacklist_or_mint_functions(token_address):
    try:
        async with httpx.AsyncClient() as client:
            url = f"https://public-api.birdeye.so/public/token/{token_address}/info"
            headers = {"X-API-KEY": BIRDEYE_API_KEY}
            res = await client.get(url, headers=headers)
            data = res.json().get("data", {})
            blacklist = data.get("hasBlacklist", False)
            mint = data.get("hasMintAuthority", True)
            return blacklist or mint
    except Exception:
        return True  # Assume unsafe if can't verify

# 🔒 LP Locked or Burned Check
async def is_lp_locked_or_burned(token_address):
    try:
        async with httpx.AsyncClient() as client:
            url = f"https://public-api.birdeye.so/public/token/{token_address}/info"
            headers = {"X-API-KEY": BIRDEYE_API_KEY}
            res = await client.get(url, headers=headers)
            data = res.json().get("data", {})
            return data.get("lpIsBurned", False) or data.get("lpLocked", False)
    except Exception:
        return False

# 🚨 Smart Rug Trigger Logic
def detect_rug_conditions(token_data):
    try:
        if token_data["liquidity"] < 1000:
            return True
        if token_data["volume24h"] < 100:
            return True
        if token_data["sellTax"] > 25:
            return True
        return False
    except Exception:
        return False
