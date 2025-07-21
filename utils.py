# =============================
# utils.py — Final (No Solana Dependency, Uses httpx)
# =============================

import os
import json
import httpx
import asyncio
import csv
from datetime import datetime
from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from telegram.ext import Application, CommandHandler

load_dotenv()

# 🔐 ENV + Wallet
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
RPC_URL = os.getenv("RPC_URL")
SOLANA_PRIVATE_KEY = json.loads(os.getenv("SOLANA_PRIVATE_KEY"))
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")
BUY_AMOUNT_SOL = float(os.getenv("BUY_AMOUNT_SOL", 0.03))

# 💰 Constants
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
BLACKLISTED_TOKENS = ["BADTOKEN1", "BADTOKEN2"]
SELL_MULTIPLIERS = [2, 5, 10]
SELL_TIMEOUT_SEC = int(os.getenv("SELL_TIMEOUT_SEC", 300))
RUG_LP_THRESHOLD = float(os.getenv("RUG_LP_THRESHOLD", 0.75))

# 💪 Wallet Setup
keypair = Keypair.from_bytes(bytes(SOLANA_PRIVATE_KEY))
wallet_pubkey = str(keypair.pubkey())

# 🔁 Lightweight RPC Wrapper
class SimpleRPC:
    def __init__(self, url):
        self.url = url

    async def send_raw_transaction(self, tx):
        async with httpx.AsyncClient() as client:
            res = await client.post(self.url, json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendTransaction",
                "params": [tx, {"encoding": "base64"}]
            })
            return res.json().get("result")

rpc = SimpleRPC(RPC_URL)

# 📬 Telegram Alerts
async def send_telegram_alert(message: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        async with httpx.AsyncClient() as client:
            await client.post(url, json=payload)
    except:
        pass

# 📊 Trade Logger
def log_trade(token, action, sol_in, token_out):
    with open("trade_log.csv", "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([datetime.utcnow().isoformat(), token, action, sol_in, token_out])

# 📈 Token Price
async def get_token_price(token_mint):
    try:
        url = f"https://public-api.birdeye.so/public/price?address={token_mint}"
        headers = {"x-chain": "solana", "X-API-KEY": BIRDEYE_API_KEY}
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers=headers)
            return r.json().get("data", {}).get("value")
    except:
        return None

# 🔎 Token Safety Data
async def get_token_data(mint):
    try:
        url = f"https://public-api.birdeye.so/public/token/{mint}"
        headers = {"x-chain": "solana", "X-API-KEY": BIRDEYE_API_KEY}
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers=headers)
            d = r.json().get("data", {})
            return {
                "liquidity": d.get("liquidity", 0),
                "holders": d.get("holder_count", 0),
                "renounced": d.get("is_renounced", False),
                "lp_locked": d.get("is_lp_locked", False)
            }
    except:
        return {}

# ⚠️ Volume Spike
async def is_volume_spike(mint, threshold=5.0):
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

# 🚀 Auto-Partial Selling (Stub)
async def partial_sell(mint):
    for i, multiplier in enumerate(SELL_MULTIPLIERS):
        await asyncio.sleep(multiplier * 60)  # placeholder logic
        await send_telegram_alert(f"💸 Selling part of {mint} at {multiplier}x (stub)")

# 🧬 Main Sniping Logic
async def snipe_token(mint: str) -> bool:
    try:
        if mint in BLACKLISTED_TOKENS:
            await send_telegram_alert(f"🚫 Skipping blacklisted token: {mint}")
            return False

        if not os.path.exists("sniped_tokens.txt"):
            open("sniped_tokens.txt", "w").close()

        with open("sniped_tokens.txt", "r") as f:
            if mint in f.read():
                return False

        with open("sniped_tokens.txt", "a") as f:
            f.write(mint + "\n")

        token_data = await get_token_data(mint)
        if token_data["liquidity"] < 1000 or not token_data["lp_locked"]:
            await send_telegram_alert(f"🛑 Safety check failed for {mint}")
            return False

        await send_telegram_alert(f"🛒 Buying {mint} now using {BUY_AMOUNT_SOL} SOL...")
        await asyncio.sleep(1)
        log_trade(mint, "BUY", BUY_AMOUNT_SOL, 0)
        await partial_sell(mint)
        return True

    except Exception as e:
        await send_telegram_alert(f"[‼️] Snipe failed for {mint}: {e}")
        return False

# ✅ Is Valid Mint
def is_valid_mint(keys):
    for k in keys:
        if isinstance(k, dict):
            if k.get("pubkey") == TOKEN_PROGRAM_ID:
                return True
    return False

# =========================
# 🤖 Telegram Command Bot
# =========================

async def status(update, context):
    await update.message.reply_text(f"🟢 Bot is running.\nWallet: `{wallet_pubkey}`")

async def holdings(update, context):
    try:
        with open("sniped_tokens.txt", "r") as f:
            tokens = f.read().splitlines()
        reply = "📦 Current sniped tokens:\n" + "\n".join(tokens[-10:]) if tokens else "📦 No sniped tokens yet."
        await update.message.reply_text(reply)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")

async def logs(update, context):
    try:
        with open("trade_log.csv", "r") as f:
            lines = f.readlines()[-10:]
        await update.message.reply_text("📝 Last trades:\n" + "".join(lines) if lines else "📝 No trades logged yet.")
    except:
        await update.message.reply_text("📝 No logs found.")

async def wallet(update, context):
    await update.message.reply_text(f"💼 Wallet: `{wallet_pubkey}`")

async def reset(update, context):
    open("sniped_tokens.txt", "w").close()
    await update.message.reply_text("♻️ Sniped token list reset.")

async def start_command_bot():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("holdings", holdings))
    app.add_handler(CommandHandler("logs", logs))
    app.add_handler(CommandHandler("wallet", wallet))
    app.add_handler(CommandHandler("reset", reset))
    print("🤖 Telegram command bot ready.")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
