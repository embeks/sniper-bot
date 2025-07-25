from fastapi import FastAPI, Request
import os
import asyncio
import telegram

from sniper_logic import start_sniper, start_sniper_with_forced_token
from utils import get_wallet_status_message  # Ensure this is implemented in utils.py

app = FastAPI()
bot = telegram.Bot(token=os.getenv("TELEGRAM_BOT_TOKEN"))

# Global reference to sniper task
sniper_task = None

@app.post("/webhook")
async def handle_webhook(request: Request):
    global sniper_task
    data = await request.json()
    update = telegram.Update.de_json(data, bot)

    if update.message and update.message.chat:
        chat_id = update.message.chat.id
        message_text = update.message.text.strip()

        if message_text.startswith("/forcebuy"):
            try:
                mint = message_text.split(" ")[1]
                await start_sniper_with_forced_token(mint)
                bot.send_message(chat_id=chat_id, text=f"🚀 Forced buy triggered for {mint}")
            except Exception as e:
                bot.send_message(chat_id=chat_id, text=f"❌ Error: {e}")

        elif message_text == "/start":
            if sniper_task and not sniper_task.done():
                bot.send_message(chat_id=chat_id, text="⚠️ Sniper already running.")
            else:
                sniper_task = asyncio.create_task(start_sniper())
                bot.send_message(chat_id=chat_id, text="🟢 Sniper bot launched.")

        elif message_text == "/stop":
            if sniper_task and not sniper_task.done():
                sniper_task.cancel()
                bot.send_message(chat_id=chat_id, text="🛑 Sniper bot stopped.")
            else:
                bot.send_message(chat_id=chat_id, text="⚠️ Sniper is not running.")

        elif message_text == "/status":
            try:
                status = await get_wallet_status_message()
                bot.send_message(chat_id=chat_id, text=status)
            except Exception as e:
                bot.send_message(chat_id=chat_id, text=f"❌ Status check failed: {e}")

        else:
            bot.send_message(chat_id=chat_id, text=(
                "🤖 Commands:\n"
                "/start — Launch sniper\n"
                "/stop — Stop sniper\n"
                "/forcebuy <TOKEN_MINT> — Force buy\n"
                "/status — Wallet + Sniper status"
            ))

    return {"ok": True}

@app.on_event("startup")
async def launch_sniper_bot():
    global sniper_task
    sniper_task = asyncio.create_task(start_sniper())
