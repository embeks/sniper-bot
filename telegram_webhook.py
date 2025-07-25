from fastapi import FastAPI, Request
import os
import telegram
import logging
import asyncio

from sniper_logic import start_sniper  # make sure this path is correct

app = FastAPI()

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("telegram_webhook")

# Telegram bot setup
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)

# Background sniper startup
@app.on_event("startup")
async def startup_event():
    logger.info("Launching sniper logic...")
    asyncio.create_task(start_sniper())  # Non-blocking sniper start

# Telegram webhook endpoint
@app.post("/webhook")
async def handle_webhook(request: Request):
    data = await request.json()
    update = telegram.Update.de_json(data, bot)

    if update.message and update.message.chat:
        chat_id = update.message.chat.id
        message_text = update.message.text

        if message_text == "/status":
            await bot.send_message(chat_id=chat_id, text="✅ Bot is live and responding.")

    return {"ok": True}
