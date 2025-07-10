import os
import time
import json
import base64
import requests
from dotenv import load_dotenv

# Load .env variables
load_dotenv()

BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Filters
MIN_LIQUIDITY = 20000
MIN_HOLDERS = 20
MAX_HOLDERS = 300

# 🐋 Whale wallet list loader
def load_wallets_to_follow(filename="wallets_to_follow.txt"):
    try:
        with open(filename, "r") as f:
            wallets = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        print(f"[+] Loaded {len(wallets)} wallets to follow.")
        return wallets
    except Exception as e:
        print(f"[!] Error loading wallet list: {e}")
        return []

# 📡 New token feed filter
def check_new_tokens():
    try:
        url = "https://public-api.birdeye.so/public/tokenlist?sort_by=txns24h&sort_type=desc&limit=15"
        headers = {"X-API-KEY": BIRDEYE_API_KEY}
        response = requests.get(url, headers=headers)
        data = response.json()
    except Exception as e:
        print(f"[!] Birdeye error: {e}")
        return []

    new_tokens = []
    for token in data.get("data", []):
        name = token.get("name", "").lower()
        address = token.get("address")
        liquidity = token.get("liquidity", 0)
        holders = token.get("holders", 0)
        txns_5m = token.get("txns5m", 0)
        chain = token.get("chain", "unknown")

        print(f"🔍 {name.upper()} | LP: {liquidity} | Holders: {holders} | 5m TXNs: {txns_5m}")

        if chain != "solana": continue
        if txns_5m > 100 and liquidity < 1000: continue
        if not (liquidity >= MIN_LIQUIDITY and MIN_HOLDERS <= holders <= MAX_HOLDERS): continue

        if not is_contract_verified(address):
            print(f"[⛔] Skipped: Unverified contract — {name.upper()}")
            continue
        if has_blacklist_or_mint_functions(address):
            print(f"[⛔] Skipped: Suspicious bytecode — {name.upper()}")
            continue
        if not is_lp_locked_or_burned(address):
            print(f"[⛔] Skipped: LP not locked or burned — {name.upper()}")
            continue

        new_tokens.append(address)
    return new_tokens

# 🐋 Wallet Activity Tracker
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
                msg = f"🐋 Whale Buy Alert\nWallet: {buyer}\nToken: {token}\nAddress: {token_address}"
                send_telegram_alert(msg)

    except Exception as e:
        print(f"[!] Wallet check failed: {e}")

# 🔍 Bytecode safety filter
def is_contract_verified(token_address):
    try:
        url = f"https://public-api.birdeye.so/public/token/{token_address}"
        headers = {"X-API-KEY": BIRDEYE_API_KEY}
        res = requests.get(url, headers=headers)
        return res.json().get("data", {}).get("is_verified", False)
    except Exception as e:
        print(f"[!] Contract verification failed: {e}")
        return False

def has_blacklist_or_mint_functions(token_address):
    try:
        url = f"https://public-api.birdeye.so/public/token/{token_address}"
        headers = {"X-API-KEY": BIRDEYE_API_KEY}
        res = requests.get(url, headers=headers)
        bytecode = res.json().get("data", {}).get("bytecode", "")

        decoded = base64.b64decode(bytecode.encode()).decode(errors="ignore")
        flags = ["blacklist", "mint", "pause", "setAdmin", "setBlacklist"]
        return any(flag in decoded for flag in flags)
    except Exception as e:
        print(f"[!] Bytecode scan failed: {e}")
        return False

def is_lp_locked_or_burned(token_address):
    try:
        url = f"https://public-api.birdeye.so/public/token/{token_address}/lp"
        headers = {"X-API-KEY": BIRDEYE_API_KEY}
        res = requests.get(url, headers=headers)
        lp = res.json().get("data", {})
        return lp.get("locked", 0) > 0 or lp.get("burned", 0) > 0
    except Exception as e:
        print(f"[!] LP lock check failed: {e}")
        return False

# 📲 Telegram alert sender
def send_telegram_alert(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print(f"[!] Telegram error: {e}")
