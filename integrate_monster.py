# integrate_monster.py - COMPLETE WITH STOP-LOSS INTEGRATION
"""
Optimized integration - Uses config.py, no env mutations
Uses only utils.buy_token with explicit amounts
Arms stop-loss immediately after successful buy
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

# Import config
import config

# Import core modules
from sniper_logic import (
    mempool_listener, trending_scanner, 
    pumpfun_migration_monitor, pumpfun_tokens, migration_watch_list,
    stop_all_tasks
)

# Import utils - USE ONLY buy_token and register_stop
from utils import (
    buy_token,  # THE ONLY BUY FUNCTION WE USE
    register_stop,  # ARM STOP-LOSS AFTER BUY
    notify,  # NEW GATED NOTIFICATION FUNCTION
    send_telegram_alert,  # For startup messages only
    keypair, rpc,
    is_bot_running, start_bot, stop_bot, 
    get_wallet_summary, get_bot_status_message,
    get_liquidity_and_ownership,
    get_dynamic_position_size,
    get_token_price_usd,
    STOPS  # Access to stop-loss tracking
)

load_dotenv()

# Load config once
CONFIG = config.load()

# ============================================
# CONFIGURATION
# ============================================

def log_configuration():
    """Log configuration at startup"""
    config_items = [
        ("MOMENTUM_MIN_LIQUIDITY", os.getenv("MOMENTUM_MIN_LIQUIDITY", "500")),
        ("MOMENTUM_MIN_1H_GAIN", os.getenv("MOMENTUM_MIN_1H_GAIN", "30")),
        ("MIN_SCORE_AUTO_BUY", os.getenv("MIN_SCORE_AUTO_BUY", "2")),
        ("RUG_LP_THRESHOLD", str(CONFIG.RUG_LP_THRESHOLD)),
        ("BUY_AMOUNT_SOL", str(CONFIG.BUY_AMOUNT_SOL)),
        ("USE_DYNAMIC_SIZING", str(CONFIG.USE_DYNAMIC_SIZING)),
        ("MIN_LP_SOL", str(CONFIG.MIN_LP_SOL)),
        ("STOP_LOSS_PCT", f"{CONFIG.STOP_LOSS_PCT*100:.0f}%"),
        ("STOP_CHECK_INTERVAL", f"{CONFIG.STOP_CHECK_INTERVAL_SEC}s"),
        ("REQUIRE_AUTH_RENOUNCED", str(CONFIG.REQUIRE_AUTH_RENOUNCED)),
        ("MAX_TRADE_TAX_BPS", f"{CONFIG.MAX_TRADE_TAX_BPS/100:.1f}%"),
        ("HELIUS_API", "SET" if os.getenv("HELIUS_API") else "NOT SET"),
        ("ALERTS", f"Buy: {CONFIG.ALERTS_NOTIFY['buy']}, Sell: {CONFIG.ALERTS_NOTIFY['sell']}, Stop: {CONFIG.ALERTS_NOTIFY['stop_triggered']}/{CONFIG.ALERTS_NOTIFY['stop_filled']}")
    ]
    
    logging.info("=" * 60)
    logging.info("BOT CONFIGURATION (WITH STOP-LOSS ENGINE)")
    logging.info("=" * 60)
    
    for key, value in config_items:
        logging.info(f"{key}: {value}")
    
    logging.info("BUY FUNCTION: utils.buy_token with pre-trade validation")
    logging.info("STOP-LOSS: Armed immediately on buy")
    logging.info("ALERTS: Using gated notify() with cooldowns")
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
        
        base_amount = CONFIG.BUY_AMOUNT_SOL
        
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
                "profit": 0,
                "stops_hit": 0
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
        elif profit < 0:
            self.daily_stats[today]["stops_hit"] += 1
        
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
        return {"trades": 0, "wins": 0, "profit": 0, "stops_hit": 0}

# ============================================
# SMART BUY WRAPPER WITH STOP-LOSS
# ============================================

async def smart_buy_token(mint: str, amount: Optional[float] = None) -> bool:
    """
    Wrapper around utils.buy_token that adds caching, position sizing, and stop-loss
    """
    try:
        # Check cache first
        pool_data = pool_cache.get(mint)
        
        if not pool_data:
            # Get fresh data
            pool_data = await get_liquidity_and_ownership(mint)
            if pool_data:
                pool_cache.set(mint, pool_data)
        
        pool_liquidity = pool_data.get("liquidity", 0) if pool_data else 0
        
        # Determine position size if not provided
        if amount is None:
            if CONFIG.USE_DYNAMIC_SIZING:
                # Use dynamic sizing from utils if enabled
                amount = await get_dynamic_position_size(mint, pool_liquidity)
            else:
                # Use simple sizing
                amount = position_sizer.get_size(pool_liquidity)
        
        # Log the attempt with explicit amount
        logging.info(f"[SMART BUY] Attempting {mint[:8]}... with explicit amount: {amount:.3f} SOL (LP: {pool_liquidity:.1f})")
        
        # Call the actual buy function from utils with explicit amount
        # NOTE: buy_token now handles pre-trade validation and stop-loss registration internally
        result = await buy_token(mint, amount=amount)
        
        # Track the trade
        if result:
            tracker.record_trade(mint, amount, 0)  # Profit tracked later
            
            # Additional success notification if needed (buy_token already notifies)
            logging.info(f"[SMART BUY] Success! Stop-loss armed for {mint[:8]}...")
        
        return result
        
    except Exception as e:
        logging.error(f"[SMART BUY] Error: {e}")
        # Fallback to direct buy_token with explicit amount
        fallback_amount = amount if amount is not None else CONFIG.BUY_AMOUNT_SOL
        return await buy_token(mint, amount=fallback_amount)

# ============================================
# FORCEBUY WITH STOP-LOSS
# ============================================

async def handle_forcebuy(mint: str, amount: Optional[float] = None) -> bool:
    """Handle forcebuy with stop-loss registration"""
    try:
        # Use smart buy which calls buy_token with stop-loss
        result = await smart_buy_token(mint, amount=amount)
        
        if result:
            # Check if stop was armed
            if mint in STOPS:
                stop_data = STOPS[mint]
                logging.info(f"Force buy successful with stop-loss @ ${stop_data['stop_price']:.6f}")
            else:
                logging.info(f"Force buy successful! Amount: {amount or CONFIG.BUY_AMOUNT_SOL} SOL")
        else:
            logging.error("Force buy failed")
        
        return result
        
    except Exception as e:
        logging.error(f"Forcebuy error: {e}")
        return False

# ============================================
# INITIALIZE COMPONENTS
# ============================================

pool_cache = PoolCache(ttl_seconds=CONFIG.CACHE_TTL_SECONDS)
position_sizer = SimplePositionSizer()
tracker = PerformanceTracker()
risk_manager = None  # Placeholder for risk manager if needed

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
    daily = tracker.get_daily_summary()
    return {
        "status": "✅ Bot Active with Stop-Loss Engine",
        "trades": tracker.trades_executed,
        "win_rate": f"{tracker.get_win_rate():.1f}%",
        "profit": f"{tracker.total_profit_sol:.3f} SOL",
        "stops_hit_today": daily["stops_hit"],
        "active_stops": len(STOPS),
        "uptime": f"{uptime_hours:.1f} hours",
        "cached_pools": len(pool_cache.cache),
        "cache_ttl": CONFIG.CACHE_TTL_SECONDS
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
        "today_stops": daily["stops_hit"],
        "active_stops": len(STOPS),
        "stop_loss_pct": f"{CONFIG.STOP_LOSS_PCT*100:.0f}%",
        "cached_pools": len(pool_cache.cache),
        "cache_ttl": CONFIG.CACHE_TTL_SECONDS,
        "pumpfun_tracking": len(pumpfun_tokens),
        "migration_watch": len(migration_watch_list),
        "buy_amount": CONFIG.BUY_AMOUNT_SOL,
        "min_lp_sol": CONFIG.MIN_LP_SOL,
        "dynamic_sizing": CONFIG.USE_DYNAMIC_SIZING,
        "alerts_enabled": {
            "buy": CONFIG.ALERTS_NOTIFY.get("buy", False),
            "sell": CONFIG.ALERTS_NOTIFY.get("sell", False),
            "stop": CONFIG.ALERTS_NOTIFY.get("stop_triggered", False)
        }
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
                await send_telegram_alert("✅ Bot already running with stop-loss protection")
            else:
                start_bot()
                await send_telegram_alert("✅ Bot started with stop-loss engine! 💰🛑")
                
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
            perf_msg += f"• Today: {daily['trades']} trades, {daily['wins']} wins, {daily['stops_hit']} stops\n"
            perf_msg += f"• Active Stops: {len(STOPS)}\n"
            perf_msg += f"• Stop-Loss: {CONFIG.STOP_LOSS_PCT*100:.0f}% @ {CONFIG.STOP_CHECK_INTERVAL_SEC}s checks\n"
            perf_msg += f"• Cached Pools: {len(pool_cache.cache)} (TTL: {CONFIG.CACHE_TTL_SECONDS}s)\n"
            perf_msg += f"• PumpFun Tracking: {len(pumpfun_tokens)}"
            
            await send_telegram_alert(f"{status_msg}{perf_msg}")
                    
        elif text.startswith("/forcebuy "):
            parts = text.split(" ")
            if len(parts) >= 2:
                mint = parts[1].strip()
                amount = float(parts[2]) if len(parts) >= 3 else None
                
                logging.info(f"Force buying: {mint[:8]}... with amount: {amount or 'default'}")
                
                # Use smart buy with stop-loss
                result = await handle_forcebuy(mint, amount=amount)
                
                if not result:
                    await send_telegram_alert("❌ Force buy failed")
                
        elif text == "/wallet":
            summary = get_wallet_summary()
            await send_telegram_alert(f"👛 Wallet:\n{summary}")
            
        elif text == "/stops":
            # Show active stop-losses
            if STOPS:
                stops_msg = "🛑 ACTIVE STOP-LOSSES:\n"
                for mint, stop_data in list(STOPS.items())[:10]:
                    stops_msg += f"• {mint[:8]}... - State: {stop_data['state']}, Stop: ${stop_data['stop_price']:.6f}"
                    if stop_data.get('stuck_reason'):
                        stops_msg += f" ({stop_data['stuck_reason']})"
                    stops_msg += "\n"
                await send_telegram_alert(stops_msg)
            else:
                await send_telegram_alert("No active stop-losses")
            
        elif text == "/launch":
            if is_bot_running():
                await send_telegram_alert("🚀 Launching snipers with stop-loss protection...")
                asyncio.create_task(start_bot_tasks())
            else:
                await send_telegram_alert("⛔ Bot paused. Use /start first")
            
        elif text == "/config":
            config_msg = f"""
⚙️ Configuration:
Min Liquidity: {CONFIG.MIN_LP_SOL} SOL
Min LP (Momentum): ${os.getenv('MOMENTUM_MIN_LIQUIDITY', '500')}
Min 1H Gain: {os.getenv('MOMENTUM_MIN_1H_GAIN', '30')}%
Auto-buy Score: {os.getenv('MIN_SCORE_AUTO_BUY', '2')}+
LP Threshold: {CONFIG.RUG_LP_THRESHOLD} SOL
Buy Amount: {CONFIG.BUY_AMOUNT_SOL} SOL
Dynamic Sizing: {CONFIG.USE_DYNAMIC_SIZING}

Pre-Trade Safety:
• Require Auth Renounced: {CONFIG.REQUIRE_AUTH_RENOUNCED}
• Max Tax: {CONFIG.MAX_TRADE_TAX_BPS/100:.1f}%

Stop-Loss Engine:
• Stop-Loss: {CONFIG.STOP_LOSS_PCT*100:.0f}%
• Check Interval: {CONFIG.STOP_CHECK_INTERVAL_SEC}s
• Max Slippage: {CONFIG.STOP_MAX_SLIPPAGE_BPS/100:.1f}%
• Emergency: {CONFIG.STOP_EMERGENCY_SLIPPAGE_BPS/100:.1f}%

Alerts:
• Buy: {CONFIG.ALERTS_NOTIFY.get('buy', False)}
• Sell: {CONFIG.ALERTS_NOTIFY.get('sell', False)}
• Stop Trigger: {CONFIG.ALERTS_NOTIFY.get('stop_triggered', False)}
• Stop Fill: {CONFIG.ALERTS_NOTIFY.get('stop_filled', False)}
• Cooldown: {CONFIG.ALERTS_NOTIFY.get('cooldown_secs', 60)}s

Cache TTL: {CONFIG.CACHE_TTL_SECONDS}s
Force Jupiter: {CONFIG.FORCE_JUPITER_SELL}
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
/start - Start bot with stop-loss
/stop - Stop bot
/status - Get detailed status
/wallet - Check wallet
/forcebuy <MINT> [amount] - Buy with stop-loss
/stops - View active stop-losses
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
    
    # Single startup message
    if CONFIG.ALERTS_NOTIFY.get("startup", True):
        await send_telegram_alert(
            "💰 BOT STARTING WITH STOP-LOSS ENGINE 💰\n\n"
            f"Configuration:\n"
            f"• Min LP: {CONFIG.MIN_LP_SOL} SOL\n"
            f"• Stop-Loss: {CONFIG.STOP_LOSS_PCT*100:.0f}%\n"
            f"• Buy Amount: {CONFIG.BUY_AMOUNT_SOL} SOL\n"
            f"• Alerts: Buy={CONFIG.ALERTS_NOTIFY['buy']}, Sell={CONFIG.ALERTS_NOTIFY['sell']}, Stop={CONFIG.ALERTS_NOTIFY['stop_triggered']}\n\n"
            "Features:\n"
            "✅ Pre-trade validation\n"
            "✅ Automatic stop-loss on buy\n"
            "✅ Minimal alerts (cooldown: {CONFIG.ALERTS_NOTIFY.get('cooldown_secs', 60)}s)\n"
            "✅ Smart position sizing\n\n"
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
            logging.info(f"Momentum Scanner: ACTIVE (targeting {os.getenv('MOMENTUM_MIN_1H_GAIN', '30')}-300% gainers)")
    except Exception as e:
        logging.warning(f"Momentum scanner not available: {e}")
    
    # PumpFun monitor
    if os.getenv("ENABLE_PUMPFUN_MIGRATION", "true").lower() == "true":
        tasks.append(asyncio.create_task(pumpfun_migration_monitor()))
        logging.info("PumpFun Migration Monitor: ACTIVE")
    
    # DexScreener
    try:
        from dexscreener_monitor import start_dexscreener_monitor
        tasks.append(asyncio.create_task(start_dexscreener_monitor()))
        logging.info("DexScreener Monitor: ACTIVE")
    except:
        pass
    
    # Performance monitoring
    tasks.append(asyncio.create_task(performance_monitor()))
    
    # Cache cleanup
    tasks.append(asyncio.create_task(cache_cleanup()))
    
    # Stop-loss monitor
    tasks.append(asyncio.create_task(stop_loss_monitor()))
    
    # Final ready message
    if CONFIG.ALERTS_NOTIFY.get("startup", True):
        await send_telegram_alert(
            f"🚀 BOT READY 🚀\n\n"
            f"Active Tasks: {len(tasks)}\n"
            f"Stop-Loss: ARMED\n"
            f"Hunting for profits with protection... 💰🛑"
        )
    
    await asyncio.gather(*tasks)

async def stop_loss_monitor():
    """Monitor all active stop-losses"""
    while True:
        try:
            if STOPS:
                stuck_stops = []
                for mint, stop_data in STOPS.items():
                    if stop_data["state"] == "TRIGGERED":
                        if stop_data.get("first_no_route"):
                            time_stuck = time.time() - stop_data["first_no_route"]
                            if time_stuck > CONFIG.ROUTE_TIMEOUT_SEC * 2:
                                stuck_stops.append((mint, time_stuck, stop_data.get("stuck_reason", "UNKNOWN")))
                
                if stuck_stops:
                    # Log internally but don't spam alerts
                    for mint, time_stuck, reason in stuck_stops[:5]:
                        logging.warning(f"Stuck stop-loss: {mint[:8]}... - {time_stuck:.0f}s ({reason})")
            
            await asyncio.sleep(30)  # Check every 30 seconds
            
        except Exception as e:
            logging.error(f"Stop-loss monitor error: {e}")
            await asyncio.sleep(60)

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
            
            # Only send if there's activity
            if tracker.trades_executed > 0:
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
• Stops Hit: {daily['stops_hit']}
• Profit: {daily['profit']:+.3f} SOL

Stop-Loss Status:
• Active Stops: {len(STOPS)}

Current Balance: {balance:.3f} SOL
Status: {"🟢 PROFITABLE" if session_pnl > 0 else "🔴 BUILDING"}
"""
                # Use regular send_telegram_alert for periodic reports
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
    config_obj = uvicorn.Config(
        app, 
        host="0.0.0.0", 
        port=port,
        log_level="warning"
    )
    server = uvicorn.Server(config_obj)
    
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
    
    print(f"""
╔════════════════════════════════════════╗
║   OPTIMIZED BOT v3.1 (QUIET MODE)     ║
║                                        ║
║  Features:                             ║
║  • Pre-trade validation                ║
║  • Automatic stop-loss on buy          ║
║  • Reliable stop monitoring            ║
║  • MINIMAL ALERTS (gated + cooldowns)  ║
║  • Smart position sizing               ║
║  • Pool data caching ({CONFIG.CACHE_TTL_SECONDS}s)     ║
║  • Performance tracking                ║
║  • Jupiter-only sells with validation  ║
║                                        ║
║  Stop-Loss Protection:                 ║
║  • {CONFIG.STOP_LOSS_PCT*100:.0f}% stop-loss level           ║
║  • {CONFIG.STOP_CHECK_INTERVAL_SEC}s check interval           ║
║  • Automatic arming on buy             ║
║                                        ║
║  Alerts Active:                        ║
║  • Buy: {str(CONFIG.ALERTS_NOTIFY.get('buy', False)):5}                      ║
║  • Sell: {str(CONFIG.ALERTS_NOTIFY.get('sell', False)):5}                     ║
║  • Stop: {str(CONFIG.ALERTS_NOTIFY.get('stop_triggered', False)):5}                    ║
║  • Cooldown: {CONFIG.ALERTS_NOTIFY.get('cooldown_secs', 60)}s               ║
║                                        ║
║  Ready for protected profits! 🚀🛑     ║
╚════════════════════════════════════════╝
    """)
    
    logging.info("=" * 50)
    logging.info("STARTING BOT WITH STOP-LOSS ENGINE (QUIET MODE)")
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
                
                daily = tracker.get_daily_summary()
                
                final_msg = (
                    f"📊 FINAL STATS\n"
                    f"Trades: {tracker.trades_executed}\n"
                    f"Win Rate: {tracker.get_win_rate():.1f}%\n"
                    f"Stops Hit Today: {daily['stops_hit']}\n"
                    f"Session P&L: {session_pnl:+.3f} SOL\n"
                    f"Final Balance: {balance:.3f} SOL\n"
                    f"Active Stops Remaining: {len(STOPS)}"
                )
                # Use regular alert for final message
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
