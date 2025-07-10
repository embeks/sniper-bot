import os
import requests
from dotenv import load_dotenv
from solana.publickey import PublicKey
from solana.rpc.api import Client
from solana.rpc.types import MemcmpOpts, TokenAccountOpts
from solana.transaction import Transaction
from solana.system_program import TransferParams, transfer
from solana.rpc.commitment import Confirmed
from solana.keypair import Keypair

load_dotenv()

# 🔧 Telegram config
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# 🔧 Birdeye config
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")

# 🔧 Solana RPC
client = Client("https://api.mainnet-beta.solana.com")


# 📤 Telegram alert
def send_telegram_alert(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[⚠️] Telegram config not set — skipping alert.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message})
    except Exception as e:
        print(f"[!] Telegram alert failed: {e}")


# 🧠 Simulated sell to detect honeypots
def simulate_sell_transaction(token_address):
    try:
        # Add real simulation logic if needed (for now we assume it's sellable)
        return True
    except Exception as e:
        print(f"[!] Sell simulation failed: {e}")
        return False


# 🔍 Token Safety Checker (from honeypot_checker.py)
def check_token_safety(token_address):
    url = f"https://public-api.birdeye.so/public/token/{token_address}/info"
    headers = {"X-API-KEY": BIRDEYE_API_KEY}

    try:
        res = requests.get(url, headers=headers)
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


# 🚫 Check for blacklist or mint functions
def has_blacklist_or_mint_functions(token_address):
    try:
        url = f"https://public-api.birdeye.so/public/token/{token_address}/info"
        headers = {"X-API-KEY": BIRDEYE_API_KEY}
        res = requests.get(url, headers=headers)
        data = res.json().get("data", {})
        blacklist = data.get("hasBlacklist", False)
        mint = data.get("hasMintAuthority", True)
        return blacklist or mint
    except Exception:
        return True  # Assume dangerous by default


# 🔒 Check if LP is locked or burned
def is_lp_locked_or_burned(token_address):
    try:
        url = f"https://public-api.birdeye.so/public/token/{token_address}/info"
        headers = {"X-API-KEY": BIRDEYE_API_KEY}
        res = requests.get(url, headers=headers)
        data = res.json().get("data", {})
        burned = data.get("lpIsBurned", False)
        locked = data.get("lpLocked", False)
        return burned or locked
    except Exception:
        return False


# 🛡️ Smart Rug Trigger Logic
def detect_rug_conditions(token_data):
    try:
        if token_data["liquidity"] < 1000:
            return True
        if token_data["volume24h"] < 100:
            return True
        if token_data["sellTax"] > 25:
            return True
        return False
    except:
        return False
