"""
Integration layer - Connects monster features to your existing bot
WITH DUMMY WEB SERVER TO KEEP RENDER HAPPY
"""

import asyncio
import os
import logging
from dotenv import load_dotenv

# Import for dummy web server
from fastapi import FastAPI
import uvicorn

# Import your existing bot
from sniper_logic import (
    mempool_listener, trending_scanner, 
    start_sniper_with_forced_token, stop_all_tasks
)

# Import monster features
from monster_bot import (
    MonsterBot, AIScorer, JitoClient, 
    CopyTrader, ArbitrageBot, SocialScanner,
    calculate_position_size
)

# Import your utils
from utils import (
    buy_token as original_buy_token,
    send_telegram_alert, keypair, BUY_AMOUNT_SOL
)

load_dotenv()

# ============================================
# DUMMY WEB SERVER FOR RENDER
# ============================================

app = FastAPI()

@app.get("/")
async def health_check():
    """Health check endpoint for Render"""
    return {
        "status": "🚀 Monster Bot Active",
        "mode": "BEAST MODE",
        "target": "$10k-100k daily"
    }

@app.get("/status")
async def status():
    """Status endpoint"""
    return {
        "bot": "running",
        "listeners": "active",
        "mode": os.getenv("BOT_MODE", "monster")
    }

# ============================================
# ENHANCED BUY FUNCTION WITH ALL FEATURES
# ============================================

async def monster_buy_token(mint: str, force_amount: float = None):
    """
    Enhanced buy with AI scoring, dynamic sizing, and MEV protection
    """
    try:
        # 1. Get pool data
        from utils import get_liquidity_and_ownership
        lp_data = await get_liquidity_and_ownership(mint)
        pool_liquidity = lp_data.get("liquidity", 0) if lp_data else 0
        
        # 2. AI Score the token
        ai_scorer = AIScorer()
        ai_score = await ai_scorer.score_token(mint, lp_data)
        
        # 3. Check if it passes AI threshold
        min_score = float(os.getenv("MIN_AI_SCORE", 0.5))
        if ai_score < min_score and not force_amount:
            await send_telegram_alert(
                f"❌ Skipped {mint[:8]}...\n"
                f"AI Score: {ai_score:.2f} (min: {min_score})\n"
                f"Liquidity: {pool_liquidity:.1f} SOL"
            )
            return False
        
        # 4. Calculate dynamic position size
        if force_amount:
            amount_sol = force_amount
        else:
            amount_sol = calculate_position_size(pool_liquidity, ai_score)
        
        if amount_sol == 0:
            await send_telegram_alert(
                f"⚠️ Skipped {mint[:8]}...\n"
                f"Pool too small for safe entry\n"
                f"Liquidity: {pool_liquidity:.1f} SOL"
            )
            return False
        
        # 5. Send alert about the buy
        await send_telegram_alert(
            f"🎯 MONSTER BUY SIGNAL\n\n"
            f"Token: {mint[:8]}...\n"
            f"AI Score: {ai_score:.2f}/1.00\n"
            f"Liquidity: {pool_liquidity:.1f} SOL\n"
            f"Position Size: {amount_sol} SOL\n"
            f"Strategy: {'Forced' if force_amount else 'Dynamic'}\n\n"
            f"Executing with MEV protection..."
        )
        
        # 6. Try Jito bundle first for MEV protection
        use_jito = os.getenv("USE_JITO", "true").lower() == "true"
        
        if use_jito and not force_amount:
            jito = JitoClient()
            bundle_sent = await jito.create_snipe_bundle(mint, amount_sol)
            if bundle_sent:
                logging.info(f"[MONSTER] Sent via Jito bundle")
                return True
        
        # 7. Fallback to regular buy (your existing logic)
        # Temporarily override BUY_AMOUNT_SOL for this trade
        original_amount = os.getenv("BUY_AMOUNT_SOL")
        os.environ["BUY_AMOUNT_SOL"] = str(amount_sol)
        
        result = await original_buy_token(mint)
        
        # Restore original amount
        os.environ["BUY_AMOUNT_SOL"] = original_amount
        
        if result:
            await send_telegram_alert(
                f"✅ MONSTER BUY SUCCESS\n"
                f"Token: {mint[:8]}...\n"
                f"Amount: {amount_sol} SOL\n"
                f"AI Score: {ai_score:.2f}"
            )
        
        return result
        
    except Exception as e:
        logging.error(f"[MONSTER BUY] Error: {e}")
        return False

# ============================================
# ENHANCED SNIPER WITH ALL FEATURES
# ============================================

async def start_monster_sniper():
    """
    Start the complete monster bot with all features
    """
    await send_telegram_alert(
        "🦾 MONSTER BOT INITIALIZING 🦾\n\n"
        "Loading Elite Features:\n"
        "• AI Token Scoring ✅\n"
        "• Dynamic Position Sizing ✅\n"
        "• MEV Bundle Protection ✅\n"
        "• Copy Trading Engine ✅\n"
        "• Social Media Scanner ✅\n"
        "• DEX Arbitrage Bot ✅\n"
        "• Performance Analytics ✅\n\n"
        "Mode: BEAST MODE ACTIVATED\n"
        "Target: $10k-100k Daily\n\n"
        "Starting all systems..."
    )
    
    # Initialize components
    monster = MonsterBot()
    tasks = []
    
    # 1. Start your existing listeners (they'll use monster_buy_token now)
    # Monkey-patch the buy function
    import utils
    utils.buy_token = monster_buy_token
    
    tasks.extend([
        asyncio.create_task(mempool_listener("Raydium")),
        asyncio.create_task(mempool_listener("Jupiter")),
        asyncio.create_task(mempool_listener("PumpFun")),
        asyncio.create_task(mempool_listener("Moonshot")),
        asyncio.create_task(trending_scanner())
    ])
    
    # 2. Add copy trading
    if os.getenv("ENABLE_COPY_TRADING", "true").lower() == "true":
        tasks.append(asyncio.create_task(monster.copy_trader.monitor_wallets()))
        await send_telegram_alert("📋 Copy Trading: ACTIVE")
    
    # 3. Add social scanning
    if os.getenv("ENABLE_SOCIAL_SCAN", "true").lower() == "true":
        tasks.append(asyncio.create_task(monster.social_scanner.scan_telegram()))
        await send_telegram_alert("📱 Social Scanner: ACTIVE")
    
    # 4. Add arbitrage bot
    if os.getenv("ENABLE_ARBITRAGE", "true").lower() == "true":
        tasks.append(asyncio.create_task(monster.arb_bot.find_opportunities()))
        await send_telegram_alert("💎 Arbitrage Bot: ACTIVE")
    
    # 5. Add performance monitoring
    tasks.append(asyncio.create_task(monster.monitor_performance()))
    
    # 6. Add auto-compounding
    if os.getenv("ENABLE_AUTO_COMPOUND", "true").lower() == "true":
        tasks.append(asyncio.create_task(monster.auto_compound_profits()))
        await send_telegram_alert("📈 Auto-Compound: ACTIVE")
    
    await send_telegram_alert(
        "🚀 MONSTER BOT FULLY OPERATIONAL 🚀\n\n"
        f"Active Strategies: {len(tasks)}\n"
        f"Position Size: Dynamic (AI-based)\n"
        f"Min AI Score: {os.getenv('MIN_AI_SCORE', '0.5')}\n"
        f"MEV Protection: {'ON' if os.getenv('USE_JITO', 'true').lower() == 'true' else 'OFF'}\n\n"
        "Ready to print money! 💰"
    )
    
    # Run all tasks
    await asyncio.gather(*tasks)

# ============================================
# MAIN ENTRY POINT WITH WEB SERVER
# ============================================

async def run_bot_with_web_server():
    """Run the bot alongside dummy web server"""
    # Start the monster bot in the background
    asyncio.create_task(start_monster_sniper())
    
    # Run the web server to keep Render happy
    port = int(os.getenv("PORT", 10000))
    config = uvicorn.Config(
        app, 
        host="0.0.0.0", 
        port=port,
        log_level="warning"  # Reduce web server noise
    )
    server = uvicorn.Server(config)
    
    logging.info(f"Starting web server on port {port} to keep Render happy...")
    await server.serve()

def main():
    """Main entry point"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    # Check if we have the necessary config
    if not os.getenv("HELIUS_API"):
        print("ERROR: HELIUS_API not set in environment")
        return
    
    # Run the bot with web server
    asyncio.run(run_bot_with_web_server())

if __name__ == "__main__":
    main()
