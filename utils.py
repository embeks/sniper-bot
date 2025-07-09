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
    try:
        url = "https://public-api.birdeye.so/public/tokenlist?sort_by=txns24h&sort_type=desc&limit=15"
        headers = {"X-API-KEY": BIRDEYE_API_KEY}
        response = requests.get(url, headers=headers)
        data = response.json()
    except Exception as e:
        print(f"[!] Birdeye error: {e}")
        return

    for token in data.get("data", []):
        name = token.get("name", "").lower()
        address = token.get("address")
        liquidity = token.get("liquidity", 0)
        holders = token.get("holders", 0)
        price = token.get("price", 0)
        txns_5m = token.get("txns5m", 0)
        chain = token.get("chain", "unknown")

        print(f"🔍 Scanning: {name.upper()} | Price: {price} | LP: {liquidity} | Holders: {holders} | 5m TXNs: {txns_5m}")

        # 🎯 DEGE WATCHLIST
        if name == "dege":
            for label, target in dege_targets.items():
                if price >= target["price"] and not target["hit"]:
                    send_telegram_alert(f"🎯 DEGE HIT {label}!\nPrice: ${price:.5f}")
                    dege_targets[label]["hit"] = True

        if chain != "solana":
            continue
        if txns_5m > 100 and liquidity < 1000:
            continue
        if not (liquidity >= MIN_LIQUIDITY and MIN_HOLDERS <= holders <= MAX_HOLDERS):
            continue

        # 🔒 Protection Checks
        if not is_token_verified(address):
            print(f"[⛔] Skipped: Contract not verified - {name.upper()}")
            continue
        if has_blacklist_or_mint_functions(address):
            print(f"[⛔] Skipped: Suspicious bytecode - {name.upper()}")
            continue
        if not is_lp_locked_or_burned(address):
            print(f"[⛔] Skipped: LP not locked or burned - {name.upper()}")
            continue

        # ✅ Passed filters — Alert!
       msg = (
    f"✅ Passed Filters — New SOLANA Token Detected\n\n"
    f"🪙 Name: {name.upper()}\n"
    f"💧 Liquidity: ${liquidity:,.0f}\n"
    f"👥 Holders: {holders}\n"
    f"📬 Address: {address}"
)
        send_telegram_alert(msg)
# 🚀 Add to the bottom of your utils.py file

import json

# 🔍 Load wallets to follow from wallets_to_follow.txt
def load_wallets_to_follow(filename="wallets_to_follow.txt"):
    try:
        with open(filename, "r") as f:
            wallets = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        print(f"[+] Loaded {len(wallets)} wallets to follow.")
        return wallets
    except Exception as e:
        print(f"[!] Error loading wallet list: {e}")
        return []
        
# 🔍 Scan recent transactions and alert if any tracked wallet buys a new token
def check_wallet_activity(wallets_to_follow):
    try:
        url = "https://public-api.birdeye.so/public/txs/recent?limit=50"
        headers = {"X-API-KEY": BIRDEYE_API_KEY}
        res = requests.get(url, headers=headers)
        res.raise_for_status()
        txs = res.json().get("data", [])

        for tx in txs:
            buyer = tx.get("signer")
            token = tx.get("token_symbol")
            token_address = tx.get("token_address")

            if buyer and buyer.lower() in wallets_to_follow:
                msg = (
                    f"🐋 Wallet Buy Detected\n\n"
                    f"Wallet: {buyer}\nToken: {token}\nToken Address: {token_address}"
                )
                send_telegram_alert(msg)
                print(f"[ALERT] Whale Buy: {buyer} -> {token}")

    except Exception as e:
        print(f"[!] Wallet activity check failed: {e}")
# utils.py (add this at the bottom of the file)

import base64
import json

# 🚨 RUG PROTECTION - Basic contract safety checks

def is_contract_verified(token_address):
    try:
        url = f"https://public-api.birdeye.so/public/token/{token_address}"
        headers = {"X-API-KEY": BIRDEYE_API_KEY}
        res = requests.get(url, headers=headers)
        data = res.json().get("data", {})
        verified = data.get("is_verified", False)
        return verified
    except Exception as e:
        print(f"[!] Contract verification check failed: {e}")
        return False

def has_blacklist_or_mint_functions(token_address):
    try:
        url = f"https://public-api.birdeye.so/public/token/{token_address}"
        headers = {"X-API-KEY": BIRDEYE_API_KEY}
        res = requests.get(url, headers=headers)
        bytecode = res.json().get("data", {}).get("bytecode", "")

        decoded = base64.b64decode(bytecode.encode()).decode(errors="ignore")
        flags = ["blacklist", "mint", "pause", "setAdmin", "setBlacklist"]
        for flag in flags:
            if flag in decoded:
                print(f"[!] Flagged function detected in bytecode: {flag}")
                return True
        return False
    except Exception as e:
        print(f"[!] Bytecode flag check failed: {e}")
        return False
def is_lp_locked_or_burned(token_address):
    try:
        url = f"https://public-api.birdeye.so/public/token/{token_address}/lp"
        headers = {"X-API-KEY": BIRDEYE_API_KEY}
        res = requests.get(url, headers=headers)
        lp_data = res.json().get("data", {})
        locked = lp_data.get("locked", 0)
        burned = lp_data.get("burned", 0)
        if locked > 0 or burned > 0:
            return True
        return False
    except Exception as e:
        print(f"[!] LP lock check failed: {e}")
        return False
