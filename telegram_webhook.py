import os
import asyncio
from fastapi import FastAPI
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from dotenv import load_dotenv

from utils import send_telegram_alert, get_wallet_status_message
from sniper_logic import start_sniper, start_sniper_with_forced_token

load_dotenv()

# === State Tracking ===
running = False
sniper_task = None

# === Command: /start ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global running, sniper_task
    if not running:
        await update.message.reply_text("🚀 Sniper bot launching...")
        running = True
        sniper_task = asyncio.create_task(start_sniper())
    else:
        await update.message.reply_text("⚠️ Sniper already running.")

# === Command: /stop ===
async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global running, sniper_task
    if running and sniper_task:
        sniper_task.cancel()
        running = False
        await update.message.reply_text("🛑 Sniper stopped.")
    else:
        await update.message.reply_text("⚠️ Sniper is not running.")

# === Command: /forcebuy <TOKEN> ===
async def forcebuy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text("❌ Usage: /forcebuy <TOKEN_MINT>")
        return
    mint = context.args[0]
    await update.message.reply_text(f"🔫 Buying token: `{mint}`", parse_mode="Markdown")
    await start_sniper_with_forced_token(mint)

# === Command: /status ===
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = get_wallet_status_message()
    await update.message.reply_text(msg)

# === FastAPI + Telegram App ===
app = FastAPI()

@app.on_event("startup")
async def startup():
    telegram_app = Application.builder().token(os.getenv("TELEGRAM_BOT_TOKEN")).build()
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("stop", stop))
    telegram_app.add_handler(CommandHandler("forcebuy", forcebuy))
    telegram_app.add_handler(CommandHandler("status", status))
    asyncio.create_task(telegram_app.initialize())
    asyncio.create_task(telegram_app.start())
    asyncio.create_task(telegram_app.updater.start_polling())
