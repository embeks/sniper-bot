# =============================
# utils.py — Jupiter SDK Buy/Sell + Partial Sell Logic (Final Version)
# =============================

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
from solders.transaction import Transaction
from solana.publickey import PublicKey
from base64 import b64decode, b64encode

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
SELL_MULTIPLIERS = list(map(float, os.getenv("SELL_MULTIPLIERS", "2,5,10").split(",")))

# 💪 Wallet Setup
keypair = Keypair.from_bytes(bytes(SOLANA_PRIVATE_KEY))
wallet_pubkey = str(keypair.pubkey())

# 🌐 Solana RPC
rpc = Client(RPC_URL)

# ===========================
# 📬 Telegram Alerts
# ===========================

async def send_telegram_alert(message: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        async with httpx.AsyncClient() as client:
            await client.post(url, json=payload)
    except:
        pass

# ===========================
# 📊 Trade Logger
# ===========================

def log_trade(token, action, sol_in, token_out):
    with open("trade_log.csv", "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([datetime.utcnow().isoformat(), token, action, sol_in, token_out])

# ===========================
# 📈 Token Price (Birdeye)
# ===========================

async def get_token_price(token_mint):
    try:
        url = f"https://public-api.birdeye.so/public/price?address={token_mint}"
        headers = {"x-chain": "solana", "X-API-KEY": BIRDEYE_API_KEY}
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers=headers)
            return r.json().get("data", {}).get("value")
    except:
        return None

# ===========================
# 🚀 BUY with Jupiter SDK (SOL -> Token)
# ===========================

async def buy_token_sdk(token_mint):
    try:
        url = "https://quote-api.jup.ag/v6/swap"
        payload = {
            "inputMint": "So11111111111111111111111111111111111111112",  # SOL
            "outputMint": token_mint,
            "amount": int(BUY_AMOUNT_SOL * 1e9),
            "slippageBps": 500,
            "userPublicKey": wallet_pubkey,
            "wrapAndUnwrapSol": True,
            "asLegacyTransaction": True
        }
        async with httpx.AsyncClient() as client:
            res = await client.post(url, json=payload)
            tx = res.json().get("swapTransaction")
            if not tx:
                await send_telegram_alert("❌ Jupiter buy TX failed to generate")
                return False
            txn = Transaction.from_bytes(b64decode(tx))
            sig = rpc.send_transaction(txn, keypair)
            await send_telegram_alert(f"✅ Bought {token_mint} — TX: https://solscan.io/tx/{sig['result']}")
            log_trade(token_mint, "BUY", BUY_AMOUNT_SOL, 0)
            return True
    except Exception as e:
        await send_telegram_alert(f"[‼️] Buy Error: {e}")
        return False

# ===========================
# 💸 SELL with Jupiter SDK (Token -> SOL)
# ===========================

async def sell_token_sdk(token_mint, amount):
    try:
        url = "https://quote-api.jup.ag/v6/swap"
        payload = {
            "inputMint": token_mint,
            "outputMint": "So11111111111111111111111111111111111111112",
            "amount": int(amount),
            "slippageBps": 500,
            "userPublicKey": wallet_pubkey,
            "wrapAndUnwrapSol": True,
            "asLegacyTransaction": True
        }
        async with httpx.AsyncClient() as client:
            res = await client.post(url, json=payload)
            tx = res.json().get("swapTransaction")
            if not tx:
                await send_telegram_alert("❌ Jupiter sell TX failed to generate")
                return False
            txn = Transaction.from_bytes(b64decode(tx))
            sig = rpc.send_transaction(txn, keypair)
            await send_telegram_alert(f"✅ Sold {token_mint} — TX: https://solscan.io/tx/{sig['result']}")
            log_trade(token_mint, "SELL", 0, amount)
            return True
    except Exception as e:
        await send_telegram_alert(f"[‼️] Sell Error: {e}")
        return False

# ===========================
# 👛 Get Token Account Balance
# ===========================

def get_token_balance(mint):
    accounts = rpc.get_token_accounts_by_owner(wallet_pubkey, {"mint": mint})
    if not accounts["result"]["value"]:
        return 0, None
    acct = accounts["result"]["value"][0]
    amt = int(acct["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
    return amt, acct["pubkey"]

# ===========================
# 🧪 Partial Sell Logic
# ===========================

async def partial_sell(mint, entry_price):
    try:
        balance, token_account = get_token_balance(mint)
        if not balance:
            await send_telegram_alert(f"⚠️ No balance to sell for {mint}")
            return

        for i, multiplier in enumerate(SELL_MULTIPLIERS):
            await asyncio.sleep(5)  # wait a bit between checks
            price_now = await get_token_price(mint)
            if not price_now:
                continue
            if price_now >= entry_price * multiplier:
                portion = [0.5, 0.25, 0.25][i] if i < 3 else 1.0
                amount = int(balance * portion)
                await sell_token_sdk(mint, amount)
                balance -= amount
                if balance <= 0:
                    break

    except Exception as e:
        await send_telegram_alert(f"[‼️] Partial sell error: {e}")

# ===========================
# ✅ Is Valid Mint
# ===========================

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

