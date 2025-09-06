"""
Elite Momentum Scanner - Finds pumping tokens with your exact criteria
Implements the hybrid strategy for 70% win rate momentum plays
Updated to use explicit amount parameter for buy_token
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import httpx
from dotenv import load_dotenv

from utils import (
    buy_token, send_telegram_alert, is_bot_running,
    get_liquidity_and_ownership, wait_and_auto_sell
)

load_dotenv()

# ============================================
# MOMENTUM CONFIGURATION (FIXED THRESHOLDS)
# ============================================

# Core Settings
MOMENTUM_SCANNER_ENABLED = os.getenv("MOMENTUM_SCANNER", "true").lower() == "true"
MOMENTUM_AUTO_BUY = os.getenv("MOMENTUM_AUTO_BUY", "true").lower() == "true"
MIN_SCORE_AUTO_BUY = int(os.getenv("MIN_SCORE_AUTO_BUY", 2))  # LOWERED FROM 5 TO 2
MIN_SCORE_ALERT = int(os.getenv("MIN_SCORE_ALERT", 1))  # LOWERED FROM 3 TO 1

# Your Golden Rules - ADJUSTED THRESHOLDS
MOMENTUM_MIN_1H_GAIN = float(os.getenv("MOMENTUM_MIN_1H_GAIN", 30))  # LOWERED FROM 50 TO 30
MOMENTUM_MAX_1H_GAIN = float(os.getenv("MOMENTUM_MAX_1H_GAIN", 300))  # INCREASED FROM 200 TO 300
MOMENTUM_MIN_LIQUIDITY = float(os.getenv("MOMENTUM_MIN_LIQUIDITY", 500))  # LOWERED FROM 30000 TO 500 USD
MOMENTUM_MAX_MC = float(os.getenv("MOMENTUM_MAX_MC", 500000))  # $500k max market cap
MOMENTUM_MIN_HOLDERS = int(os.getenv("MOMENTUM_MIN_HOLDERS", 100))
MOMENTUM_MAX_HOLDERS = int(os.getenv("MOMENTUM_MAX_HOLDERS", 2000))
MOMENTUM_MIN_AGE_HOURS = float(os.getenv("MOMENTUM_MIN_AGE_HOURS", 2))
MOMENTUM_MAX_AGE_HOURS = float(os.getenv("MOMENTUM_MAX_AGE_HOURS", 24))

# Position Sizing - DYNAMIC BASED ON SCORE
MOMENTUM_POSITION_5_SCORE = float(os.getenv("MOMENTUM_POSITION_5_SCORE", 0.15))  # Score 5+: 0.15 SOL
MOMENTUM_POSITION_4_SCORE = float(os.getenv("MOMENTUM_POSITION_4_SCORE", 0.10))  # Score 4: 0.10 SOL
MOMENTUM_POSITION_3_SCORE = float(os.getenv("MOMENTUM_POSITION_3_SCORE", 0.05))  # Score 3: 0.05 SOL
MOMENTUM_POSITION_2_SCORE = float(os.getenv("MOMENTUM_POSITION_2_SCORE", 0.02))  # Score 2: 0.02 SOL
MOMENTUM_TEST_POSITION = float(os.getenv("MOMENTUM_TEST_POSITION", 0.02))  # Testing

# Trading Hours (AEST)
PRIME_HOURS = [21, 22, 23, 0, 1, 2, 3]  # 9 PM - 3 AM AEST (US market active)
REDUCED_HOURS = list(range(6, 21))  # 6 AM - 9 PM AEST (be pickier)

# Scan Settings
SCAN_INTERVAL = int(os.getenv("MOMENTUM_SCAN_INTERVAL", 60))  # Check every 60 seconds
MAX_TOKENS_TO_CHECK = 20  # Check top 20 gainers

# Track already analyzed tokens
analyzed_tokens = {}  # token -> {score, timestamp, bought}
momentum_bought = set()  # Track momentum purchases to prevent duplicates

# ============================================
# PATTERN DETECTION
# ============================================

def detect_chart_pattern(price_data: List[float]) -> str:
    """
    Detect if chart shows good or bad patterns
    Returns: 'steady_climb', 'pump_dump', 'vertical', 'consolidating', 'unknown'
    """
    if not price_data or len(price_data) < 5:
        return "unknown"
    
    # Calculate changes between candles
    changes = []
    for i in range(1, len(price_data)):
        if price_data[i-1] != 0:
            change = ((price_data[i] - price_data[i-1]) / price_data[i-1]) * 100
            changes.append(change)
    
    if not changes:
        return "unknown"
    
    # Detect patterns
    max_change = max(changes)
    avg_change = sum(changes) / len(changes)
    positive_candles = sum(1 for c in changes if c > 0)
    
    # Vertical pump (bad)
    if max_change > 100:
        return "vertical"
    
    # Pump and dump shape (bad)
    if len(changes) > 2:
        first_half = changes[:len(changes)//2]
        second_half = changes[len(changes)//2:]
        if sum(first_half) > 50 and sum(second_half) < -30:
            return "pump_dump"
    
    # Steady climb (good)
    if positive_candles >= len(changes) * 0.6 and 0 < avg_change < 20:
        return "steady_climb"
    
    # Consolidating (good for entry)
    if -5 < avg_change < 5 and max_change < 20:
        return "consolidating"
    
    return "unknown"

# ============================================
# SCORING SYSTEM (ADJUSTED FOR LOWER THRESHOLDS)
# ============================================

async def score_token(token_data: Dict) -> Tuple[int, List[str]]:
    """
    Score a token based on adjusted criteria
    Returns: (score, [list of signals that passed])
    """
    score = 0
    signals = []
    
    try:
        # Extract data
        price_change_1h = float(token_data.get("priceChange", {}).get("h1", 0))
        price_change_5m = float(token_data.get("priceChange", {}).get("m5", 0))
        liquidity_usd = float(token_data.get("liquidity", {}).get("usd", 0))
        volume_h24 = float(token_data.get("volume", {}).get("h24", 0))
        market_cap = float(token_data.get("marketCap", 0))
        created_at = token_data.get("pairCreatedAt", 0)
        
        # Calculate age in hours
        if created_at:
            age_hours = (time.time() * 1000 - created_at) / (1000 * 60 * 60)
        else:
            age_hours = 0
        
        # Get price history if available
        price_history = token_data.get("priceHistory", [])
        pattern = detect_chart_pattern(price_history) if price_history else "unknown"
        
        # ===== MOMENTUM RULES (ADJUSTED CRITERIA) =====
        
        # 1. Hour gain in sweet spot (30-300%) - ADJUSTED
        if MOMENTUM_MIN_1H_GAIN <= price_change_1h <= MOMENTUM_MAX_1H_GAIN:
            score += 1
            signals.append(f"✅ 1h gain: {price_change_1h:.1f}%")
        elif price_change_1h > MOMENTUM_MAX_1H_GAIN:
            signals.append(f"⚠️ High gain: {price_change_1h:.1f}% (still ok)")
            # Don't disqualify, just don't add score
        
        # 2. Still pumping (5m green)
        if price_change_5m > 0:
            score += 1
            signals.append(f"✅ Still pumping: {price_change_5m:.1f}% on 5m")
        else:
            signals.append(f"⚠️ Cooling off: {price_change_5m:.1f}% on 5m")
        
        # 3. Volume/Liquidity ratio > 2 (good activity)
        if liquidity_usd > 0:
            vol_liq_ratio = volume_h24 / liquidity_usd
            if vol_liq_ratio > 2:
                score += 1
                signals.append(f"✅ Volume/Liq ratio: {vol_liq_ratio:.1f}")
        
        # 4. Safe liquidity - MUCH LOWER THRESHOLD
        if liquidity_usd >= MOMENTUM_MIN_LIQUIDITY:
            score += 1
            signals.append(f"✅ Liquidity: ${liquidity_usd:,.0f}")
        else:
            signals.append(f"⚠️ Low liquidity: ${liquidity_usd:,.0f}")
            # Don't disqualify completely
        
        # 5. Room to grow (MC < $500k)
        if market_cap < MOMENTUM_MAX_MC:
            score += 1
            signals.append(f"✅ Room to grow: ${market_cap:,.0f} MC")
        else:
            signals.append(f"⚠️ High MC: ${market_cap:,.0f}")
        
        # 6. Good age (2-24 hours)
        if MOMENTUM_MIN_AGE_HOURS <= age_hours <= MOMENTUM_MAX_AGE_HOURS:
            score += 0.5
            signals.append(f"✅ Good age: {age_hours:.1f}h old")
        
        # 7. Pattern bonus
        if pattern == "steady_climb":
            score += 0.5
            signals.append("✅ Steady climb pattern")
        elif pattern == "consolidating":
            score += 0.25
            signals.append("✅ Consolidating pattern")
        elif pattern in ["vertical", "pump_dump"]:
            signals.append(f"⚠️ Pattern: {pattern}")
            # Don't penalize too hard
        
        # 8. Check if NOT at ATH (bonus)
        if price_change_5m < 0 and price_change_1h > 30:
            score += 0.25
            signals.append("✅ Pulling back from high")
        
    except Exception as e:
        logging.error(f"Error scoring token: {e}")
        return (0, [f"Error: {str(e)}"])
    
    return (int(score), signals)

# ============================================
# DEXSCREENER API INTERFACE
# ============================================

async def fetch_top_gainers() -> List[Dict]:
    """
    Fetch top gaining tokens from DexScreener
    """
    try:
        url = "https://api.dexscreener.com/latest/dex/pairs/solana"
        
        async with httpx.AsyncClient(timeout=10, verify=False) as client:
            response = await client.get(url)
            
            if response.status_code == 200:
                data = response.json()
                pairs = data.get("pairs", [])
                
                # Filter for Raydium/Orca pairs only (avoid scams)
                filtered_pairs = []
                for pair in pairs:
                    if pair.get("dexId") in ["raydium", "orca"]:
                        # Check basic criteria
                        price_change_1h = float(pair.get("priceChange", {}).get("h1", 0))
                        liquidity_usd = float(pair.get("liquidity", {}).get("usd", 0))
                        
                        # Pre-filter with LOWER thresholds
                        if (price_change_1h >= MOMENTUM_MIN_1H_GAIN * 0.8 and  # 80% of minimum
                            liquidity_usd >= MOMENTUM_MIN_LIQUIDITY * 0.5):  # 50% of minimum
                            filtered_pairs.append(pair)
                
                # Sort by 1h gain
                filtered_pairs.sort(key=lambda x: float(x.get("priceChange", {}).get("h1", 0)), reverse=True)
                
                # Return top candidates
                return filtered_pairs[:MAX_TOKENS_TO_CHECK]
                
    except Exception as e:
        logging.error(f"Error fetching gainers: {e}")
    
    return []

# ============================================
# MAIN MOMENTUM SCANNER (FIXED BUY EXECUTION)
# ============================================

async def momentum_scanner():
    """
    Main momentum scanner - Runs continuously finding pumping tokens
    """
    if not MOMENTUM_SCANNER_ENABLED:
        logging.info("[Momentum Scanner] Disabled via configuration")
        return
    
    await send_telegram_alert(
        "🔥 MOMENTUM SCANNER ACTIVE 🔥\n\n"
        f"Mode: {'AUTO-BUY' if MOMENTUM_AUTO_BUY else 'ALERT ONLY'}\n"
        f"Auto-buy threshold: {MIN_SCORE_AUTO_BUY}/5\n"
        f"Alert threshold: {MIN_SCORE_ALERT}/5\n"
        f"Position sizes: 0.02-0.15 SOL\n"
        f"Min Liquidity: ${MOMENTUM_MIN_LIQUIDITY}\n"
        f"1H Gain Range: {MOMENTUM_MIN_1H_GAIN}%-{MOMENTUM_MAX_1H_GAIN}%\n\n"
        "Hunting for pumps..."
    )
    
    consecutive_errors = 0
    
    while True:
        try:
            if not is_bot_running():
                await asyncio.sleep(30)
                continue
            
            # Check if we're in prime trading hours
            current_hour = datetime.now().hour
            is_prime_time = current_hour in PRIME_HOURS
            
            # Adjust thresholds based on time
            if not is_prime_time and current_hour not in REDUCED_HOURS:
                await asyncio.sleep(SCAN_INTERVAL)
                continue  # Skip dead hours
            
            # Fetch top gainers
            top_gainers = await fetch_top_gainers()
            
            if not top_gainers:
                consecutive_errors += 1
                if consecutive_errors > 5:
                    logging.warning("[Momentum Scanner] Multiple fetch failures")
                    await asyncio.sleep(SCAN_INTERVAL * 2)
                continue
            
            consecutive_errors = 0
            candidates_found = 0
            
            # Analyze each token
            for token_data in top_gainers:
                try:
                    token_address = token_data.get("baseToken", {}).get("address")
                    token_symbol = token_data.get("baseToken", {}).get("symbol", "Unknown")
                    
                    if not token_address:
                        continue
                    
                    # Skip if recently analyzed (within 5 minutes)
                    if token_address in analyzed_tokens:
                        last_check = analyzed_tokens[token_address].get("timestamp", 0)
                        if time.time() - last_check < 300:  # 5 minutes
                            continue
                    
                    # Skip if already bought through momentum scanner
                    if token_address in momentum_bought:
                        continue
                    
                    # Score the token
                    score, signals = await score_token(token_data)
                    
                    # Store analysis
                    analyzed_tokens[token_address] = {
                        "score": score,
                        "timestamp": time.time(),
                        "signals": signals,
                        "symbol": token_symbol
                    }
                    
                    # Skip low scores
                    if score < MIN_SCORE_ALERT:
                        continue
                    
                    candidates_found += 1
                    
                    # Determine action based on score
                    if score >= MIN_SCORE_AUTO_BUY and MOMENTUM_AUTO_BUY:
                        # AUTO BUY - Good setup
                        position_size = get_position_size_by_score(score)
                        
                        # Extra caution during off-hours
                        if not is_prime_time:
                            position_size *= 0.5
                        
                        logging.info(f"[Momentum] Auto-buying {token_symbol} with score {score}")
                        
                        await send_telegram_alert(
                            f"🎯 MOMENTUM AUTO-BUY 🎯\n\n"
                            f"Token: {token_symbol} ({token_address[:8]}...)\n"
                            f"Score: {score}/5 ⭐\n"
                            f"Position: {position_size} SOL\n\n"
                            f"Signals:\n" + "\n".join(signals[:5]) + "\n\n"
                            f"Executing..."
                        )
                        
                        # FIXED: Use explicit amount parameter instead of force_amount
                        success = await buy_token(token_address, amount=position_size)
                        
                        if success:
                            # Track momentum purchase
                            momentum_bought.add(token_address)
                            momentum_stats["auto_buys"] += 1
                            
                            await send_telegram_alert(
                                f"✅ MOMENTUM BUY SUCCESS\n"
                                f"Token: {token_symbol}\n"
                                f"Amount: {position_size} SOL (explicit)\n"
                                f"Strategy: Momentum Play\n\n"
                                f"Monitoring with 1.5x/3x/10x targets..."
                            )
                            
                            # Start auto-sell with standard wait_and_auto_sell
                            asyncio.create_task(wait_and_auto_sell(token_address))
                        else:
                            logging.error(f"[Momentum] Failed to buy {token_symbol}")
                        
                    elif score >= MIN_SCORE_ALERT:
                        # ALERT ONLY - Decent setup needs approval
                        await send_telegram_alert(
                            f"🔔 MOMENTUM OPPORTUNITY 🔔\n\n"
                            f"Token: {token_symbol} ({token_address[:8]}...)\n"
                            f"Score: {score}/5 ⭐\n"
                            f"Suggested: {get_position_size_by_score(score)} SOL\n\n"
                            f"Signals:\n" + "\n".join(signals[:5]) + "\n\n"
                            f"Use /forcebuy {token_address} to execute"
                        )
                        momentum_stats["alerts_sent"] += 1
                    
                    # Rate limit between checks
                    await asyncio.sleep(1)
                    
                except Exception as e:
                    logging.error(f"Error analyzing token: {e}")
                    continue
            
            # Summary log
            if candidates_found > 0:
                logging.info(f"[Momentum Scanner] Found {candidates_found} candidates this scan")
            
            momentum_stats["total_scans"] += 1
            momentum_stats["tokens_analyzed"] += len(top_gainers)
            
            # Wait before next scan
            await asyncio.sleep(SCAN_INTERVAL)
            
        except Exception as e:
            logging.error(f"[Momentum Scanner] Error in main loop: {e}")
            await asyncio.sleep(SCAN_INTERVAL)

# ============================================
# POSITION SIZING BASED ON SCORE
# ============================================

def get_position_size_by_score(score: int) -> float:
    """Get position size based on momentum score"""
    if score >= 5:
        return MOMENTUM_POSITION_5_SCORE  # 0.15 SOL
    elif score >= 4:
        return MOMENTUM_POSITION_4_SCORE  # 0.10 SOL
    elif score >= 3:
        return MOMENTUM_POSITION_3_SCORE  # 0.05 SOL
    elif score >= 2:
        return MOMENTUM_POSITION_2_SCORE  # 0.02 SOL
    else:
        return MOMENTUM_TEST_POSITION  # 0.02 SOL

# ============================================
# CHECK MOMENTUM SCORE (FOR FORCE BUYS)
# ============================================

async def check_momentum_score(token_address: str) -> Dict:
    """
    Check momentum score for a specific token (used by forcebuy)
    """
    try:
        # Fetch token data from DexScreener
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
        
        async with httpx.AsyncClient(timeout=10, verify=False) as client:
            response = await client.get(url)
            
            if response.status_code == 200:
                data = response.json()
                pairs = data.get("pairs", [])
                
                if pairs:
                    # Get best pair
                    best_pair = pairs[0]
                    score, signals = await score_token(best_pair)
                    
                    return {
                        "score": score,
                        "signals": signals,
                        "recommendation": get_position_size_by_score(score),
                        "should_buy": score >= MIN_SCORE_AUTO_BUY
                    }
        
    except Exception as e:
        logging.error(f"Error checking momentum score: {e}")
    
    return {
        "score": 0, 
        "signals": ["Failed to fetch data"], 
        "recommendation": 0,
        "should_buy": False
    }

# ============================================
# STATS TRACKING
# ============================================

momentum_stats = {
    "total_scans": 0,
    "tokens_analyzed": 0,
    "auto_buys": 0,
    "alerts_sent": 0,
    "successful_trades": 0,
    "total_profit": 0
}

async def report_momentum_stats():
    """Send daily momentum scanner report"""
    while True:
        await asyncio.sleep(86400)  # Daily
        
        win_rate = 0
        if momentum_stats['auto_buys'] > 0:
            win_rate = (momentum_stats['successful_trades'] / momentum_stats['auto_buys']) * 100
        
        await send_telegram_alert(
            f"📊 MOMENTUM SCANNER DAILY REPORT\n\n"
            f"Scans: {momentum_stats['total_scans']}\n"
            f"Tokens Analyzed: {momentum_stats['tokens_analyzed']}\n"
            f"Auto Buys: {momentum_stats['auto_buys']}\n"
            f"Alerts Sent: {momentum_stats['alerts_sent']}\n"
            f"Win Rate: {win_rate:.1f}%\n"
            f"Total Profit: {momentum_stats['total_profit']:.2f} SOL"
        )

# ============================================
# EXPORT FOR USE IN OTHER MODULES
# ============================================

__all__ = [
    'momentum_scanner',
    'check_momentum_score',
    'report_momentum_stats',
    'momentum_bought',
    'momentum_stats'
]
