# =============================
# utils.py — Log Skipped Tokens + Alert
# =============================

import os
import json
import httpx
import asyncio
import csv
from datetime import datetime, timedelta
from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.api import Client
from telegram.ext import Application, CommandHandler
from jupiter_aggregator import JupiterAggregatorClient

load_dotenv()

# 🔐 ENV + Wallet
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
RPC_URL = os.getenv("RPC_URL")
SOLANA_PRIVATE_KEY = json.loads(os.getenv("SOLANA_PRIVATE_KEY"))
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")
BUY_AMOUNT_SOL = float(os.getenv("BUY_AMOUNT_SOL", 0.03))
SELL_TIMEOUT_SEC = int(os.getenv("SELL_TIMEOUT_SEC", 300))
RUG_LP_THRESHOLD = float(os.getenv("RUG_LP_THRESHOLD", 0.75))

# 💪 Wallet Setup
keypair = Keypair.from_bytes(bytes(SOLANA_PRIVATE_KEY))
wallet_pubkey = str(keypair.pubkey())
rpc = Client(RPC_URL)
jupiter = JupiterAggregatorClient(RPC_URL)

# 📩 Telegram Alerts
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

# ⚠️ Skipped Token Logger
def log_skipped_token(mint: str, reason: str):
    with open("skipped_tokens.csv", "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([datetime.utcnow().isoformat(), mint, reason])

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

# 🔁 Buy Token
async def buy_token(mint: str):
    try:
        input_mint = Pubkey.from_string("So11111111111111111111111111111111111111112")
        output_mint = Pubkey.from_string(mint)

        quote = jupiter.get_quote(input_mint, output_mint, int(BUY_AMOUNT_SOL * 1e9))
        if not quote:
            await send_telegram_alert(f"❌ No quote found for {mint}")
            log_skipped_token(mint, "No Jupiter quote")
            return False

        tx = jupiter.build_swap_transaction(quote["swapTransaction"], keypair)
        sig = rpc.send_raw_transaction(tx)
        await send_telegram_alert(f"✅ Buy tx sent: https://solscan.io/tx/{sig}")
        log_trade(mint, "BUY", BUY_AMOUNT_SOL, 0)
        return True

    except Exception as e:
        await send_telegram_alert(f"❌ Buy failed for {mint}: {e}")
        log_skipped_token(mint, f"Buy failed: {e}")
        return False

# 💸 Sell Token
async def sell_token(mint: str, percent: float = 100.0):
    try:
        input_mint = Pubkey.from_string(mint)
        output_mint = Pubkey.from_string("So11111111111111111111111111111111111111112")

        # Dummy logic for example (replace with token balance check)
        quote = jupiter.get_quote(input_mint, output_mint, int(BUY_AMOUNT_SOL * 1e9 * percent / 100))
        if not quote:
            await send_telegram_alert(f"❌ No sell quote found for {mint}")
            return False

        tx = jupiter.build_swap_transaction(quote["swapTransaction"], keypair)
        sig = rpc.send_raw_transaction(tx)
        await send_telegram_alert(f"✅ Sell {percent}% sent: https://solscan.io/tx/{sig}")
        log_trade(mint, f"SELL {percent}%", 0, quote.get("outAmount", 0) / 1e9)
        return True

    except Exception as e:
        await send_telegram_alert(f"❌ Sell failed for {mint}: {e}")
        return False

# 📈 Price Auto-Sell Logic (unchanged placeholder)
async def wait_and_auto_sell(mint):
    pass

# ✅ Is Valid Mint
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
def is_valid_mint(keys):
    for k in keys:
        if isinstance(k, dict):
            if k.get("pubkey") == TOKEN_PROGRAM_ID:
                return True
    return False

# 🤖 Telegram Bot
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
        await update.message.reply_text("📜 Last trades:\n" + "".join(lines) if lines else "📜 No trades logged yet.")
    except:
        await update.message.reply_text("📜 No logs found.")

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

