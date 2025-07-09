### 🔹 utils.py
import os
import time
import requests
from dotenv import load_dotenv

# Load secrets from .env
load_dotenv()

BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Config
MIN_LIQUIDITY = 20000
MIN_HOLDERS = 20
MAX_HOLDERS = 300
CHECK_INTERVAL = 60

# Target tracker
dege_targets = {
    "10x": {"price": 0.002, "hit": False},
    "50x": {"price": 0.01,  "hit": False},
    "100x": {"price": 0.02,  "hit": False},
    "300x": {"price": 0.06,  "hit": False}
}

def send_telegram_alert(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print(f"[!] Telegram error: {e}")

def check_new_tokens():
    url = "https://public-api.birdeye.so/public/tokenlist?sort_by=txns24h&sort_type=desc&limit=15"
    headers = {"X-API-KEY": BIRDEYE_API_KEY}
    try:
        response = requests.get(url, headers=headers)
        tokens = response.json().get("data", [])
    except Exception as e:
        print(f"[!] Birdeye error: {e}")
        return

    for token in tokens:
        name = token.get("name", "").lower()
        address = token.get("address")
        liquidity = token.get("liquidity", 0)
        holders = token.get("holders", 0)
        price = token.get("price", 0)
        txns_5m = token.get("txns5m", 0)
        chain = token.get("chain", "unknown")

        print(f"Scanning: {name} | Chain: {chain} | Price: {price} | LP: {liquidity} | Holders: {holders} | 5m TXNs: {txns_5m}")

        # DEGE x targets
        if name == "dege":
            for label, target in dege_targets.items():
                if price >= target["price"] and not target["hit"]:
                    send_telegram_alert(f"🔥 DEGE HIT {label.upper()}\nPrice: ${price:.5f}")
                    target["hit"] = True

        if chain != "solana":
            continue

        if txns_5m > 100 and liquidity < 1000:
            continue

        if liquidity >= MIN_LIQUIDITY and MIN_HOLDERS <= holders <= MAX_HOLDERS:
            msg = (
                f"🆕 NEW SOLANA TOKEN DETECTED\n\n"
                f"Name: {name.upper()}\nLiquidity: ${liquidity:,.0f}\nHolders: {holders}"
            )
            send_telegram_alert(msg)
