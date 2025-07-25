# =============================
# telegram_webhook.py — FINAL WEBHOOK VERSION (FastAPI Only)
# =============================

import os
import asyncio
from fastapi import FastAPI, Request
from dotenv import load_dotenv

from sniper_logic import start_sniper, start_sniper_with_forced_token, stop_all_tasks
from utils import is_bot_running, stop_bot, start_bot, send_telegram_alert

load_dotenv()

app = FastAPI()

@app.get("/")
async def root():
    return {"status": "Bot is running"}

@app.post("/start")
async def start():
    start_bot()
    await send_telegram_alert("▶️ Bot resumed via webhook.")
    return {"status": "Bot resumed"}

@app.post("/stop")
async def stop():
    stop_bot()
    await stop_all_tasks()
    return {"status": "Bot stopped and all tasks cancelled"}

@app.post("/launch")
async def launch():
    if is_bot_running():
        asyncio.create_task(start_sniper())
        return {"status": "Sniper bot launched"}
    else:
        return {"error": "Bot is inactive. Use /start first."}

@app.post("/forcebuy")
async def force_buy(request: Request):
    data = await request.json()
    token_mint = data.get("mint")
    if not token_mint:
        return {"error": "Missing mint address"}

    asyncio.create_task(start_sniper_with_forced_token(token_mint))
    return {"status": f"Force buy triggered for {token_mint}"}

@app.post("/reset")
async def reset():
    with open("sniped_tokens.txt", "w"): pass
    return {"status": "Sniped token list reset"}
