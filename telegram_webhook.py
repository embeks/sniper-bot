# telegram_webhook.py
import os
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from fastapi import FastAPI, Request
from telegram.ext import ApplicationBuilder

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# FastAPI app
app = FastAPI()

# Create the Telegram bot app (webhook mode)
bot_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

# Example /status command handler
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot is alive.")

bot_app.add_handler(CommandHandler("status", status))

# ✅ This route handles incoming Telegram updates (Webhook POSTs)
@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, bot_app.bot)
    await bot_app.process_update(update)
    return {"ok": True}

# ✅ Optional test route
@app.get("/")
async def root():
    return {"message": "Bot is running."}
