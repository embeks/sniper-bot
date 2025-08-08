# telegram_webhook.py 
# =========================
from fastapi import FastAPI, Request
import asyncio
import os
from dotenv import load_dotenv
# Direct imports - no lazy loading
from sniper_logic import start_sniper, start_sniper_with_forced_token, stop_all_tasks
from utils import send_telegram_alert, is_bot_running, start_bot, stop_bot, get_wallet_summary, get_bot_status_message

load_dotenv()

app = FastAPI()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
AUTHORIZED_USER_ID = int(os.getenv("TELEGRAM_USER_ID") or os.getenv("TELEGRAM_CHAT_ID", 0))

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
        
        # Parse commands
        if text == "/start":
            if is_bot_running():
                await send_telegram_alert("✅ Bot already running.")
            else:
                start_bot()
                await send_telegram_alert("✅ Bot is now active.")
                
        elif text == "/stop":
            if not is_bot_running():
                await send_telegram_alert("⏸ Bot already paused.")
            else:
                stop_bot()
                await stop_all_tasks()
                await send_telegram_alert("🛑 Bot stopped.")
                
        elif text == "/status":
            status_msg = get_bot_status_message()
            await send_telegram_alert(f"📊 Status:\n{status_msg}")
            
        elif text == "/launch":
            if is_bot_running():
                asyncio.create_task(start_sniper())
                await send_telegram_alert("🚀 Sniper launched.")
            else:
                await send_telegram_alert("⛔ Bot is paused. Use /start first.")
                
        elif text.startswith("/forcebuy "):
            parts = text.split(" ")
            if len(parts) == 2:
                mint = parts[1].strip()
                await send_telegram_alert(f"🚨 Force buying: {mint}")
                asyncio.create_task(start_sniper_with_forced_token(mint))
            else:
                await send_telegram_alert("❌ Invalid format. Use /forcebuy <MINT>")
                
        elif text == "/wallet":
            summary = get_wallet_summary()
            await send_telegram_alert(f"👛 Wallet:\n{summary}")
            
        elif text == "/memory":
            # Debug command to check memory usage
            try:
                import psutil
                process = psutil.Process()
                memory_mb = process.memory_info().rss / 1024 / 1024
                await send_telegram_alert(f"💾 Memory usage: {memory_mb:.1f} MB")
            except:
                await send_telegram_alert("💾 Memory check not available")
            
        else:
            await send_telegram_alert("🤖 Unknown command.")
            
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
