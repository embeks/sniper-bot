# Fixed dexscreener_monitor.py - PRODUCTION READY WITH EXPLICIT AMOUNTS
"""
ELITE DEXSCREENER MONITOR - Fixed API and improved detection
"""

import asyncio
import httpx
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, Set, List, Optional
import os
from dotenv import load_dotenv

from utils import (
    buy_token, send_telegram_alert, is_bot_running,
    get_liquidity_and_ownership, wait_and_auto_sell,
    BROKEN_TOKENS, mark_broken_token,
    increment_stat, update_last_activity, HTTPManager
)

load_dotenv()

# Configuration
CHECK_INTERVAL = int(os.getenv("DEXSCREENER_CHECK_INTERVAL", 15))  # Check every 15 seconds
MAX_AGE_MINUTES = int(os.getenv("MAX_POOL_AGE_MINUTES", 5))  
MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", 1500))  # Lower threshold
MIN_VOLUME_USD = float(os.getenv("MIN_VOLUME_USD", 500))  # Lower threshold
MIN_BUYS = int(os.getenv("MIN_BUYS", 5))  # Less strict
MAX_PRICE_IMPACT = float(os.getenv("MAX_PRICE_IMPACT", 15))  # Allow more impact

# Elite settings
ELITE_MODE = os.getenv("ELITE_MODE", "true").lower() == "true"
PUMPFUN_PRIORITY = os.getenv("PUMPFUN_PRIORITY", "true").lower() == "true"
AUTO_BUY = os.getenv("AUTO_BUY", "true").lower() == "true"

# FIXED: Default buy amounts - never mutate ENV
DEFAULT_BUY_AMOUNT = float(os.getenv("BUY_AMOUNT_SOL", 0.02))
PUMPFUN_MIGRATION_BUY = float(os.getenv("PUMPFUN_MIGRATION_BUY", 0.1))

# Track seen pools to avoid duplicates
seen_pools: Set[str] = set()
pool_creation_times: Dict[str, datetime] = {}

class DexScreenerMonitor:
    """Fixed DexScreener monitor with proper API handling"""
    
    def __init__(self):
        self.session = None
        self.running = False
        self.stats = {
            "pools_found": 0,
            "pools_bought": 0,
            "pools_skipped": 0,
            "last_check": None,
            "api_failures": 0
        }
        
    async def start(self):
        """Start the monitor"""
        self.running = True
        await send_telegram_alert(
            "🎯 DexScreener Monitor ACTIVE\n\n"
            f"Check interval: {CHECK_INTERVAL}s\n"
            f"Min liquidity: ${MIN_LIQUIDITY_USD:,.0f}\n"
            f"Min volume: ${MIN_VOLUME_USD:,.0f}\n"
            f"Auto-buy: {'ON' if AUTO_BUY else 'OFF'}\n\n"
            "Hunting for opportunities..."
        )
        
        await self.monitor_loop()
    
    async def monitor_loop(self):
        """Main monitoring loop"""
        consecutive_errors = 0
        
        while self.running:
            try:
                if not is_bot_running():
                    await asyncio.sleep(CHECK_INTERVAL)
                    continue
                
                # Get latest pools
                new_pools = await self.fetch_new_pools()
                
                if new_pools:
                    logging.info(f"[DexScreener] Found {len(new_pools)} pools to analyze")
                    
                    for pool in new_pools:
                        await self.process_pool(pool)
                
                # Reset error counter on success
                consecutive_errors = 0
                self.stats["last_check"] = datetime.now()
                
            except Exception as e:
                consecutive_errors += 1
                self.stats["api_failures"] += 1
                logging.error(f"[DexScreener] Monitor error: {e}")
                
                if consecutive_errors > 5:
                    await send_telegram_alert(
                        f"⚠️ DexScreener having issues\n"
                        f"Errors: {consecutive_errors}\n"
                        f"Waiting 60s..."
                    )
                    await asyncio.sleep(60)
                    consecutive_errors = 0
                else:
                    await asyncio.sleep(CHECK_INTERVAL * 2)
            
            await asyncio.sleep(CHECK_INTERVAL)
    
    async def fetch_new_pools(self) -> List[Dict]:
        """Fetch pools from DexScreener with fixed API call using HTTPManager"""
        try:
            # Try multiple approaches
            urls_to_try = [
                "https://api.dexscreener.com/latest/dex/search?q=SOL",  # Search for SOL pairs
                "https://api.dexscreener.com/latest/dex/pairs/solana",  # Original endpoint
                "https://api.dexscreener.com/latest/dex/tokens/solana"  # Token endpoint
            ]
            
            for url in urls_to_try:
                try:
                    headers = {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "Accept": "application/json",
                        "Accept-Language": "en-US,en;q=0.9",
                        "Cache-Control": "no-cache",
                        "Pragma": "no-cache",
                        "Referer": "https://dexscreener.com/"
                    }
                    
                    response = await HTTPManager.request(url, headers=headers, timeout=10)
                    
                    if response:
                        data = response.json()
                        pairs = data.get("pairs", [])
                        
                        if not pairs and "results" in data:
                            # Handle search results format
                            results = data.get("results", [])
                            if results and isinstance(results, list) and len(results) > 0:
                                pairs = results[0].get("pairs", []) if "pairs" in results[0] else []
                        
                        if pairs:
                            logging.info(f"[DexScreener] Got {len(pairs)} pairs from {url}")
                            return self.filter_new_pools(pairs)
                        
                except Exception as e:
                    logging.debug(f"[DexScreener] Error with {url}: {e}")
                    continue
            
            # If all fail, return empty
            logging.warning("[DexScreener] All API endpoints failed")
            return []
                
        except Exception as e:
            logging.error(f"[DexScreener] Fetch error: {e}")
            return []
    
    def filter_new_pools(self, pairs: List[Dict]) -> List[Dict]:
        """Filter for genuinely new pools"""
        new_pools = []
        current_time = datetime.now()
        
        for pair in pairs[:50]:  # Check top 50
            try:
                pool_address = pair.get("pairAddress")
                if not pool_address or pool_address in seen_pools:
                    continue
                
                # Parse pool age
                created_at = pair.get("pairCreatedAt")
                if created_at:
                    # Check if pool is new enough
                    pool_age_ms = current_time.timestamp() * 1000 - created_at
                    pool_age_minutes = pool_age_ms / (1000 * 60)
                    
                    if pool_age_minutes <= MAX_AGE_MINUTES:
                        pair["age_minutes"] = pool_age_minutes
                        new_pools.append(pair)
                        seen_pools.add(pool_address)
                        
            except Exception as e:
                logging.debug(f"[DexScreener] Error parsing pair: {e}")
                continue
        
        return new_pools
    
    async def process_pool(self, pool: Dict) -> bool:
        """Process a pool and decide whether to buy - FIXED with explicit amounts"""
        try:
            self.stats["pools_found"] += 1
            
            # Extract pool data
            token_address = pool.get("baseToken", {}).get("address")
            token_symbol = pool.get("baseToken", {}).get("symbol", "UNKNOWN")
            token_name = pool.get("baseToken", {}).get("name", "Unknown")
            
            if not token_address:
                return False
            
            # Skip if it's WSOL or a stablecoin
            if token_address in ["So11111111111111111111111111111111111111112", 
                                "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                                "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"]:
                return False
            
            liquidity_usd = float(pool.get("liquidity", {}).get("usd", 0))
            volume_h1 = float(pool.get("volume", {}).get("h1", 0))
            price_usd = float(pool.get("priceUsd", 0))
            market_cap = float(pool.get("marketCap", 0))
            
            age_minutes = pool.get("age_minutes", 0)
            txns = pool.get("txns", {})
            buys_h1 = txns.get("h1", {}).get("buys", 0)
            sells_h1 = txns.get("h1", {}).get("sells", 0)
            
            # Check if PumpFun graduate
            is_pumpfun = "pump" in token_name.lower() or market_cap == 69420
            
            logging.info(
                f"[DexScreener] Analyzing: {token_symbol}\n"
                f"  Age: {age_minutes:.1f} mins\n"
                f"  Liquidity: ${liquidity_usd:,.0f}\n"
                f"  Volume: ${volume_h1:,.0f}\n"
                f"  Buys/Sells: {buys_h1}/{sells_h1}"
            )
            
            # Apply filters
            if not await self.should_buy_pool(pool):
                self.stats["pools_skipped"] += 1
                return False
            
            # Send alert
            alert_emoji = "🎓" if is_pumpfun else "💎"
            priority = "PUMPFUN GRADUATE" if is_pumpfun else "QUALITY TOKEN"
            
            await send_telegram_alert(
                f"{alert_emoji} {priority} FOUND!\n\n"
                f"Token: {token_symbol}\n"
                f"Address: `{token_address}`\n"
                f"Age: {age_minutes:.1f} minutes\n"
                f"Liquidity: ${liquidity_usd:,.0f}\n"
                f"Volume (1h): ${volume_h1:,.0f}\n"
                f"Market Cap: ${market_cap:,.0f}\n"
                f"Buys/Sells: {buys_h1}/{sells_h1}\n\n"
                f"{'🚀 Attempting buy...' if AUTO_BUY else '⏸ Manual mode'}"
            )
            
            # Auto-buy if enabled - FIXED: Use explicit amount
            if AUTO_BUY and token_address:
                try:
                    # FIXED: Determine buy amount based on token type
                    if is_pumpfun:
                        buy_amount = PUMPFUN_MIGRATION_BUY
                    else:
                        buy_amount = DEFAULT_BUY_AMOUNT
                    
                    # FIXED: Pass explicit amount to buy_token
                    success = await buy_token(token_address, amount=buy_amount)
                    
                    if success:
                        self.stats["pools_bought"] += 1
                        asyncio.create_task(wait_and_auto_sell(token_address))
                        return True
                    else:
                        mark_broken_token(token_address, 0)
                        
                except Exception as e:
                    logging.error(f"[DexScreener] Buy error: {e}")
            
            return False
            
        except Exception as e:
            logging.error(f"[DexScreener] Process pool error: {e}")
            return False
    
    async def should_buy_pool(self, pool: Dict) -> bool:
        """Determine if pool meets buying criteria - LESS STRICT"""
        try:
            # Extract metrics
            liquidity_usd = float(pool.get("liquidity", {}).get("usd", 0))
            volume_h1 = float(pool.get("volume", {}).get("h1", 0))
            age_minutes = pool.get("age_minutes", 999)
            
            txns = pool.get("txns", {})
            buys_h1 = txns.get("h1", {}).get("buys", 0)
            sells_h1 = txns.get("h1", {}).get("sells", 0)
            
            # Basic filters - LESS STRICT
            if liquidity_usd < MIN_LIQUIDITY_USD:
                logging.info(f"[DexScreener] Skip - Low liquidity: ${liquidity_usd:.0f}")
                return False
            
            # Only check volume for older pools
            if volume_h1 < MIN_VOLUME_USD and age_minutes > 3:
                logging.info(f"[DexScreener] Skip - Low volume: ${volume_h1:.0f}")
                return False
            
            # Only check buys for older pools
            if buys_h1 < MIN_BUYS and age_minutes > 2:
                logging.info(f"[DexScreener] Skip - Low buys: {buys_h1}")
                return False
            
            # Check buy/sell ratio (avoid dumps) - LESS STRICT
            if sells_h1 > 0 and buys_h1 > 0:
                buy_sell_ratio = buys_h1 / sells_h1
                if buy_sell_ratio < 0.3:  # Very lenient
                    logging.info(f"[DexScreener] Skip - Bad ratio: {buy_sell_ratio:.2f}")
                    return False
            
            # Elite mode checks
            if ELITE_MODE:
                # Check price change
                price_change = pool.get("priceChange", {}).get("h1", 0)
                if price_change < -30:  # Only skip major dumps
                    logging.info(f"[DexScreener] Skip - Dumping: {price_change:.1f}%")
                    return False
            
            # Special case: Always buy PumpFun graduates
            token_name = pool.get("baseToken", {}).get("name", "").lower()
            if PUMPFUN_PRIORITY and "pump" in token_name:
                logging.info(f"[DexScreener] PRIORITY - PumpFun graduate detected!")
                return True
            
            return True
            
        except Exception as e:
            logging.error(f"[DexScreener] Filter error: {e}")
            return False
    
    async def get_stats(self) -> Dict:
        """Get monitor statistics"""
        return {
            "pools_found": self.stats["pools_found"],
            "pools_bought": self.stats["pools_bought"],
            "pools_skipped": self.stats["pools_skipped"],
            "success_rate": (self.stats["pools_bought"] / max(1, self.stats["pools_found"])) * 100,
            "api_failures": self.stats["api_failures"],
            "last_check": self.stats["last_check"]
        }
    
    def stop(self):
        """Stop the monitor"""
        self.running = False

# Global monitor instance
monitor = DexScreenerMonitor()

async def start_dexscreener_monitor():
    """Start the DexScreener monitor"""
    await monitor.start()

async def stop_dexscreener_monitor():
    """Stop the DexScreener monitor"""
    monitor.stop()
    await send_telegram_alert("🛑 DexScreener Monitor stopped")

# For testing
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(start_dexscreener_monitor())
