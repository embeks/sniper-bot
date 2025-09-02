# integrate_monster.py - MEDIUM VERSION WITH USEFUL FEATURES ONLY
"""
Optimized integration - Keeps useful features, removes broken/fake ones
Uses only utils.buy_token, includes caching and position sizing
"""

import asyncio
import os
import logging
import time
from typing import Dict, Optional, List
from dotenv import load_dotenv
from fastapi import FastAPI, Request
import uvicorn
import httpx
import certifi
from datetime import datetime, timedelta
from collections import deque

# Import core modules
from sniper_logic import (
    mempool_listener, trending_scanner, 
    pumpfun_migration_monitor, pumpfun_tokens, migration_watch_list,
    stop_all_tasks
)

# Import utils - USE ONLY buy_token
from utils import (
    buy_token,  # THE ONLY BUY FUNCTION WE USE
    send_telegram_alert,
    keypair, rpc,
    is_bot_running, start_bot, stop_bot, 
    get_wallet_summary, get_bot_status_message,
    get_liquidity_and_ownership,
    USE_DYNAMIC_SIZING, get_dynamic_position_size
)

load_dotenv()

# ============================================
# CONFIGURATION
# ============================================

def log_configuration():
    """Log configuration at startup"""
    config_items = [
        ("MOMENTUM_MIN_LIQUIDITY", os.getenv("MOMENTUM_MIN_LIQUIDITY", "500")),
        ("MOMENTUM_MIN_1H_GAIN", os.getenv("MOMENTUM_MIN_1H_GAIN", "30")),
        ("MIN_SCORE_AUTO_BUY", os.getenv("MIN_SCORE_AUTO_BUY", "2")),
        ("RUG_LP_THRESHOLD", os.getenv("RUG_LP_THRESHOLD", "1.0")),
        ("BUY_AMOUNT_SOL", os.getenv("BUY_AMOUNT_SOL", "0.03")),
        ("USE_DYNAMIC_SIZING", os.getenv("USE_DYNAMIC_SIZING", "true")),
        ("POOL_SCAN_LIMIT", os.getenv("POOL_SCAN_LIMIT", "20")),
        ("HELIUS_API", "SET" if os.getenv("HELIUS_API") else "NOT SET")
    ]
    
    logging.info("=" * 60)
    logging.info("BOT CONFIGURATION (OPTIMIZED)")
    logging.info("=" * 60)
    
    for key, value in config_items:
        logging.info(f"{key}: {value}")
    
    logging.info("BUY FUNCTION: utils.buy_token ONLY")
    logging.info("=" * 60)

# ============================================
# USEFUL FEATURES ONLY
# ============================================

class PoolCache:
    """Cache pool data to reduce API calls"""
    def __init__(self, ttl_seconds=60):
        self.cache = {}
        self.timestamps = {}
        self.ttl = ttl_seconds
    
    def get(self, mint: str) -> Optional[Dict]:
        """Get cached pool data if fresh"""
        if mint in self.cache:
            if time.time() - self.timestamps.get(mint, 0) < self.ttl:
                return self.cache[mint]
            else:
                # Clean up stale entry
                del self.cache[mint]
                del self.timestamps[mint]
        return None
    
    def set(self, mint: str, data: Dict):
        """Cache pool data"""
        self.cache[mint] = data
        self.timestamps[mint] = time.time()
    
    def cleanup(self):
        """Remove stale entries"""
        current_time = time.time()
        stale_mints = [
            mint for mint, timestamp in self.timestamps.items()
            if current_time - timestamp > self.ttl
        ]
        for mint in stale_mints:
            del self.cache[mint]
            del self.timestamps[mint]

class SimplePositionSizer:
    """Basic position sizing based on liquidity"""
    @staticmethod
    def get_size(pool_liquidity_sol: float, force_amount: Optional[float] = None) -> float:
        """Get position size based on pool liquidity"""
        if force_amount:
            return force_amount
        
        base_amount = float(os.getenv("BUY_AMOUNT_SOL", "0.03"))
        
        # Simple liquidity-based sizing
        if pool_liquidity_sol < 1:
            return 0.01  # Ultra small for tiny pools
        elif pool_liquidity_sol < 5:
            return min(base_amount, 0.02)
        elif pool_liquidity_sol < 20:
            return min(base_amount, 0.05)
        elif pool_liquidity_sol < 50:
            return min(base_amount, 0.1)
        else:
            return base_amount

class PerformanceTracker:
    """Track performance metrics without blocking trades"""
    def __init__(self):
        self.start_time = time.time()
        self.start_balance = None
        self.trades_executed = 0
        self.winning_trades = 0
        self.total_profit_sol = 0
        self.recent_trades = deque(maxlen=50)  # Last 50 trades
        self.daily_stats = {}
        self.reset_daily_stats()
    
    def reset_daily_stats(self):
        """Reset daily statistics"""
        today = datetime.now().date()
        if today not in self.daily_stats:
            self.daily_stats[today] = {
                "trades": 0,
                "wins": 0,
                "profit": 0
            }
    
    def record_trade(self, mint: str, amount: float, profit: float = 0):
        """Record a trade execution"""
        self.trades_executed += 1
        self.reset_daily_stats()
        today = datetime.now().date()
        
        self.daily_stats[today]["trades"] += 1
        
        if profit > 0:
            self.winning_trades += 1
            self.daily_stats[today]["wins"] += 1
        
        self.total_profit_sol += profit
        self.daily_stats[today]["profit"] += profit
        
        self.recent_trades.append({
            "mint": mint,
            "amount": amount,
            "profit": profit,
            "time": time.time()
        })
    
    def get_win_rate(self) -> float:
        """Calculate win rate"""
        if self.trades_executed == 0:
            return 0
        return (self.winning_trades / self.trades_executed) * 100
    
    def get_daily_summary(self) -> Dict:
        """Get today's performance"""
        today = datetime.now().date()
        if today in self.daily_stats:
            return self.daily_stats[today]
        return {"trades": 0, "wins": 0, "profit": 0}

# ============================================
# SMART BUY WRAPPER
# ============================================

async def smart_buy_token(mint: str, force_amount: Optional[float] = None) -> bool:
    """
    Wrapper around utils.buy_token that adds caching and position sizing
    This is NOT a duplicate buy function - it calls utils.buy_token
    """
    try:
        # Check cache first
        pool_data = pool_cache.get(mint)
        
        if not pool_data:
            # Get fresh data
            pool_data = await get_liquidity_and_ownership(mint)
            if pool_data:
                pool_cache.set(mint, pool_data)
        
        # Determine position size
        if USE_DYNAMIC_SIZING and not force_amount:
            # Use dynamic sizing from utils if enabled
            pool_liquidity = pool_data.get("liquidity", 0) if pool_data else 0
            amount = await get_dynamic_position_size(mint, pool_liquidity)
        else:
            # Use simple sizing
            pool_liquidity = pool_data.get("liquidity", 0) if pool_data else 0
            amount = position_sizer.get_size(pool_liquidity, force_amount)
        
        # Log the attempt
        logging.info(f"[BUY] Attempting {mint[:8]}... with {amount:.3f} SOL (LP: {pool_liquidity:.1f})")
        
        # Override environment variable temporarily
        original_amount = os.environ.get("BUY_AMOUNT_SOL")
        os.environ["BUY_AMOUNT_SOL"] = str(amount)
        
        # Call the actual buy function from utils
        result = await buy_token(mint)
        
        # Restore original amount
        if original_amount:
            os.environ["BUY_AMOUNT_SOL"] = original_amount
        
        # Track the trade
        if result:
            tracker.record_trade(mint, amount, 0)  # Profit tracked later
            await send_telegram_alert(
                f"✅ BUY SUCCESS\n"
                f"Token: {mint[:8]}...\n"
                f"Amount: {amount:.3f} SOL\n"
                f"Liquidity: {pool_liquidity:.1f} SOL"
            )
        
        return result
        
    except Exception as e:
        logging.error(f"[SMART BUY] Error: {e}")
        # Fallback to direct buy_token
        return await buy_token(mint)

# ============================================
# INITIALIZE COMPONENTS
# ============================================

pool_cache = PoolCache(ttl_seconds=60)
position_sizer = SimplePositionSizer()
tracker = PerformanceTracker()

# ============================================
# WEB SERVER
# ============================================

app = FastAPI()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
AUTHORIZED_USER_ID = int(os.getenv("TELEGRAM_USER_ID") or os.getenv("TELEGRAM_CHAT_ID", 0))

@app.get("/")
async def health_check():
    """Health check endpoint"""
    uptime_hours = (time.time() - tracker.start_time) / 3600
    return {
        "status": "✅ Bot Active",
        "trades": tracker.trades_executed,
        "win_rate": f"{tracker.get_win_rate():.1f}%",
        "profit": f"{tracker.total_profit_sol:.3f} SOL",
        "uptime": f"{uptime_hours:.1f} hours",
        "cached_pools": len(pool_cache.cache)
    }

@app.get("/status")
async def status():
    """Detailed status endpoint"""
    daily = tracker.get_daily_summary()
    return {
        "bot": "running" if is_bot_running() else "paused",
        "total_trades": tracker.trades_executed,
        "win_rate": f"{tracker.get_win_rate():.1f}%",
        "total_profit": f"{tracker.total_profit_sol:.3f} SOL",
        "today_trades": daily["trades"],
        "today_wins": daily["wins"],
        "today_profit": f"{daily['profit']:.3f} SOL",
        "cached_pools": len(pool_cache.cache),
        "pumpfun_tracking": len(pumpfun_tokens),
        "migration_watch": len(migration_watch_list)
    }

@app.post("/webhook")
@app.post("/")
async def telegram_webhook(request: Request):
    """Handle Telegram commands"""
    try:
        data = await request.json()
        message = data.get("message") or data.get("edited_message")
        if not message:
            return {"ok": True}
        
        user_id = message["from"]["id"]
        text = message.get("text", "")
        
        if user_id != AUTHORIZED_USER_ID:
            return {"ok": True}
        
        logging.info(f"[TELEGRAM] Command: {text}")
        
        if text == "/start":
            if is_bot_running():
                await send_telegram_alert("✅ Bot already running")
            else:
                start_bot()
                await send_telegram_alert("✅ Bot started! 💰")
                
        elif text == "/stop":
            if not is_bot_running():
                await send_telegram_alert("⏸ Bot already paused")
            else:
                stop_bot()
                await stop_all_tasks()
                await send_telegram_alert("🛑 Bot stopped")
                
        elif text == "/status":
            status_msg = get_bot_status_message()
            
            # Add performance metrics
            daily = tracker.get_daily_summary()
            perf_msg = f"\n📊 PERFORMANCE:\n"
            perf_msg += f"• Total Trades: {tracker.trades_executed}\n"
            perf_msg += f"• Win Rate: {tracker.get_win_rate():.1f}%\n"
            perf_msg += f"• Total Profit: {tracker.total_profit_sol:.3f} SOL\n"
            perf_msg += f"• Today: {daily['trades']} trades, {daily['wins']} wins\n"
            perf_msg += f"• Cached Pools: {len(pool_cache.cache)}\n"
            perf_msg += f"• PumpFun Tracking: {len(pumpfun_tokens)}"
            
            await send_telegram_alert(f"{status_msg}{perf_msg}")
                    
        elif text.startswith("/forcebuy "):
            parts = text.split(" ")
            if len(parts) >= 2:
                mint = parts[1].strip()
                amount = float(parts[2]) if len(parts) >= 3 else None
                
                await send_telegram_alert(f"🚨 Force buying: {mint[:8]}...")
                
                # Use smart buy with force amount
                result = await smart_buy_token(mint, amount)
                
                if result:
                    await send_telegram_alert("✅ Force buy successful!")
                else:
                    await send_telegram_alert("❌ Force buy failed")
                
        elif text == "/wallet":
            summary = get_wallet_summary()
            await send_telegram_alert(f"👛 Wallet:\n{summary}")
            
        elif text == "/launch":
            if is_bot_running():
                await send_telegram_alert("🚀 Launching snipers...")
                asyncio.create_task(start_bot_tasks())
            else:
                await send_telegram_alert("⛔ Bot paused. Use /start first")
            
        elif text == "/config":
            config_msg = f"""
⚙️ Configuration:
Min Liquidity: ${os.getenv('MOMENTUM_MIN_LIQUIDITY', '500')}
Min 1H Gain: {os.getenv('MOMENTUM_MIN_1H_GAIN', '30')}%
Auto-buy Score: {os.getenv('MIN_SCORE_AUTO_BUY', '2')}+
LP Threshold: {os.getenv('RUG_LP_THRESHOLD', '1.0')} SOL
Buy Amount: {os.getenv('BUY_AMOUNT_SOL', '0.03')} SOL
Dynamic Sizing: {USE_DYNAMIC_SIZING}
Cache TTL: {pool_cache.ttl}s
"""
            await send_telegram_alert(config_msg)
            
        elif text == "/recent":
            # Show recent trades
            if tracker.recent_trades:
                recent_msg = "📈 RECENT TRADES:\n"
                for trade in list(tracker.recent_trades)[-5:]:
                    recent_msg += f"• {trade['mint'][:8]}... | {trade['amount']:.3f} SOL"
                    if trade['profit'] != 0:
                        recent_msg += f" | P&L: {trade['profit']:+.3f}"
                    recent_msg += "\n"
                await send_telegram_alert(recent_msg)
            else:
                await send_telegram_alert("No recent trades")
            
        elif text == "/help":
            help_text = """
📚 Commands:
/start - Start bot
/stop - Stop bot
/status - Get detailed status
/wallet - Check wallet
/forcebuy <MINT> [amount] - Buy token
/launch - Launch all snipers
/config - Show configuration
/recent - Show recent trades
/help - Show this message
"""
            await send_telegram_alert(help_text)
            
        return {"ok": True}
        
    except Exception as e:
        logging.error(f"Webhook error: {e}")
        return {"ok": True}

# ============================================
# MAIN BOT LOGIC
# ============================================

async def start_bot_tasks():
    """Start all bot tasks"""
    
    log_configuration()
    
    # Initialize tracker balance
    try:
        balance = rpc.get_balance(keypair.pubkey()).value / 1e9
        tracker.start_balance = balance
        logging.info(f"Starting balance: {balance:.3f} SOL")
    except:
        pass
    
    await send_telegram_alert(
        "💰 BOT STARTING (OPTIMIZED) 💰\n\n"
        f"Configuration:\n"
        f"• Min Liquidity: ${os.getenv('MOMENTUM_MIN_LIQUIDITY', '500')}\n"
        f"• Min Gain: {os.getenv('MOMENTUM_MIN_1H_GAIN', '30')}%\n"
        f"• Auto-buy: Score {os.getenv('MIN_SCORE_AUTO_BUY', '2')}+\n"
        f"• LP Threshold: {os.getenv('RUG_LP_THRESHOLD', '1.0')} SOL\n"
        f"• Pool Cache: {pool_cache.ttl}s TTL\n\n"
        "Features:\n"
        "✅ Smart position sizing\n"
        "✅ Pool data caching\n"
        "✅ Performance tracking\n"
        "✅ Using utils.buy_token only\n\n"
        "Initializing..."
    )
    
    tasks = []
    
    # Core snipers
    tasks.extend([
        asyncio.create_task(mempool_listener("Raydium")),
        asyncio.create_task(mempool_listener("PumpFun")),
        asyncio.create_task(mempool_listener("Moonshot")),
        asyncio.create_task(trending_scanner())
    ])
    
    # Momentum Scanner
    try:
        from momentum_scanner import momentum_scanner
        if os.getenv("MOMENTUM_SCANNER", "true").lower() == "true":
            tasks.append(asyncio.create_task(momentum_scanner()))
            await send_telegram_alert(
                f"🔥 Momentum Scanner: ACTIVE\n"
                f"Targeting {os.getenv('MOMENTUM_MIN_1H_GAIN', '30')}-300% gainers"
            )
    except Exception as e:
        logging.warning(f"Momentum scanner not available: {e}")
    
    # PumpFun monitor
    if os.getenv("ENABLE_PUMPFUN_MIGRATION", "true").lower() == "true":
        tasks.append(asyncio.create_task(pumpfun_migration_monitor()))
        await send_telegram_alert("🎯 PumpFun Migration Monitor: ACTIVE")
    
    # DexScreener
    try:
        from dexscreener_monitor import start_dexscreener_monitor
        tasks.append(asyncio.create_task(start_dexscreener_monitor()))
        await send_telegram_alert("📊 DexScreener Monitor: ACTIVE")
    except:
        pass
    
    # Performance monitoring
    tasks.append(asyncio.create_task(performance_monitor()))
    
    # Cache cleanup
    tasks.append(asyncio.create_task(cache_cleanup()))
    
    await send_telegram_alert(
        f"🚀 BOT READY 🚀\n\n"
        f"Active Tasks: {len(tasks)}\n"
        f"Buy Function: utils.buy_token\n"
        f"Position Sizing: {'Dynamic' if USE_DYNAMIC_SIZING else 'Simple'}\n\n"
        f"Hunting for profits... 💰"
    )
    
    await asyncio.gather(*tasks)

async def performance_monitor():
    """Send hourly performance reports"""
    while True:
        await asyncio.sleep(3600)  # Every hour
        
        try:
            balance = rpc.get_balance(keypair.pubkey()).value / 1e9
            
            session_pnl = 0
            if tracker.start_balance:
                session_pnl = balance - tracker.start_balance
            
            daily = tracker.get_daily_summary()
            
            report = f"""
📊 HOURLY REPORT 📊

Session Stats:
• Trades: {tracker.trades_executed}
• Win Rate: {tracker.get_win_rate():.1f}%
• Total P&L: {tracker.total_profit_sol:+.3f} SOL
• Session P&L: {session_pnl:+.3f} SOL

Today's Stats:
• Trades: {daily['trades']}
• Wins: {daily['wins']}
• Profit: {daily['profit']:+.3f} SOL

Current Balance: {balance:.3f} SOL
Cached Pools: {len(pool_cache.cache)}
Uptime: {(time.time() - tracker.start_time) / 3600:.1f}h

Status: {"🟢 PROFITABLE" if session_pnl > 0 else "🔴 BUILDING"}
"""
            await send_telegram_alert(report)
            
        except Exception as e:
            logging.error(f"Performance monitor error: {e}")

async def cache_cleanup():
    """Periodically clean up stale cache entries"""
    while True:
        await asyncio.sleep(300)  # Every 5 minutes
        pool_cache.cleanup()
        logging.debug(f"Cache cleanup: {len(pool_cache.cache)} entries remaining")

# ============================================
# MAIN ENTRY
# ============================================

async def run_bot():
    """Run bot with web server"""
    asyncio.create_task(start_bot_tasks())
    
    if BOT_TOKEN:
        try:
            webhook_url = f"https://sniper-bot-web.onrender.com/webhook"
            async with httpx.AsyncClient(verify=certifi.where()) as client:
                await client.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
                    json={"url": webhook_url}
                )
                logging.info("Webhook set successfully")
        except Exception as e:
            logging.error(f"Webhook setup failed: {e}")
    
    port = int(os.getenv("PORT", 10000))
    config = uvicorn.Config(
        app, 
        host="0.0.0.0", 
        port=port,
        log_level="warning"
    )
    server = uvicorn.Server(config)
    
    logging.info(f"Starting web server on port {port}")
    await server.serve()

async def main():
    """Main entry point"""
    
    if not os.getenv("HELIUS_API"):
        print("ERROR: HELIUS_API not set")
        return
    
    if not os.getenv("SOLANA_PRIVATE_KEY"):
        print("ERROR: SOLANA_PRIVATE_KEY not set")
        return
    
    print("""
╔════════════════════════════════════════╗
║   OPTIMIZED BOT v2.0 (MEDIUM)         ║
║                                        ║
║  Features:                             ║
║  • Uses utils.buy_token only           ║
║  • Smart position sizing               ║
║  • Pool data caching (60s)            ║
║  • Performance tracking                ║
║  • No blocking risk management         ║
║  • Lower thresholds for more trades   ║
║                                        ║
║  Ready for profitable operation! 🚀    ║
╚════════════════════════════════════════╝
    """)
    
    logging.info("=" * 50)
    logging.info("STARTING OPTIMIZED BOT")
    logging.info("=" * 50)
    
    await run_bot()

# ============================================
# SIGNAL HANDLERS
# ============================================

import signal
import sys

def signal_handler(sig, frame):
    """Handle shutdown signals gracefully"""
    logging.info("Shutdown signal received, cleaning up...")
    asyncio.create_task(cleanup())
    sys.exit(0)

async def cleanup():
    """Clean up resources on shutdown"""
    try:
        await stop_all_tasks()
        
        # Final report
        if tracker.start_balance:
            try:
                balance = rpc.get_balance(keypair.pubkey()).value / 1e9
                session_pnl = balance - tracker.start_balance
                
                final_msg = (
                    f"📊 FINAL STATS\n"
                    f"Trades: {tracker.trades_executed}\n"
                    f"Win Rate: {tracker.get_win_rate():.1f}%\n"
                    f"Session P&L: {session_pnl:+.3f} SOL\n"
                    f"Final Balance: {balance:.3f} SOL"
                )
                await send_telegram_alert(final_msg)
            except:
                pass
    except:
        pass

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ============================================
# ENTRY POINT
# ============================================

if __name__ == "__main__":
    log_level = logging.DEBUG if os.getenv("DEBUG", "false").lower() == "true" else logging.INFO
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Bot stopped by user")
    except Exception as e:
        logging.error(f"Fatal error: {e}")
        import traceback
        traceback.print_exc()
