from fastapi import FastAPI, Request
import os
import telegram
import logging
import asyncio

from sniper_logic import start_sniper_with_forced_token

app = FastAPI()
bot = telegram.Bot(token=os.getenv("TELEGRAM_BOT_TOKEN"))

@app.post("/webhook")
async def handle_webhook(request: Request):
    data = await request.json()
    update = telegram.Update.de_json(data, bot)

    if update.message and update.message.chat:
        chat_id = update.message.chat.id
        message_text = update.message.text

        if message_text == "/status":
            await bot.send_message(chat_id=chat_id, text="✅ Bot is live and responding.")

        elif message_text.startswith("/forcebuy"):
            parts = message_text.split()
            if len(parts) == 2:
                token_mint = parts[1].strip()
                await bot.send_message(chat_id=chat_id, text=f"🚨 Force-buying token: `{token_mint}`", parse_mode="Markdown")
                
                # Launch sniper logic
                asyncio.create_task(start_sniper_with_forced_token(token_mint))
            else:
                await bot.send_message(chat_id=chat_id, text="⚠️ Usage: /forcebuy <TOKEN_MINT>")

    return {"ok": True}
