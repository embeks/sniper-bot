from fastapi import FastAPI, Request
import os
import telegram
import asyncio

app = FastAPI()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
bot = telegram.Bot(token=TELEGRAM_TOKEN)

@app.get("/")
def root():
    return {"status": "Bot is live"}

@app.post("/webhook")
async def handle_webhook(request: Request):
    data = await request.json()
    update = telegram.Update.de_json(data, bot)

    if update.message and update.message.chat:
        chat_id = update.message.chat.id
        message_text = update.message.text

        if message_text == "/status":
            await asyncio.to_thread(bot.send_message, chat_id=chat_id, text="✅ Bot is live and responding.")
        else:
            await asyncio.to_thread(bot.send_message, chat_id=chat_id, text="I only understand /status for now.")

    return {"ok": True}
