# =========================
# utils.py (Final with Advanced Logic)
# =========================
import os
import json
import csv
import requests
from datetime import datetime
from dotenv import load_dotenv
from solana.rpc.api import Client
from solders.keypair import Keypair
from telegram import Bot

# ============ 🔧 Load Environment ============
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
RPC_URL = os.getenv("RPC_URL")
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")

bot = Bot(token=TELEGRAM_TOKEN)

# ============ 📩 Telegram Alert ============
def send_telegram_alert(message):
    try:
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
    except Exception as e:
        print(f"[‼️] Telegram send error: {e}")

# ============ 🔌 Client ============
def get_rpc_client():
    return Client(RPC_URL)

# ============ 💹 Get Price ============
def get_token_price(mint):
    try:
        url = f"https://public-api.birdeye.so/public/price?address={mint}"
        headers = {"X-API-KEY": BIRDEYE_API_KEY}
        res = requests.get(url, headers=headers)
        data = res.json()
        return data['data']['value'] if data.get('data') else None
    except:
        return None

# ============ 📊 Token Data (LP, Holders, etc.) ============
def get_token_data(mint):
    try:
        url = f"https://public-api.birdeye.so/public/token/{mint}/metrics"
        headers = {"X-API-KEY": BIRDEYE_API_KEY}
        res = requests.get(url, headers=headers)
        return res.json().get("data", {})
    except:
        return {}

# ============ 🧠 Holder Count ============
def get_holder_count(mint):
    try:
        url = f"https://public-api.birdeye.so/public/token/{mint}/holders"
        headers = {"X-API-KEY": BIRDEYE_API_KEY}
        res = requests.get(url, headers=headers)
        return res.json().get("data", {}).get("holders", 0)
    except:
        return 0

# ============ 📥 Token Balance ============
def get_token_balance(mint):
    try:
        client = get_rpc_client()
        accs = client.get_token_accounts_by_owner(os.getenv("WALLET_ADDRESS"), {"mint": mint})
        if accs and accs['result']['value']:
            amt = accs['result']['value'][0]['account']['data']['parsed']['info']['tokenAmount']['amount']
            return int(amt)
        return 0
    except:
        return 0

# ============ 📈 Log Trades to CSV ============
def log_trade_to_csv(token, side, size_in, size_out):
    try:
        with open("trade_log.csv", "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([datetime.now().isoformat(), token, side, size_in, size_out])
    except Exception as e:
        print(f"[‼️] CSV Logging Error: {e}")

# ============ ✅ Filters ============
def is_blacklisted(mint):
    blacklist = ["9n4nbM75f5Ui33ZbPYXn59EwSgE8CGsHtAeTH5YFeJ9E"]
    return mint in blacklist

def has_locked_lp(mint):
    try:
        url = f"https://public-api.birdeye.so/public/token/{mint}/pool"
        headers = {"X-API-KEY": BIRDEYE_API_KEY}
        res = requests.get(url, headers=headers)
        lock = res.json().get("data", {}).get("lp_locked")
        return lock and int(lock) > 15552000  # 6 months in seconds
    except:
        return False

def is_renounced_or_multisig(mint):
    try:
        url = f"https://public-api.birdeye.so/public/token/{mint}/info"
        headers = {"X-API-KEY": BIRDEYE_API_KEY}
        res = requests.get(url, headers=headers)
        ownership = res.json().get("data", {}).get("ownership")
        return ownership in ["renounced", "multisig"]
    except:
        return False

# ============ ⚡ Raydium Fallback (Stub) ============
def buy_on_raydium(client, keypair, token, amount):
    print(f"[Fallback] Would buy {token} via Raydium with {amount} SOL")
    return False  # Placeholder

# ============ 🔐 Pre-Approval (Stub) ============
def preapprove_token(token):
    print(f"[Pre-Approval] Would pre-approve token: {token}")
    return True  # Simulated success

# ============ 🧠 Multi-Wallet Rotation ============
wallet_keypairs = [
    Keypair.from_bytes(bytes(json.loads(os.getenv("SOLANA_PRIVATE_KEY"))))  # Add more if needed
]
