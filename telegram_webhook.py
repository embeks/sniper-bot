# telegram_webhook.py 
# =========================
from fastapi import FastAPI, Request
import asyncio
import os
import logging
from dotenv import load_dotenv
# Direct imports - no lazy loading
from sniper_logic import start_sniper, start_sniper_with_forced_token, stop_all_tasks
from utils import send_telegram_alert, is_bot_running, start_bot, stop_bot, get_wallet_summary, get_bot_status_message
load_dotenv()
app = FastAPI()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
AUTHORIZED_USER_ID = int(os.getenv("TELEGRAM_USER_ID") or os.getenv("TELEGRAM_CHAT_ID", 0))

# FIX: Add task state tracking to prevent duplicate sniper tasks
SNIPER_RUNNING = False

# ✅ Command Router
@app.post("/")
async def telegram_webhook(request: Request):
    global SNIPER_RUNNING
    
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
                SNIPER_RUNNING = False  # Reset sniper flag when stopping
                await stop_all_tasks()
                await send_telegram_alert("🛑 Bot stopped.")
                
        elif text == "/status":
            status_msg = get_bot_status_message()
            sniper_status = "🟢 Running" if SNIPER_RUNNING else "🔴 Stopped"
            await send_telegram_alert(f"📊 Status:\n{status_msg}\n🎯 Sniper: {sniper_status}")
            
        elif text == "/launch":
            if not is_bot_running():
                await send_telegram_alert("⛔ Bot is paused. Use /start first.")
            elif SNIPER_RUNNING:
                await send_telegram_alert("⚠️ Sniper already running! Use /stop first to restart.")
            else:
                try:
                    SNIPER_RUNNING = True
                    asyncio.create_task(start_sniper())
                    await send_telegram_alert("🚀 Sniper launched.")
                except Exception as e:
                    SNIPER_RUNNING = False
                    await send_telegram_alert(f"❌ Launch failed: {str(e)[:100]}")
                
        elif text.startswith("/forcebuy "):
            if not is_bot_running():
                await send_telegram_alert("⛔ Bot is paused. Use /start first.")
            else:
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
        
        elif text == "/ping":
            # Simple ping command for testing
            await send_telegram_alert("🏓 Pong! Commands are working!")
            
        elif text == "/restart":
            # Restart sniper (stop then start)
            if not is_bot_running():
                await send_telegram_alert("⛔ Bot is paused. Use /start first.")
            else:
                try:
                    SNIPER_RUNNING = False
                    await stop_all_tasks()
                    await asyncio.sleep(2)  # Brief pause
                    SNIPER_RUNNING = True
                    asyncio.create_task(start_sniper())
                    await send_telegram_alert("🔄 Sniper restarted successfully!")
                except Exception as e:
                    SNIPER_RUNNING = False
                    await send_telegram_alert(f"❌ Restart failed: {str(e)[:100]}")
            
        elif text == "/help":
            # Help command
            help_text = """
📚 Available Commands:
/start - Start the bot
/stop - Stop the bot
/status - Get bot status
/wallet - Check wallet balance
/forcebuy <MINT> - Force buy a token
/launch - Launch sniper
/restart - Restart sniper
/memory - Check memory usage
/ping - Test commands
/help - Show this message
"""
            await send_telegram_alert(help_text)
            
        else:
            await send_telegram_alert("🤖 Unknown command. Try /help")
            
        return {"ok": True}
        
    except Exception as e:
        print(f"Error in webhook: {e}")
        logging.error(f"Webhook error: {e}")
        return {"ok": True}

@app.on_event("startup")
async def startup_event():
    """Minimal startup to save memory"""
    print("Bot webhook started - waiting for commands...")

@app.on_event("shutdown")
async def shutdown_event():
    """Clean shutdown"""
    global SNIPER_RUNNING
    SNIPER_RUNNING = False
    try:
        await stop_all_tasks()
    except:
        pass

@app.get("/health")
async def health_check():
    """Health check endpoint for Render"""
    global SNIPER_RUNNING
    return {
        "status": "ok",
        "sniper_running": SNIPER_RUNNING,
        "bot_running": is_bot_running()
    }

# ============================================
# NEW FUNCTION FOR MONSTER BOT INTEGRATION
# ============================================

async def start_telegram_webhook():
    """
    Function to start webhook handler when called from monster bot.
    This allows the webhook to be imported and run from integrate_monster.py
    """
    logging.info("[TELEGRAM] Webhook handler activated from monster bot")
    
    # The FastAPI app is already configured above
    # This function just keeps the webhook alive when called
    # The actual webhook endpoints are handled by FastAPI
    
    while True:
        # Keep the webhook task alive
        await asyncio.sleep(60)
        # Optional: Could add health checks or status updates here
        
# This allows the module to work both standalone and when imported
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
