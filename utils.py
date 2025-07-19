# =========================
# utils.py (Elite Upgraded with get_token_balance)
# =========================
import os
import json
import httpx
import asyncio
import csv
from datetime import datetime
from dotenv import load_dotenv
from solana.rpc.api import Client
from solders.keypair import Keypair
from solders.rpc.config import RpcSendTransactionConfig
from solana.rpc.commitment import Confirmed

load_dotenv()

# 🌐 Telegram Setup
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ✅ Solana RPC & Client
RPC_URL = os.getenv("RPC_URL")
def get_rpc_client():
    return Client(RPC_URL)

# ✅ Wallet Setup
SOLANA_PRIVATE_KEY = json.loads(os.getenv("SOLANA_PRIVATE_KEY"))
keypair = Keypair.from_bytes(bytes(SOLANA_PRIVATE_KEY))
wallet_pubkey = str(keypair.pubkey())

# 🔔 Telegram Alert
async def send_telegram_alert(message: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        async with httpx.AsyncClient() as client:
            await client.post(url, json=payload)
    except Exception as e:
        print(f"[‼️] Telegram alert failed: {e}")

# 📊 CSV Logger
def log_trade_to_csv(token, action, amount_in, amount_out):
    with open("trade_log.csv", "a", newline="") as file:
        writer = csv.writer(file)
        writer.writerow([datetime.utcnow().isoformat(), token, action, amount_in, amount_out])

# 📉 Get Token Price
async def get_token_price(token_mint: str) -> float:
    try:
        url = f"https://public-api.birdeye.so/public/price?address={token_mint}"
        headers = {"x-chain": "solana", "X-API-KEY": os.getenv("BIRDEYE_API_KEY")}
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers=headers)
            return r.json().get("data", {}).get("value")
    except:
        return None

# 📦 Token Data: liquidity, holders, LP status
async def get_token_data(mint: str) -> dict:
    try:
        url = f"https://public-api.birdeye.so/public/token/{mint}"
        headers = {"x-chain": "solana", "X-API-KEY": os.getenv("BIRDEYE_API_KEY")}
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers=headers)
            data = r.json().get("data", {})
            return {
                "liquidity": data.get("liquidity", 0),
                "holders": data.get("holder_count", 0),
                "renounced": data.get("is_renounced", False),
                "lp_locked": data.get("is_lp_locked", False)
            }
    except:
        return {}

# 🧠 Holder Delta (for dynamic buy size)
async def get_holder_delta(mint: str, delay=60):
    initial = (await get_token_data(mint)).get("holders", 0)
    await asyncio.sleep(delay)
    later = (await get_token_data(mint)).get("holders", 0)
    return later - initial

# 🔒 Pre-Approve Token Transfer
async def preapprove_token(token_address: str) -> bool:
    try:
        url = f"https://api.dexscreener.com/latest/dex/pair/solana/{token_address}"
        async with httpx.AsyncClient() as client:
            await client.get(url)  # Placeholder for actual approval logic
        return True
    except:
        return False

# ⚠️ Token Safety Filter
async def is_safe_token(mint: str) -> bool:
    try:
        data = await get_token_data(mint)
        if not data.get("lp_locked", False):
            return False
        if not data.get("renounced", False):
            return False
        return True
    except:
        return False

# 💥 Rug Detection (Liquidity drop by 25%)
def is_rug(initial_lp, current_lp, threshold=0.75):
    return current_lp < (initial_lp * threshold)

# 📈 1-min Volume Spike Detection
async def is_volume_spike(mint: str, threshold: float = 5.0):
    try:
        url = f"https://public-api.birdeye.so/public/token/{mint}/chart?time=1m"
        headers = {"x-chain": "solana", "X-API-KEY": os.getenv("BIRDEYE_API_KEY")}
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers=headers)
            data = r.json().get("data", [])
            if len(data) < 2:
                return False
            open_vol = data[-2].get("volume", 1)
            close_vol = data[-1].get("volume", 1)
            return (close_vol / open_vol) >= threshold
    except:
        return False

# 🧮 Get Token Balance (for specific token)
async def get_token_balance(wallet_address: str, token_mint: str) -> float:
    try:
        url = f"https://public-api.birdeye.so/public/holder_token_amount?wallet={wallet_address}&token={token_mint}"
        headers = {"x-chain": "solana", "X-API-KEY": os.getenv("BIRDEYE_API_KEY")}
        async with httpx.AsyncClient() as client:
            res = await client.get(url, headers=headers)
            data = res.json().get("data", {})
            return float(data.get("amount", 0))
    except:
        return 0.0

# 🧪 Multi-Wallet Stub (for future rotation)
def get_next_wallet():
    return keypair, wallet_pubkey

# 📦 Raydium Fallback Stub (integrated in jupiter_trade)
async def buy_on_raydium(rpc_client, kp, token, amount):
    await asyncio.sleep(0.3)  # Simulated TX logic
    return False

# 🧠 Alpha Feed Scanner Stub
async def scan_alpha_feeds():
    return ["token_mint_example_1", "token_mint_example_2"]
