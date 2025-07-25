# =============================
# telegram_webhook.py — Final Version (Slash Command Controlled)
# =============================

import os
import asyncio
from fastapi import FastAPI, Request
from dotenv import load_dotenv

from sniper_logic import (
    start_sniper,
    stop_all_tasks,
    start_sniper_with_forced_token
)
from utils import is_bot_running, stop_bot, start_bot

load_dotenv()

app = FastAPI()

@app.get("/")
async def root():
    return {"status": "Sniper Bot is Live"}

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
    return {"status": "Bot resumed. Use /launch to activate sniper."}

@app.post("/stop")
async def stop():
    stop_bot()
    await stop_all_tasks()
    return {"status": "Bot paused. All tasks canceled."}

@app.post("/launch")
async def launch():
    if is_bot_running():
        asyncio.create_task(start_sniper())
        return {"status": "Sniper bot launched"}
    else:
        return {"error": "Bot is paused. Use /start to resume first."}
