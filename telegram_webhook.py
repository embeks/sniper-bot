# =========================
# telegram_webhook.py — Webhook Listener (FastAPI Architecture)
# =========================

from fastapi import FastAPI, Request
import asyncio
import os
from sniper_logic import start_sniper, start_sniper_with_forced_token, stop_all_tasks
from utils import send_telegram_message, is_bot_running, set_bot_status, get_wallet_summary

app = FastAPI()

BOT_TOKEN = os.getenv("BOT_TOKEN")
AUTHORIZED_USER_ID = int(os.getenv("TELEGRAM_USER_ID"))

# ✅ Command Router
@app.post("/")
async def telegram_webhook(request: Request):
    data = await request.json()
    message = data.get("message") or data.get("edited_message")
    if not message:
        return {"ok": True}

    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    text = message.get("text", "")

    # Only allow messages from the authorized user
    if user_id != AUTHORIZED_USER_ID:
        await send_telegram_message(chat_id, "❌ Unauthorized.")
        return {"ok": True}

    # Parse commands
    if text == "/start":
        if is_bot_running():
            await send_telegram_message(chat_id, "✅ Bot already running.")
        else:
            set_bot_status(True)
            await send_telegram_message(chat_id, "✅ Bot is now active.")
    elif text == "/stop":
        if not is_bot_running():
            await send_telegram_message(chat_id, "⏸ Bot already paused.")
        else:
            set_bot_status(False)
            await stop_all_tasks()
    elif text == "/status":
        status = "▶️ RUNNING" if is_bot_running() else "⏸ PAUSED"
        await send_telegram_message(chat_id, f"📊 Bot Status: {status}")
    elif text == "/launch":
        if is_bot_running():
            asyncio.create_task(start_sniper())
            await send_telegram_message(chat_id, "🚀 Sniper launched.")
        else:
            await send_telegram_message(chat_id, "⛔ Bot is paused. Use /start first.")
    elif text.startswith("/forcebuy "):
        parts = text.split(" ")
        if len(parts) == 2:
            mint = parts[1].strip()
            await send_telegram_message(chat_id, f"🚨 Force buying: {mint}")
            asyncio.create_task(start_sniper_with_forced_token(mint))
        else:
            await send_telegram_message(chat_id, "❌ Invalid format. Use /forcebuy <MINT>")
    elif text == "/wallet":
        summary = await get_wallet_summary()
        await send_telegram_message(chat_id, f"👛 Wallet:\n{summary}")
    else:
        await send_telegram_message(chat_id, "🤖 Unknown command.")

    return {"ok": True}
