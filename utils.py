# =========================
# utils.py (Final Version)
# =========================
import os
import json
import csv
import base64
import asyncio
import aiohttp
from datetime import datetime
from dotenv import load_dotenv

from solana.rpc.api import Client
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solders.instruction import Instruction
from solders.message import MessageV0
from solders.message import LoadedMessageV0
from solders.hash import Hash

load_dotenv()

# ========================= 🔐 ENV & Setup =========================
RPC_URL = os.getenv("RPC_URL") or "https://api.mainnet-beta.solana.com"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")

# ========================= 📡 Client Helpers =========================
def get_rpc_client():
    return Client(RPC_URL)

# ========================= 📢 Telegram Alerts =========================
async def send_telegram_alert(message: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
        async with aiohttp.ClientSession() as session:
            await session.post(url, data=payload)
    except Exception as e:
        print(f"[!] Telegram alert failed: {e}")

# ========================= 💾 Trade Logger =========================
def log_trade_to_csv(token_address, action, amount_in, amount_out):
    file = "trade_log.csv"
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    pnl = amount_out - amount_in
    row = [now, token_address, action, amount_in, amount_out, pnl]
    with open(file, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(row)

# ========================= 🧠 Token Data =========================
async def get_token_price(token_address: str) -> float:
    try:
        url = f"https://public-api.birdeye.so/public/price?address={token_address}"
        headers = {"X-API-KEY": BIRDEYE_API_KEY}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as res:
                data = await res.json()
                return float(data['data']['value']) if data.get('data') else None
    except:
        return None

async def get_token_data(token_address: str):
    try:
        url = f"https://public-api.birdeye.so/public/token/{token_address}"
        headers = {"X-API-KEY": BIRDEYE_API_KEY}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as res:
                data = await res.json()
                return data.get("data", {})
    except:
        return {}

async def get_token_balance(token_address: str) -> int:
    try:
        keypair = Keypair.from_bytes(bytes(json.loads(os.getenv("SOLANA_PRIVATE_KEY"))))
        wallet = str(keypair.pubkey())
        client = get_rpc_client()
        resp = client.get_token_accounts_by_owner(wallet, {"mint": token_address})
        for acc in resp.value:
            amount = int(acc.account.data.parsed['info']['tokenAmount']['amount'])
            return amount
        return 0
    except:
        return 0

# ========================= 📦 Raydium Direct Fallback =========================
async def buy_on_raydium(client: Client, keypair: Keypair, token_mint: str, amount_sol: float):
    try:
        # This is a placeholder until a proper Raydium buy TX is constructed
        # Here you'd encode a swap instruction using the Raydium swap pool info
        print(f"[⏳] Raydium fallback buy for {token_mint} @ {amount_sol} SOL")
        return False
    except Exception as e:
        print(f"[‼️] Raydium buy failed: {e}")
        return False

# ========================= 📊 Token Holder Delta Tracker =========================
async def get_token_holder_count(token_address: str):
    try:
        url = f"https://public-api.birdeye.so/public/token/{token_address}"
        headers = {"X-API-KEY": BIRDEYE_API_KEY}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as res:
                data = await res.json()
                return int(data["data"].get("holders", 0))
    except:
        return 0
