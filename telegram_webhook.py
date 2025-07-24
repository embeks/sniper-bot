from fastapi import FastAPI, Request
import os
import telegram
import logging

app = FastAPI()

# Setup logging
logging.basicConfig(level=logging.INFO)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)

@app.get("/")
async def root():
    return {"status": "online"}

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    logging.info(f"Webhook received: {data}")  # log raw Telegram update

    update = telegram.Update.de_json(data, bot)

    if update.message:
        chat_id = update.message.chat.id
        text = update.message.text
        if text == "/status":
            bot.send_message(chat_id=chat_id, text="✅ Bot is live and responding.")
    return {"ok": True}
