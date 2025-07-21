# =========================
# utils.py — FINAL VERSION (All-in-One)
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
from solana.publickey import PublicKey

load_dotenv()

# 🔐 ENV + Wallet
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
RPC_URL = os.getenv("RPC_URL")
SOLANA_PRIVATE_KEY = json.loads(os.getenv("SOLANA_PRIVATE_KEY"))
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")

# 💰 Constants
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

# 💪 Wallet Setup
keypair = Keypair.from_bytes(bytes(SOLANA_PRIVATE_KEY))
wallet_pubkey = str(keypair.pubkey())

# 🌐 Solana RPC
def get_rpc_client():
    return Client(RPC_URL)

# 📬 Telegram Alerts
async def send_telegram_alert(message: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        async with httpx.AsyncClient() as client:
            await client.post(url, json=payload)
    except Exception as e:
        print(f"[‼️] Telegram alert failed: {e}")

# 📊 Trade Logger
def log_trade_to_csv(token, action, amount_in, amount_out):
    with open("trade_log.csv", "a", newline="") as file:
        writer = csv.writer(file)
        writer.writerow([datetime.utcnow().isoformat(), token, action, amount_in, amount_out])

# 📈 Get Token Price
async def get_token_price(token_mint: str) -> float:
    try:
        url = f"https://public-api.birdeye.so/public/price?address={token_mint}"
        headers = {"x-chain": "solana", "X-API-KEY": BIRDEYE_API_KEY}
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers=headers)
            return r.json().get("data", {}).get("value")
    except:
        return None

# 🔎 Get Token Data
async def get_token_data(mint: str) -> dict:
    try:
        url = f"https://public-api.birdeye.so/public/token/{mint}"
        headers = {"x-chain": "solana", "X-API-KEY": BIRDEYE_API_KEY}
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

# 💥 Rug Detection
def is_rug(initial_lp, current_lp, threshold=0.75):
    return current_lp < (initial_lp * threshold)

# ✅ Valid Mint Checker
def is_valid_mint(account_keys):
    for key in account_keys:
        if isinstance(key, dict):
            pubkey = key.get("pubkey", "")
            if pubkey == TOKEN_PROGRAM_ID:
                return True
    return False

# 🔐 Buy Token
async def buy_token(token_address: str, amount_sol: float):
    try:
        client = get_rpc_client()
        lamports = int(amount_sol * 1_000_000_000)
        tx = client.request_airdrop(PublicKey(wallet_pubkey), lamports)  # TEMP MOCK
        sig = tx.get("result")
        await send_telegram_alert(f"✅ Simulated buy: {amount_sol} SOL into {token_address}\nTX: {sig}")
        log_trade_to_csv(token_address, "BUY", amount_sol, 0)
        return True
    except Exception as e:
        await send_telegram_alert(f"❌ Buy failed for {token_address}: {e}")
        return False

# 🧬 Snipe Token
async def snipe_token(mint: str) -> bool:
    try:
        if not os.path.exists("sniped_tokens.txt"):
            open("sniped_tokens.txt", "w").close()
        with open("sniped_tokens.txt", "r") as f:
            if mint in f.read():
                await send_telegram_alert(f"⚠️ Token already sniped: {mint}")
                return False
        with open("sniped_tokens.txt", "a") as f:
            f.write(mint + "\n")

        await send_telegram_alert(f"🛒 Buying token: {mint}")
        await buy_token(token_address=mint, amount_sol=0.03)
        return True
    except Exception as e:
        await send_telegram_alert(f"[‼️] Snipe error: {e}")
        print(f"[‼️] Snipe token error: {e}")
        return False
        # ✅ Final Buy Logic (Simulated for now – replace with Jupiter SDK if needed)
async def buy_token(token_address: str, amount_sol: float):
    try:
        # Replace this with actual Jupiter logic later
        print(f"[LIVE] Buying {amount_sol} SOL of {token_address} (simulated)")
        await send_telegram_alert(f"✅ Simulated buy of {amount_sol} SOL for `{token_address}`")
        return True
    except Exception as e:
        await send_telegram_alert(f"❌ Buy failed for {token_address}: {e}")
        return False
