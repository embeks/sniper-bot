from fastapi import FastAPI, Request
import os
import telegram
import logging
from telegram import Update

app = FastAPI()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)

@app.get("/")
async def root():
    return {"status": "online"}

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
        logger.info(f"📩 Webhook received: {data}")

        update = telegram.Update.de_json(data, bot)

        if update.message:
            chat_id = update.message.chat.id
            text = update.message.text
            logger.info(f"👤 From: {chat_id}, Message: {text}")
            if text == "/status":
                await bot.send_message(chat_id=chat_id, text="✅ Bot is live and responding.")

        return {"ok": True}
    except Exception as e:
        logger.error(f"❌ Error handling webhook: {e}")
        return {"ok": False, "error": str(e)}
