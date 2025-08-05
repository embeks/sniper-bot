# =========================
# telegram_webhook.py — Memory Optimized Version
# =========================
from fastapi import FastAPI, Request
import asyncio
import os
import gc  # Garbage collection
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
AUTHORIZED_USER_ID = int(os.getenv("TELEGRAM_USER_ID"))

# Lazy imports to reduce memory at startup
sniper_logic = None
utils = None

def lazy_import():
    """Import heavy modules only when needed"""
    global sniper_logic, utils
    if sniper_logic is None:
        from sniper_logic import start_sniper, start_sniper_with_forced_token, stop_all_tasks
        sniper_logic = type('', (), {
            'start_sniper': start_sniper,
            'start_sniper_with_forced_token': start_sniper_with_forced_token,
            'stop_all_tasks': stop_all_tasks
        })()
    if utils is None:
        from utils import send_telegram_alert, is_bot_running, start_bot, stop_bot, get_wallet_summary, get_bot_status_message
        utils = type('', (), {
            'send_telegram_alert': send_telegram_alert,
            'is_bot_running': is_bot_running,
            'start_bot': start_bot,
            'stop_bot': stop_bot,
            'get_wallet_summary': get_wallet_summary,
            'get_bot_status_message': get_bot_status_message
        })()

# ✅ Command Router
@app.post("/")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        message = data.get("message") or data.get("edited_message")
        if not message:
            return {"ok": True}
        
        chat_id = message["chat"]["id"]
        user_id = message["from"]["id"]
        text = message.get("text", "")
        
        # Only allow messages from the authorized user
        if user_id != AUTHORIZED_USER_ID:
            return {"ok": True}
        
        # Import modules when first command is received
        lazy_import()
        
        # Parse commands
        if text == "/start":
            if utils.is_bot_running():
                await utils.send_telegram_alert("✅ Bot already running.")
            else:
                utils.start_bot()
                await utils.send_telegram_alert("✅ Bot is now active.")
                
        elif text == "/stop":
            if not utils.is_bot_running():
                await utils.send_telegram_alert("⏸ Bot already paused.")
            else:
                utils.stop_bot()
                await sniper_logic.stop_all_tasks()
                await utils.send_telegram_alert("🛑 Bot stopped.")
                # Force garbage collection
                gc.collect()
                
        elif text == "/status":
            status_msg = utils.get_bot_status_message()
            await utils.send_telegram_alert(f"📊 Status:\n{status_msg}")
            
        elif text == "/launch":
            if utils.is_bot_running():
                asyncio.create_task(sniper_logic.start_sniper())
                await utils.send_telegram_alert("🚀 Sniper launched.")
            else:
                await utils.send_telegram_alert("⛔ Bot is paused. Use /start first.")
                
        elif text.startswith("/forcebuy "):
            parts = text.split(" ")
            if len(parts) == 2:
                mint = parts[1].strip()
                await utils.send_telegram_alert(f"🚨 Force buying: {mint}")
                asyncio.create_task(sniper_logic.start_sniper_with_forced_token(mint))
            else:
                await utils.send_telegram_alert("❌ Invalid format. Use /forcebuy <MINT>")
                
        elif text == "/wallet":
            summary = utils.get_wallet_summary()
            await utils.send_telegram_alert(f"👛 Wallet:\n{summary}")
            
        elif text == "/memory":
            # Debug command to check memory usage
            import psutil
            process = psutil.Process()
            memory_mb = process.memory_info().rss / 1024 / 1024
            await utils.send_telegram_alert(f"💾 Memory usage: {memory_mb:.1f} MB")
            
        else:
            await utils.send_telegram_alert("🤖 Unknown command.")
            
        return {"ok": True}
        
    except Exception as e:
        print(f"Error in webhook: {e}")
        return {"ok": True}

@app.on_event("startup")
async def startup_event():
    """Minimal startup to save memory"""
    print("Bot webhook started - waiting for commands...")

@app.get("/health")
async def health_check():
    """Health check endpoint for Render"""
    return {"status": "ok"}
