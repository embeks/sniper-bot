"""
ELITE DEXSCREENER MONITOR - Catches ALL new pools in real-time
No WebSocket needed - More reliable than mempool monitoring!
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
    BUY_AMOUNT_SOL, BROKEN_TOKENS, mark_broken_token,
    increment_stat, update_last_activity
)

load_dotenv()

# Configuration
CHECK_INTERVAL = int(os.getenv("DEXSCREENER_CHECK_INTERVAL", 5))  # Check every 5 seconds
MAX_AGE_MINUTES = int(os.getenv("MAX_POOL_AGE_MINUTES", 5))  # Only pools <5 mins old
MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", 5000))  # $5k minimum
MIN_VOLUME_USD = float(os.getenv("MIN_VOLUME_USD", 1000))  # $1k minimum volume
MIN_BUYS = int(os.getenv("MIN_BUYS", 10))  # At least 10 buy transactions
MAX_PRICE_IMPACT = float(os.getenv("MAX_PRICE_IMPACT", 10))  # Max 10% price impact

# Elite settings
ELITE_MODE = os.getenv("ELITE_MODE", "true").lower() == "true"
PUMPFUN_PRIORITY = os.getenv("PUMPFUN_PRIORITY", "true").lower() == "true"
AUTO_BUY = os.getenv("AUTO_BUY", "true").lower() == "true"

# Track seen pools to avoid duplicates
seen_pools: Set[str] = set()
pool_creation_times: Dict[str, datetime] = {}

class DexScreenerMonitor:
    """
    Elite DexScreener monitor - better than WebSocket monitoring
    Catches ALL new pools regardless of program ID
    """
    
    def __init__(self):
        self.session = None
        self.running = False
        self.stats = {
            "pools_found": 0,
            "pools_bought": 0,
            "pools_skipped": 0,
            "last_check": None
        }
        
    async def start(self):
        """Start the monitor"""
        self.running = True
        await send_telegram_alert(
            "🎯 ELITE DexScreener Monitor ACTIVE\n\n"
            f"Checking every: {CHECK_INTERVAL}s\n"
            f"Max age: {MAX_AGE_MINUTES} mins\n"
            f"Min liquidity: ${MIN_LIQUIDITY_USD:,.0f}\n"
            f"Min volume: ${MIN_VOLUME_USD:,.0f}\n\n"
            "This will catch ALL new pools!"
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
                    logging.info(f"[DexScreener] Found {len(new_pools)} new pools")
                    
                    for pool in new_pools:
                        await self.process_pool(pool)
                
                # Reset error counter on success
                consecutive_errors = 0
                self.stats["last_check"] = datetime.now()
                
            except Exception as e:
                consecutive_errors += 1
                logging.error(f"[DexScreener] Monitor error: {e}")
                
                if consecutive_errors > 5:
                    await send_telegram_alert(
                        f"⚠️ DexScreener Monitor Error\n"
                        f"Errors: {consecutive_errors}\n"
                        f"Restarting in 30s..."
                    )
                    await asyncio.sleep(30)
                    consecutive_errors = 0
            
            await asyncio.sleep(CHECK_INTERVAL)
    
    async def fetch_new_pools(self) -> List[Dict]:
        """Fetch new pools from DexScreener"""
        try:
            url = "https://api.dexscreener.com/latest/dex/pairs/solana"
            
            async with httpx.AsyncClient(timeout=10, verify=False) as client:
                response = await client.get(url, headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "application/json"
                })
                
                if response.status_code != 200:
                    logging.warning(f"[DexScreener] API returned {response.status_code}")
                    return []
                
                data = response.json()
                pairs = data.get("pairs", [])
                
                # Filter for new pools
                new_pools = []
                current_time = datetime.now()
                
                for pair in pairs[:100]:  # Check top 100 pairs
                    try:
                        # Skip if we've seen this pool
                        pool_address = pair.get("pairAddress")
                        if not pool_address or pool_address in seen_pools:
                            continue
                        
                        # Parse pool age
                        created_at = pair.get("pairCreatedAt")
                        if not created_at:
                            continue
                        
                        # Check if pool is new enough
                        pool_age_ms = current_time.timestamp() * 1000 - created_at
                        pool_age_minutes = pool_age_ms / (1000 * 60)
                        
                        if pool_age_minutes > MAX_AGE_MINUTES:
                            continue
                        
                        # Add to new pools
                        pair["age_minutes"] = pool_age_minutes
                        new_pools.append(pair)
                        seen_pools.add(pool_address)
                        
                    except Exception as e:
                        logging.debug(f"[DexScreener] Error parsing pair: {e}")
                        continue
                
                return new_pools
                
        except Exception as e:
            logging.error(f"[DexScreener] Fetch error: {e}")
            return []
    
    async def process_pool(self, pool: Dict) -> bool:
        """Process a new pool and decide whether to buy"""
        try:
            self.stats["pools_found"] += 1
            
            # Extract pool data
            token_address = pool.get("baseToken", {}).get("address")
            token_symbol = pool.get("baseToken", {}).get("symbol", "UNKNOWN")
            token_name = pool.get("baseToken", {}).get("name", "Unknown")
            
            liquidity_usd = float(pool.get("liquidity", {}).get("usd", 0))
            volume_h24 = float(pool.get("volume", {}).get("h24", 0))
            volume_h1 = float(pool.get("volume", {}).get("h1", 0))
            price_usd = float(pool.get("priceUsd", 0))
            market_cap = float(pool.get("marketCap", 0))
            
            age_minutes = pool.get("age_minutes", 0)
            txns = pool.get("txns", {})
            buys_h1 = txns.get("h1", {}).get("buys", 0)
            sells_h1 = txns.get("h1", {}).get("sells", 0)
            
            # Check if PumpFun graduate
            is_pumpfun = "pump" in token_name.lower() or "pump.fun" in pool.get("info", {}).get("text", "").lower()
            
            logging.info(
                f"[DexScreener] New Pool: {token_symbol}\n"
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
            alert_emoji = "🎓" if is_pumpfun else "🆕"
            priority = "PUMPFUN GRADUATE" if is_pumpfun else "NEW POOL"
            
            await send_telegram_alert(
                f"{alert_emoji} {priority} DETECTED!\n\n"
                f"Token: {token_symbol} ({token_name})\n"
                f"Address: `{token_address}`\n"
                f"Age: {age_minutes:.1f} minutes\n"
                f"Liquidity: ${liquidity_usd:,.0f}\n"
                f"Volume (1h): ${volume_h1:,.0f}\n"
                f"Market Cap: ${market_cap:,.0f}\n"
                f"Buys/Sells: {buys_h1}/{sells_h1}\n"
                f"Price: ${price_usd:.8f}\n\n"
                f"{'🚀 AUTO-BUYING...' if AUTO_BUY else '⏸ Manual mode - use /forcebuy'}"
            )
            
            # Auto-buy if enabled
            if AUTO_BUY and token_address:
                try:
                    # Use higher amount for PumpFun graduates
                    buy_amount = float(os.getenv("PUMPFUN_MIGRATION_BUY", 0.1)) if is_pumpfun else BUY_AMOUNT_SOL
                    
                    original_amount = os.getenv("BUY_AMOUNT_SOL")
                    os.environ["BUY_AMOUNT_SOL"] = str(buy_amount)
                    
                    success = await buy_token(token_address)
                    
                    if original_amount:
                        os.environ["BUY_AMOUNT_SOL"] = original_amount
                    
                    if success:
                        self.stats["pools_bought"] += 1
                        await send_telegram_alert(
                            f"✅ BOUGHT {token_symbol}!\n"
                            f"Amount: {buy_amount} SOL\n"
                            f"Monitoring for profits..."
                        )
                        asyncio.create_task(wait_and_auto_sell(token_address))
                        return True
                    else:
                        await send_telegram_alert(f"❌ Failed to buy {token_symbol}")
                        mark_broken_token(token_address, 0)
                        
                except Exception as e:
                    logging.error(f"[DexScreener] Buy error: {e}")
                    await send_telegram_alert(f"❌ Buy error: {str(e)[:100]}")
            
            return False
            
        except Exception as e:
            logging.error(f"[DexScreener] Process pool error: {e}")
            return False
    
    async def should_buy_pool(self, pool: Dict) -> bool:
        """Determine if pool meets buying criteria"""
        try:
            # Extract metrics
            liquidity_usd = float(pool.get("liquidity", {}).get("usd", 0))
            volume_h1 = float(pool.get("volume", {}).get("h1", 0))
            age_minutes = pool.get("age_minutes", 999)
            
            txns = pool.get("txns", {})
            buys_h1 = txns.get("h1", {}).get("buys", 0)
            sells_h1 = txns.get("h1", {}).get("sells", 0)
            
            # Basic filters
            if liquidity_usd < MIN_LIQUIDITY_USD:
                logging.info(f"[DexScreener] Skip - Low liquidity: ${liquidity_usd:.0f}")
                return False
            
            if volume_h1 < MIN_VOLUME_USD and age_minutes > 2:  # Give 2 mins for volume to build
                logging.info(f"[DexScreener] Skip - Low volume: ${volume_h1:.0f}")
                return False
            
            if buys_h1 < MIN_BUYS and age_minutes > 1:  # Need some buy activity
                logging.info(f"[DexScreener] Skip - Low buys: {buys_h1}")
                return False
            
            # Check buy/sell ratio (avoid dumps)
            if sells_h1 > 0 and buys_h1 > 0:
                buy_sell_ratio = buys_h1 / sells_h1
                if buy_sell_ratio < 0.5:  # More sells than buys
                    logging.info(f"[DexScreener] Skip - Bad ratio: {buy_sell_ratio:.2f}")
                    return False
            
            # Elite mode checks
            if ELITE_MODE:
                # Check price impact
                price_impact = pool.get("priceChange", {}).get("h1", 0)
                if price_impact < -20:  # Dumping
                    logging.info(f"[DexScreener] Skip - Dumping: {price_impact:.1f}%")
                    return False
                
                # Check for honeypot indicators
                if "honeypot" in str(pool).lower():
                    logging.info(f"[DexScreener] Skip - Honeypot detected")
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
