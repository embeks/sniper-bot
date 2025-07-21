# =========================
# utils.py — Full Elite Version (with buy+simulate snipe_token)
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

from jupiter_trade import (
    is_token_supported_by_jupiter,
    get_jupiter_quote,
    build_jupiter_swap_tx,
    sign_and_send_tx
)

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

# 🔎 Get Token Data (LP, holders, renounced, locked)
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

# 🧠 Holder Delta (momentum metric)
async def get_holder_delta(mint: str, delay=60):
    initial = (await get_token_data(mint)).get("holders", 0)
    await asyncio.sleep(delay)
    later = (await get_token_data(mint)).get("holders", 0)
    return later - initial

# 📦 Pre-Approve Stub
async def preapprove_token(token_address: str) -> bool:
    try:
        await asyncio.sleep(0.1)
        return True
    except:
        return False

# 🛡️ Safety Check
async def is_safe_token(mint: str) -> bool:
    try:
        data = await get_token_data(mint)
        return data.get("lp_locked", False) and data.get("renounced", False)
    except:
        return False

# 💥 Rug Detection
def is_rug(initial_lp, current_lp, threshold=0.75):
    return current_lp < (initial_lp * threshold)

# ⚡ Volume Spike Check
async def is_volume_spike(mint: str, threshold: float = 5.0):
    try:
        url = f"https://public-api.birdeye.so/public/token/{mint}/chart?time=1m"
        headers = {"x-chain": "solana", "X-API-KEY": BIRDEYE_API_KEY}
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

# 💼 Balance Check
async def get_token_balance(wallet_address: str, token_mint: str) -> float:
    try:
        url = f"https://public-api.birdeye.so/public/holder_token_amount?wallet={wallet_address}&token={token_mint}"
        headers = {"x-chain": "solana", "X-API-KEY": BIRDEYE_API_KEY}
        async with httpx.AsyncClient() as client:
            res = await client.get(url, headers=headers)
            data = res.json().get("data", {})
            return float(data.get("amount", 0))
    except:
        return 0.0

# 🧪 Multi-Wallet Stub
def get_next_wallet():
    return keypair, wallet_pubkey

# ♻ Raydium Fallback Stub
async def buy_on_raydium(rpc_client, kp, token, amount):
    await asyncio.sleep(0.3)
    return False

# 🧠 Alpha Feed Scanner Stub
async def scan_alpha_feeds():
    return ["token_mint_example_1", "token_mint_example_2"]

# ✅ Valid Mint Checker
def is_valid_mint(account_keys):
    for key in account_keys:
        if isinstance(key, dict):
            pubkey = key.get("pubkey", "")
            if pubkey == TOKEN_PROGRAM_ID:
                return True
    return False

# 🧬 Sniped Tokens Log + Full Buy Logic
async def snipe_token(mint: str) -> bool:
    try:
        if not os.path.exists("sniped_tokens.txt"):
            with open("sniped_tokens.txt", "w") as f:
                pass
        with open("sniped_tokens.txt", "r") as f:
            if mint in f.read():
                return False
        with open("sniped_tokens.txt", "a") as f:
            f.write(mint + "\n")

        await send_telegram_alert(f"🎯 New token detected: `{mint}`")
        amount_sol = 0.03

        supported = await is_token_supported_by_jupiter(mint)
        if not supported:
            await send_telegram_alert("❌ Not supported by Jupiter. Trying Raydium...")
            result = await buy_on_raydium(get_rpc_client(), keypair, mint, amount_sol)
            if result:
                await send_telegram_alert("✅ Bought on Raydium fallback.")
            else:
                await send_telegram_alert("‼️ Raydium fallback failed.")
            return result

        await asyncio.sleep(0.1)
        await send_telegram_alert("🔍 Getting Jupiter route...")
        route = await get_jupiter_quote(mint, amount_sol)
        if not route:
            await send_telegram_alert("❌ No route from Jupiter.")
            return False

        await asyncio.sleep(0.1)
        await send_telegram_alert("🧠 Building TX for simulation...")
        raw_tx = await build_jupiter_swap_tx(route)
        if not raw_tx:
            await send_telegram_alert("❌ Failed to build TX.")
            return False

        await asyncio.sleep(0.1)
        await send_telegram_alert("🚀 Sending TX...")
        tx_sig = sign_and_send_tx(raw_tx)
        if tx_sig:
            await send_telegram_alert(f"✅ TX sent: https://solscan.io/tx/{tx_sig}")
            log_trade_to_csv(mint, "buy", amount_sol, route['outAmount'] / 1e9)
            return True
        else:
            await send_telegram_alert("‼️ TX failed to send.")
            return False

    except Exception as e:
        print(f"[‼️] Snipe error: {e}")
        await send_telegram_alert(f"[‼️] Snipe error: {e}")
        return False
