"""
Integration layer - Connects monster features to your existing bot
Run this INSTEAD of sniper_logic.py for BEAST MODE
"""

import asyncio
import os
import logging
from dotenv import load_dotenv

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
# CONFIGURATION HELPER
# ============================================

def setup_monster_config():
    """
    Add these to your .env file for monster features
    """
    config = """
# ============================================
# MONSTER BOT CONFIGURATION
# Add these to your .env file
# ============================================

# AI Scoring
MIN_AI_SCORE=0.6                # Minimum AI score to buy (0-1)
AI_LIQUIDITY_WEIGHT=0.3         # Weight for liquidity in AI score
AI_HOLDER_WEIGHT=0.2            # Weight for holder distribution
AI_SOCIAL_WEIGHT=0.2            # Weight for social signals
AI_PATTERN_WEIGHT=0.3          # Weight for pattern matching

# MEV Protection (Jito)
USE_JITO=true                   # Use Jito bundles for MEV protection
JITO_URL=https://mainnet.block-engine.jito.wtf/api/v1
JITO_TIP=0.001                  # SOL tip for priority (0.001 = $0.15)

# Copy Trading
ENABLE_COPY_TRADING=true        # Follow profitable wallets
COPY_WALLET_1=9WzDXwBbmkg8ZTbNFMPiAaQ9xhqvK8GXhPYjfgMJ8a9
COPY_WALLET_2=Cs5qShsPL85WtanR8G2XticV9Y7eQFpBCCVUwvjxLgpn
COPY_SCALE_PERCENT=10           # Copy at 10% of whale's size

# Social Scanning
ENABLE_SOCIAL_SCAN=true         # Scan Telegram/Twitter
TELEGRAM_CHANNEL_1=@alphagroup  # Replace with real channels
TELEGRAM_CHANNEL_2=@gemcalls
SOCIAL_MIN_MENTIONS=3           # Need 3 mentions to trigger buy

# Arbitrage
ENABLE_ARBITRAGE=true           # DEX arbitrage bot
ARB_MIN_PROFIT=2.0             # Minimum 2% profit to execute
ARB_MAX_POSITION=5.0           # Max 5 SOL per arbitrage

# Auto-Scaling
ENABLE_AUTO_COMPOUND=true       # Auto increase positions with profits
COMPOUND_THRESHOLD=10          # Compound after 10 SOL profit
COMPOUND_INCREASE=20           # Increase positions by 20%

# Position Limits
MAX_POSITION_PER_TOKEN=5.0     # Max 5 SOL per token
MAX_OPEN_POSITIONS=20          # Max 20 concurrent positions
DAILY_LOSS_LIMIT=50            # Stop if down 50 SOL in a day
    """
    
    print(config)
    return config

# ============================================
# MAIN ENTRY POINT
# ============================================

async def main():
    """
    Launch the monster bot
    """
    # Check if we have the necessary config
    if not os.getenv("HELIUS_API"):
        print("ERROR: HELIUS_API not set in environment")
        print("The monster bot needs Helius API for mempool monitoring")
        return
    
    # Show config helper if needed
    if os.getenv("SHOW_CONFIG_HELP", "false").lower() == "true":
        setup_monster_config()
        return
    
    # Choose mode
    mode = os.getenv("BOT_MODE", "monster").lower()
    
    if mode == "monster":
        # Run with ALL features
        await start_monster_sniper()
    elif mode == "basic":
        # Run your original bot
        from sniper_logic import start_sniper
        await start_sniper()
    else:
        print(f"Unknown mode: {mode}")

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    asyncio.run(main())
