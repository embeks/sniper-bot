# =============================
# telegram_webhook.py — Elite Bot Webhook Entry with Command Bot Support
# =============================

import os
import asyncio
import threading
from fastapi import FastAPI, Request
from dotenv import load_dotenv
from sniper_logic import start_sniper, start_sniper_with_forced_token
from utils import is_bot_running, stop_bot, start_bot, start_command_bot

load_dotenv()
app = FastAPI()

@app.get("/")
async def root():
    return {"status": "Bot is running"}

@app.post("/forcebuy")
async def force_buy(request: Request):
    data = await request.json()
    token_mint = data.get("mint")
    if not token_mint:
        return {"error": "Missing mint address"}
    
    asyncio.create_task(start_sniper_with_forced_token(token_mint))
    return {"status": f"Force buy triggered for {token_mint}"}

@app.post("/start")
async def start():
    start_bot()
    return {"status": "Bot resumed"}

@app.post("/stop")
async def stop():
    stop_bot()
    return {"status": "Bot stopped"}

@app.post("/launch")
async def launch():
    if is_bot_running():
        asyncio.create_task(start_sniper())
        return {"status": "Sniper bot launched"}
    else:
        return {"error": "Bot is inactive. Use /start to activate."}

# ✅ Start Telegram Command Bot in Background
def launch_command_bot():
    asyncio.run(start_command_bot())

threading.Thread(target=launch_command_bot, daemon=True).start()
