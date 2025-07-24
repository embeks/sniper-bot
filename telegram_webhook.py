from fastapi import FastAPI, Request
import os
import telegram

# Create FastAPI app
app = FastAPI()

# Initialize Telegram Bot using token from env
bot = telegram.Bot(token=os.getenv("TELEGRAM_BOT_TOKEN"))

# Telegram webhook route
@app.post("/webhook")
async def handle_webhook(request: Request):
    data = await request.json()
    update = telegram.Update.de_json(data, bot)

    if update.message and update.message.chat:
        chat_id = update.message.chat.id
        message_text = update.message.text
        bot.send_message(chat_id=chat_id, text="✅ Webhook received!")

    return {"ok": True}
