# =========================
# telegram_webhook.py — FastAPI Webhook Handler
# =========================

import os
import asyncio
from fastapi import FastAPI, Request
from dotenv import load_dotenv
from sniper_logic import start_sniper, start_sniper_with_forced_token, stop_all_tasks
from utils import send_telegram_alert, start_bot, stop_bot, is_bot_running

load_dotenv()

app = FastAPI()

@app.get("/")
async def root():
    return {"status": "Bot is running"}

@app.post("/")
async def telegram_webhook(request: Request):
    data = await request.json()
    message = data.get("message", {})
    text = message.get("text", "").strip()
    chat_id = message.get("chat", {}).get("id")

    if not text or not chat_id:
        return {"ok": False, "error": "Invalid Telegram data"}

    if text.startswith("/start"):
        start_bot()
        await send_telegram_alert("✅ Bot started.")
    elif text.startswith("/stop"):
        await stop_bot()
        await send_telegram_alert("🛑 Bot stopped.")
    elif text.startswith("/status"):
        status = "running ✅" if is_bot_running() else "stopped ⛔"
        await send_telegram_alert(f"📟 Bot status: {status}")
    elif text.startswith("/launch"):
        if is_bot_running():
            asyncio.create_task(start_sniper())
            await send_telegram_alert("🚀 Sniper launched.")
        else:
            await send_telegram_alert("❗ Use /start before launching sniper.")
    elif text.startswith("/forcebuy"):
        if not is_bot_running():
            await send_telegram_alert("❗ Use /start before force buying.")
            return {"ok": True}
        parts = text.split()
        if len(parts) == 2:
            mint = parts[1].strip()
            asyncio.create_task(start_sniper_with_forced_token(mint))
            await send_telegram_alert(f"🚨 Forced snipe triggered: `{mint}`")
        else:
            await send_telegram_alert("❌ Usage: /forcebuy <TOKEN_MINT>")
    else:
        await send_telegram_alert("❓ Unknown command.")

    return {"ok": True}
