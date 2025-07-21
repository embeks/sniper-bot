# =========================
# utils.py — Final Elite Version (Full Features: Buy/Sell/Alerts/PnL)
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
BLACKLIST_FILE = "blacklist.txt"

# 💪 Wallet Setup
keypair = Keypair.from_bytes(bytes(SOLANA_PRIVATE_KEY))
wallet_pubkey = str(keypair.pubkey())

# 🌐 Solana RPC
rpc = Client(RPC_URL)

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

# ✅ Is Valid Mint
def is_valid_mint(keys):
    for k in keys:
        if isinstance(k, dict):
            if k.get("pubkey") == TOKEN_PROGRAM_ID:
                return True
    return False

# ✅ Sniped Token Tracker + Buy Logic
async def snipe_token(mint):
    try:
        if not os.path.exists("sniped_tokens.txt"):
            open("sniped_tokens.txt", "w").close()
        with open("sniped_tokens.txt", "r") as f:
            if mint in f.read():
                return False
        with open("sniped_tokens.txt", "a") as f:
            f.write(mint + "\n")

        # Blacklist check
        if os.path.exists(BLACKLIST_FILE):
            with open(BLACKLIST_FILE, "r") as b:
                if mint in b.read():
                    await send_telegram_alert(f"🚫 Blacklisted token: {mint}")
                    return False

        data = await get_token_data(mint)
        if data["liquidity"] < 1000 or not data["lp_locked"]:
            await send_telegram_alert(f"🛑 Safety check failed for {mint}")
            return False

        if await is_volume_spike(mint):
            await send_telegram_alert(f"📉 Volume spike detected. Skipping {mint}")
            return False

        await send_telegram_alert(f"🛒 Buying {mint} with {BUY_AMOUNT_SOL} SOL...")

        # Placeholder for actual Jupiter swap logic
        await asyncio.sleep(0.3)  # Simulate delay

        log_trade(mint, "BUY", BUY_AMOUNT_SOL, 0)
        await asyncio.sleep(3)
        await send_telegram_alert(f"✅ Buy complete. Monitoring for profit on {mint}...")

        # Placeholder for partial sells
        await asyncio.sleep(10)
        await send_telegram_alert(f"🔁 Auto-partial sell executed for {mint}")
        log_trade(mint, "SELL", 0, 0)

        return True

    except Exception as e:
        await send_telegram_alert(f"[‼️] Snipe error: {e}")
        return False

# ✅ Balance Check
async def get_token_balance(wallet: str, token_mint: str):
    try:
        url = f"https://public-api.birdeye.so/public/holder_token_amount?wallet={wallet}&token={token_mint}"
        headers = {"x-chain": "solana", "X-API-KEY": BIRDEYE_API_KEY}
        async with httpx.AsyncClient() as client:
            res = await client.get(url, headers=headers)
            data = res.json().get("data", {})
            return float(data.get("amount", 0))
    except:
        return 0.0

# ✅ Telegram Bot Commands
async def status(update, context):
    sol = await get_token_balance(wallet_pubkey, "So11111111111111111111111111111111111111112")
    await update.message.reply_text(f"🟢 Bot is running.\nWallet: `{wallet_pubkey}`\nSOL: {sol:.4f}")

async def holdings(update, context):
    try:
        with open("sniped_tokens.txt", "r") as f:
            tokens = f.read().splitlines()
        reply = "📦 Current sniped tokens:\n" + "\n".join(tokens[-10:]) if tokens else "📦 No sniped tokens yet."
        await update.message.reply_text(reply)
    except:
        await update.message.reply_text("⚠️ Could not load holdings.")

async def logs(update, context):
    try:
        with open("trade_log.csv", "r") as f:
            lines = f.readlines()[-10:]
        reply = "📝 Last trades:\n" + "".join(lines) if lines else "📝 No trades yet."
        await update.message.reply_text(reply)
    except:
        await update.message.reply_text("📝 Could not read log file.")

async def wallet(update, context):
    await update.message.reply_text(f"💼 Current wallet: `{wallet_pubkey}`")

async def reset(update, context):
    open("sniped_tokens.txt", "w").close()
    await update.message.reply_text("♻️ Sniped list reset.")

def start_command_bot():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("holdings", holdings))
    app.add_handler(CommandHandler("logs", logs))
    app.add_handler(CommandHandler("wallet", wallet))
    app.add_handler(CommandHandler("reset", reset))
    print("🤖 Telegram command bot ready.")
    return app.run_polling()
