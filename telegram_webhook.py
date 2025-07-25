# =============================
# telegram_webhook.py — Full Telegram Command Bot
# =============================

import os
import asyncio
from dotenv import load_dotenv
from fastapi import FastAPI
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

from sniper_logic import start_sniper, start_sniper_with_forced_token
from utils import (
    get_wallet_status_message,
    is_bot_running,
    start_bot,
    stop_bot,
)

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FORCE_TEST_MINT = os.getenv("FORCE_TEST_MINT")

app = FastAPI()
telegram_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

# ✅ /start — Start sniper bot
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_bot_running():
        await update.message.reply_text("⚠️ Bot is already running.")
        return
    await update.message.reply_text("✅ Starting sniper bot...")
    start_bot()
    asyncio.create_task(start_sniper())

# ✅ /stop — Stop sniper bot
async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_running():
        await update.message.reply_text("⚠️ Bot is not currently running.")
        return
    stop_bot()
    await update.message.reply_text("🛑 Bot stopped.")

# ✅ /status — Bot status + wallet
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = get_wallet_status_message()
    await update.message.reply_text(status_msg)

# ✅ /wallet — Just wallet
async def wallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from utils import wallet_pubkey
    await update.message.reply_text(f"💼 Wallet: `{wallet_pubkey}`", parse_mode="Markdown")

# ✅ /reset — Clear sniped list
async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    open("sniped_tokens.txt", "w").close()
    await update.message.reply_text("♻️ Sniped token list reset.")

# ✅ /forcebuy <MINT>
async def forcebuy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usage: /forcebuy <TOKEN_MINT>")
        return
    mint = context.args[0]
    await update.message.reply_text(f"🚨 Forcing buy on {mint}...")
    asyncio.create_task(start_sniper_with_forced_token(mint))

# ✅ Register handlers
telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(CommandHandler("stop", stop_command))
telegram_app.add_handler(CommandHandler("status", status_command))
telegram_app.add_handler(CommandHandler("wallet", wallet_command))
telegram_app.add_handler(CommandHandler("reset", reset_command))
telegram_app.add_handler(CommandHandler("forcebuy", forcebuy_command))

# ✅ FastAPI + Telegram integration
@app.on_event("startup")
async def startup():
    print("🚀 Telegram webhook starting...")
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling()
