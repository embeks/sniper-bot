import asyncio
import json
import os
import logging
import time
import re
import base64
from base58 import b58encode, b58decode
from datetime import datetime, timedelta
from dotenv import load_dotenv
import httpx
import random
from dexscreener_monitor import start_dexscreener_monitor
import gc  # Added for garbage collection

# Import buy manager instead of directly from utils to prevent circular dependencies
from buy_manager import execute_buy as buy_token

from utils import (
    is_valid_mint, log_skipped_token, send_telegram_alert,
    get_trending_mints, wait_and_auto_sell, get_liquidity_and_ownership,
    is_bot_running, keypair, BUY_AMOUNT_SOL, BROKEN_TOKENS,
    mark_broken_token, daily_stats_reset_loop,
    update_last_activity, increment_stat, record_skip,
    listener_status, last_seen_token
)
from solders.pubkey import Pubkey
from raydium_aggregator import RaydiumAggregatorClient

load_dotenv()

# ============================================
# FIX: Proper cache management with size limits
# ============================================
processed_signatures_cache = {}  # signature -> timestamp
CACHE_CLEANUP_INTERVAL = 300  # Clean cache every 5 minutes
last_cache_cleanup = time.time()
MAX_FETCH_RETRIES = 2  # Maximum retries for transaction fetching
MAX_CACHE_SIZE = 100  # Maximum signatures to keep in cache

# Automated cleanup task
CLEANUP_TASK = None

async def automated_cache_cleanup():
    """Run cache cleanup every 60 seconds automatically"""
    while True:
        try:
            await asyncio.sleep(60)
            cleanup_all_caches()
            
            # Also log memory usage
            try:
                import psutil
                process = psutil.Process()
                memory_mb = process.memory_info().rss / 1024 / 1024
                if memory_mb > 400:
                    logging.warning(f"[MEMORY] High usage: {memory_mb:.1f} MB - forcing cleanup")
                    gc.collect()
            except:
                pass
                
        except Exception as e:
            logging.error(f"[CLEANUP] Error: {e}")

async def periodic_cleanup():
    """Run cleanup every 60 seconds"""
    while True:
        try:
            await asyncio.sleep(60)
            cleanup_all_caches()
            gc.collect()
            logging.debug("[CLEANUP] Periodic cleanup completed")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.error(f"[CLEANUP] Error: {e}")

# CONCURRENT PROCESSING CONFIGURATION
MAX_CONCURRENT_TOKENS = int(os.getenv("MAX_CONCURRENT_TOKENS", 3))
CONCURRENT_PROCESSING_ENABLED = os.getenv("CONCURRENT_PROCESSING", "true").lower() == "true"

FORCE_TEST_MINT = os.getenv("FORCE_TEST_MINT")
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
HELIUS_API = os.getenv("HELIUS_API")
RPC_URL = os.getenv("RPC_URL")
SLIPPAGE_BPS = 100
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")

# Enhanced position sizing
SAFE_BUY_AMOUNT = float(os.getenv("SAFE_BUY_AMOUNT", 0.1))
RISKY_BUY_AMOUNT = float(os.getenv("RISKY_BUY_AMOUNT", 0.05))
ULTRA_RISKY_BUY_AMOUNT = float(os.getenv("ULTRA_RISKY_BUY_AMOUNT", 0.01))

# Quality filters - FIXED TO USE CORRECT ENV VARIABLES
MIN_AI_SCORE = float(os.getenv("MIN_AI_SCORE", 0.10))
MIN_HOLDER_COUNT = int(os.getenv("MIN_HOLDER_COUNT", 10))
MAX_TOP_HOLDER_PERCENT = float(os.getenv("MAX_TOP_HOLDER_PERCENT", 35))
MIN_BUYS_COUNT = int(os.getenv("MIN_BUYS_COUNT", 5))
MIN_BUY_SELL_RATIO = float(os.getenv("MIN_BUY_SELL_RATIO", 1.5))
RUG_LP_THRESHOLD = float(os.getenv("RUG_LP_THRESHOLD", 1.5))  # Updated to match new env
RISKY_LP_THRESHOLD = 1.0
TREND_SCAN_INTERVAL = int(os.getenv("TREND_SCAN_INTERVAL", 60))  # Now matches env

# Enhanced pool detection thresholds
RAYDIUM_MIN_INDICATORS = int(os.getenv("RAYDIUM_MIN_INDICATORS", 1))  # More aggressive
RAYDIUM_MIN_LOGS = int(os.getenv("RAYDIUM_MIN_LOGS", 20))
PUMPFUN_MIN_INDICATORS = int(os.getenv("PUMPFUN_MIN_INDICATORS", 3))
PUMPFUN_MIN_LOGS = int(os.getenv("PUMPFUN_MIN_LOGS", 1))  # More aggressive

# Pool verification settings
POOL_CHECK_DELAY = float(os.getenv("POOL_CHECK_DELAY", 1.0))
POOL_CHECK_TIMEOUT = float(os.getenv("POOL_CHECK_TIMEOUT", 30.0))
MIN_POOL_CHECK_INTERVAL = float(os.getenv("MIN_POOL_CHECK_INTERVAL", 0.5))

# Anti-duplicate settings
DUPLICATE_CHECK_WINDOW = int(os.getenv("DUPLICATE_CHECK_WINDOW", 300))
MAX_BUYS_PER_TOKEN = int(os.getenv("MAX_BUYS_PER_TOKEN", 1))
BLACKLIST_AFTER_BUY = os.getenv("BLACKLIST_AFTER_BUY", "true").lower() == "true"

# Disable Jupiter mempool if configured
SKIP_JUPITER_MEMPOOL = os.getenv("SKIP_JUPITER_MEMPOOL", "true").lower() == "true"

# PumpFun Migration Settings
PUMPFUN_MIGRATION_BUY = float(os.getenv("PUMPFUN_MIGRATION_BUY", 0.1))
PUMPFUN_EARLY_BUY = float(os.getenv("PUMPFUN_EARLY_AMOUNT", 0.05))
PUMPFUN_GRADUATION_MC = int(os.getenv("PUMPFUN_MAX_MARKET_CAP", 50000))  # Use env value
ENABLE_PUMPFUN_MIGRATION = os.getenv("ENABLE_PUMPFUN_MIGRATION", "true").lower() == "true"
MIN_LP_FOR_PUMPFUN = float(os.getenv("MIN_LP_FOR_PUMPFUN", 1.0))

# Pool initialization delays
MEMPOOL_DELAY_MS = float(os.getenv("MEMPOOL_DELAY_MS", 200))
PUMPFUN_INIT_DELAY = float(os.getenv("PUMPFUN_INIT_DELAY", 1.0))

# ============================================
# MOMENTUM SCANNER CONFIGURATION - FIXED
# ============================================
MOMENTUM_SCANNER_ENABLED = os.getenv("MOMENTUM_SCANNER", "true").lower() == "true"
MOMENTUM_AUTO_BUY = os.getenv("MOMENTUM_AUTO_BUY", "true").lower() == "true"
MIN_SCORE_AUTO_BUY = int(os.getenv("MIN_SCORE_AUTO_BUY", 3))
MIN_SCORE_ALERT = int(os.getenv("MIN_SCORE_ALERT", 3))

# FIXED: Now using correct env variable names
MOMENTUM_MIN_1H_GAIN = float(os.getenv("MOMENTUM_MIN_1H_GAIN", 50))
MOMENTUM_MAX_1H_GAIN = float(os.getenv("MOMENTUM_MAX_1H_GAIN", 200))
MOMENTUM_MIN_LIQUIDITY = float(os.getenv("MOMENTUM_MIN_LIQUIDITY", 2000))  # Now uses correct env
MOMENTUM_MAX_MC = float(os.getenv("MOMENTUM_MAX_MC", 500000))
MOMENTUM_MIN_HOLDERS = int(os.getenv("MOMENTUM_MIN_HOLDERS", 100))
MOMENTUM_MAX_HOLDERS = int(os.getenv("MOMENTUM_MAX_HOLDERS", 2000))
MOMENTUM_MIN_AGE_HOURS = float(os.getenv("MOMENTUM_MIN_AGE_HOURS", 2))
MOMENTUM_MAX_AGE_HOURS = float(os.getenv("MOMENTUM_MAX_AGE_HOURS", 24))

MOMENTUM_POSITION_5_SCORE = float(os.getenv("MOMENTUM_POSITION_5_SCORE", 0.20))
MOMENTUM_POSITION_4_SCORE = float(os.getenv("MOMENTUM_POSITION_4_SCORE", 0.15))
MOMENTUM_POSITION_3_SCORE = float(os.getenv("MOMENTUM_POSITION_3_SCORE", 0.10))
MOMENTUM_TEST_POSITION = float(os.getenv("MOMENTUM_TEST_POSITION", 0.02))

PRIME_HOURS = [21, 22, 23, 0, 1, 2, 3]
REDUCED_HOURS = list(range(6, 21))

MOMENTUM_SCAN_INTERVAL = int(os.getenv("MOMENTUM_SCAN_INTERVAL", 120))
MAX_MOMENTUM_TOKENS = 20

# Track momentum tokens
momentum_analyzed = {}
momentum_bought = set()

# Global tracking sets - FIX: Add size limits
seen_tokens = set()
BLACKLIST = set()
TASKS = []

# Enhanced tracking for pool detection
pumpfun_tokens = {}
migration_watch_list = set()
already_bought = set()
recent_buy_attempts = {}
pool_verification_cache = {}
detected_pools = {}
pending_pool_checks = {}  # Tokens waiting for pool creation

# Track concurrent processing
tokens_being_processed = set()

raydium = RaydiumAggregatorClient(RPC_URL)

last_alert_sent = {"Raydium": 0, "Jupiter": 0, "PumpFun": 0, "Moonshot": 0}
alert_cooldown_sec = 1800

SYSTEM_PROGRAMS = [
    "11111111111111111111111111111111",
    "ComputeBudget111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
    "srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX",
]

# ============================================
# FIX: Cache cleanup function with size limits
# ============================================
def cleanup_all_caches():
    """FIXED: Clean up all caches to prevent memory leaks with proper limits"""
    global processed_signatures_cache, seen_tokens, momentum_analyzed
    global pool_verification_cache, detected_pools, recent_buy_attempts
    global pending_pool_checks
    
    current_time = time.time()
    
    # Clean signature cache - keep only last 25 entries
    if len(processed_signatures_cache) > 25:
        sorted_sigs = sorted(processed_signatures_cache.items(), key=lambda x: x[1], reverse=True)
        processed_signatures_cache = dict(sorted_sigs[:25])
        logging.debug(f"[CACHE] Cleaned signatures, kept 25 most recent")
    
    # Clean seen_tokens - keep only last 100
    if len(seen_tokens) > 100:
        token_list = list(seen_tokens)
        seen_tokens = set(token_list[-100:])
        logging.debug(f"[CACHE] Cleaned tokens, kept 100 most recent")
    
    # Clean momentum_analyzed - remove older than 30 minutes
    old_momentum = [token for token, data in momentum_analyzed.items() 
                     if current_time - data.get("timestamp", 0) > 1800]
    for token in old_momentum:
        del momentum_analyzed[token]
    
    # Clean recent_buy_attempts - remove older than 5 minutes
    old_attempts = [token for token, timestamp in recent_buy_attempts.items() 
                    if current_time - timestamp > 300]
    for token in old_attempts:
        del recent_buy_attempts[token]
    
    # Clean pending_pool_checks - remove older than 60 seconds
    old_pending = [token for token, data in pending_pool_checks.items()
                   if current_time - data.get("timestamp", 0) > 60]
    for token in old_pending:
        del pending_pool_checks[token]
    
    # Clear other caches if too large
    if len(pool_verification_cache) > 50:
        # Keep only recent entries
        recent_entries = sorted(
            [(k, v) for k, v in pool_verification_cache.items() if not k.endswith("_time")],
            key=lambda x: pool_verification_cache.get(f"{x[0]}_time", 0),
            reverse=True
        )[:25]
        pool_verification_cache.clear()
        for k, v in recent_entries:
            pool_verification_cache[k] = v
            pool_verification_cache[f"{k}_time"] = current_time
    
    if len(detected_pools) > 50:
        # Keep only 50 most recent pools
        if len(detected_pools) > 50:
            items = list(detected_pools.items())
            detected_pools.clear()
            detected_pools.update(dict(items[-50:]))
    
    # Force garbage collection
    gc.collect()
    
    logging.info(f"[CACHE] Cleanup complete - Sigs: {len(processed_signatures_cache)}, "
                 f"Tokens: {len(seen_tokens)}, Pools: {len(detected_pools)}")

# ============================================
# ENHANCED POOL DETECTION FUNCTIONS
# ============================================
async def wait_for_pool_creation(mint: str, source: str, timeout: float = POOL_CHECK_TIMEOUT):
    """
    Wait for a pool to be created for a token
    """
    start_time = time.time()
    check_count = 0
    
    logging.info(f"[{source}] Token {mint[:8]}... detected, waiting for pool creation...")
    
    # Add to pending pool checks
    pending_pool_checks[mint] = {
        "source": source,
        "timestamp": time.time(),
        "checks": 0
    }
    
    while time.time() - start_time < timeout:
        check_count += 1
        
        # Check if pool exists via multiple methods
        pool_exists = await verify_pool_exists(mint)
        
        if pool_exists:
            logging.info(f"[{source}] ✅ Pool found for {mint[:8]}... after {check_count} checks!")
            
            # Get liquidity
            lp_data = await get_liquidity_and_ownership(mint)
            lp_amount = lp_data.get("liquidity", 0) if lp_data else 0
            
            if lp_amount > 0:
                # Remove from pending
                pending_pool_checks.pop(mint, None)
                return True, lp_amount
            
        # Update check count
        if mint in pending_pool_checks:
            pending_pool_checks[mint]["checks"] = check_count
        
        # Dynamic wait time - start fast, slow down over time
        if check_count < 10:
            await asyncio.sleep(MIN_POOL_CHECK_INTERVAL)
        elif check_count < 20:
            await asyncio.sleep(1.0)
        else:
            await asyncio.sleep(2.0)
    
    # Timeout reached
    pending_pool_checks.pop(mint, None)
    logging.info(f"[{source}] ⏱️ Timeout waiting for pool creation for {mint[:8]}...")
    return False, 0

async def verify_pool_exists(mint: str) -> bool:
    """Enhanced pool verification with multiple methods"""
    try:
        # Check cache first
        if mint in pool_verification_cache:
            cache_time = pool_verification_cache.get(f"{mint}_time", 0)
            if time.time() - cache_time < 10:  # Cache for 10 seconds
                return pool_verification_cache[mint]
        
        # Method 1: Check detected pools
        if mint in detected_pools:
            pool_verification_cache[mint] = True
            pool_verification_cache[f"{mint}_time"] = time.time()
            return True
        
        # Method 2: Check Raydium
        pool = raydium.find_pool_realtime(mint)
        if pool:
            detected_pools[mint] = pool
            pool_verification_cache[mint] = True
            pool_verification_cache[f"{mint}_time"] = time.time()
            return True
        
        # Method 3: Check Jupiter Quote API (most reliable)
        try:
            # Try to get a quote for swapping SOL to this token
            quote_url = f"https://quote-api.jup.ag/v6/quote?inputMint=So11111111111111111111111111111111111111112&outputMint={mint}&amount=1000000000"
            
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(quote_url)
                if resp.status_code == 200:
                    data = resp.json()
                    # If we get routes, pool exists
                    if data.get("routePlan") or data.get("data"):
                        pool_verification_cache[mint] = True
                        pool_verification_cache[f"{mint}_time"] = time.time()
                        return True
        except:
            pass
        
        # Method 4: Check Jupiter Price API
        try:
            url = f"https://price.jup.ag/v4/price?ids={mint}"
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    if mint in data.get("data", {}):
                        pool_verification_cache[mint] = True
                        pool_verification_cache[f"{mint}_time"] = time.time()
                        return True
        except:
            pass
        
        # No pool found
        pool_verification_cache[mint] = False
        pool_verification_cache[f"{mint}_time"] = time.time()
        return False
        
    except Exception as e:
        logging.error(f"Pool verification error for {mint[:8]}...: {e}")
        return False

def is_pool_initialization_transaction(logs: list, account_keys: list, name: str) -> bool:
    """
    FIXED: Enhanced detection for ACTUAL pool initialization transactions
    """
    if name == "Raydium":
        # Look for specific pool initialization patterns
        pool_init_indicators = 0
        has_liquidity_add = False
        has_pool_create = False
        
        for log in logs:
            log_lower = log.lower()
            
            # Strong indicators of pool creation
            if "initialize2" in log_lower or "initializeinstruction2" in log_lower:
                pool_init_indicators += 5
                has_pool_create = True
            
            if "init_pc_amount" in log_lower or "init_coin_amount" in log_lower:
                pool_init_indicators += 4
                has_liquidity_add = True
            
            if "opentime" in log_lower:
                pool_init_indicators += 3
            
            if "amm_v4" in log_lower and "initialize" in log_lower:
                pool_init_indicators += 4
                has_pool_create = True
            
            # Check for liquidity addition
            if "deposit" in log_lower and "liquidity" in log_lower:
                pool_init_indicators += 3
                has_liquidity_add = True
            
            if "create_pool" in log_lower or "new_pool" in log_lower:
                pool_init_indicators += 5
                has_pool_create = True
            
            # Raydium specific success messages
            if "success" in log_lower and any(x in log_lower for x in ["pool", "amm", "liquidity"]):
                pool_init_indicators += 2
        
        # FIXED: More aggressive detection
        return pool_init_indicators >= 5 or (has_pool_create and has_liquidity_add)
    
    elif name == "PumpFun":
        # PumpFun graduation to Raydium
        graduation_indicators = 0
        has_migration = False
        
        for log in logs:
            log_lower = log.lower()
            
            if "migration" in log_lower or "graduated" in log_lower:
                graduation_indicators += 5
                has_migration = True
            
            if "raydium" in log_lower and "create" in log_lower:
                graduation_indicators += 4
            
            if "complete" in log_lower and "bonding" in log_lower:
                graduation_indicators += 3
            
            if "liquidity" in log_lower and "added" in log_lower:
                graduation_indicators += 3
        
        return graduation_indicators >= 8 and has_migration
    
    return False

# ============================================
# FIX: Fixed transaction fetching without infinite recursion
# ============================================
async def fetch_transaction_accounts(signature: str, rpc_url: str = None, retry_count: int = 0) -> list:
    """
    FIXED: Fetch transaction details with proper recursion prevention
    """
    global processed_signatures_cache, last_cache_cleanup
    
    # Prevent infinite loops
    if retry_count > MAX_FETCH_RETRIES:
        logging.warning(f"[TX FETCH] Max retries reached for {signature[:8]}...")
        return []
    
    # Check cache first
    if signature in processed_signatures_cache:
        logging.debug(f"[TX FETCH] Already processed {signature[:8]}...")
        return []
    
    # Mark as processing
    processed_signatures_cache[signature] = time.time()
    
    # Periodic cache cleanup
    current_time = time.time()
    if current_time - last_cache_cleanup > CACHE_CLEANUP_INTERVAL:
        cleanup_all_caches()
        last_cache_cleanup = current_time
    
    try:
        if not rpc_url:
            rpc_url = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
            if HELIUS_API:
                rpc_url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API}"
        
        async with httpx.AsyncClient(timeout=5) as client:
            for encoding in ["jsonParsed", "json"]:
                try:
                    response = await client.post(
                        rpc_url,
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "getTransaction",
                            "params": [
                                signature,
                                {
                                    "encoding": encoding,
                                    "maxSupportedTransactionVersion": 0,
                                    "commitment": "confirmed"
                                }
                            ]
                        }
                    )
                    
                    if response.status_code == 200:
                        data = response.json()
                        if "result" in data and data["result"]:
                            result = data["result"]
                            account_keys = []
                            
                            # Extract accounts from transaction
                            if "transaction" in result:
                                tx = result["transaction"]
                                if "message" in tx:
                                    msg = tx["message"]
                                    
                                    if "accountKeys" in msg:
                                        for key in msg["accountKeys"]:
                                            if isinstance(key, str):
                                                account_keys.append(key)
                                            elif isinstance(key, dict):
                                                pubkey = key.get("pubkey") or key.get("address")
                                                if pubkey:
                                                    account_keys.append(pubkey)
                            
                            # Deduplicate and validate
                            seen = set()
                            unique_keys = []
                            for key in account_keys:
                                if key and key not in seen and len(key) == 44 and key not in SYSTEM_PROGRAMS:
                                    try:
                                        Pubkey.from_string(key)
                                        seen.add(key)
                                        unique_keys.append(key)
                                    except:
                                        pass
                            
                            if unique_keys:
                                return unique_keys[:10]  # Limit to 10 accounts max
                            
                except asyncio.TimeoutError:
                    logging.warning(f"[TX FETCH] Timeout for {encoding} encoding")
                    continue
                except Exception as e:
                    logging.debug(f"[TX FETCH] {encoding} encoding failed: {e}")
                    continue
            
            # FIX: Only try fallback once, no recursion back
            if retry_count == 0:
                logging.debug(f"[TX FETCH] Trying fallback for {signature[:8]}...")
                return await fetch_pumpfun_token_from_logs(signature, rpc_url, 1)
            
            return []
        
    except Exception as e:
        logging.error(f"[TX FETCH] Error: {e}")
        return []

async def fetch_pumpfun_token_from_logs(signature: str, rpc_url: str = None, retry_count: int = 0) -> list:
    """
    FIXED: Fallback method that never calls back to prevent recursion
    """
    # Never recurse back to fetch_transaction_accounts
    if retry_count >= MAX_FETCH_RETRIES:
        return []
    
    if signature in processed_signatures_cache:
        return []
    
    try:
        if not rpc_url:
            rpc_url = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
            if HELIUS_API:
                rpc_url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API}"
        
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(
                rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTransaction",
                    "params": [
                        signature,
                        {
                            "encoding": "base64",
                            "maxSupportedTransactionVersion": 0,
                            "commitment": "confirmed"
                        }
                    ]
                }
            )
            
            if response.status_code == 200:
                data = response.json()
                if "result" in data and data["result"]:
                    result = data["result"]
                    potential_mints = []
                    
                    # Check logs for addresses
                    if "meta" in result and "logMessages" in result["meta"]:
                        logs = result["meta"]["logMessages"]
                        
                        for log in logs[:50]:  # Process max 50 logs
                            if any(keyword in log.lower() for keyword in ["mint", "token", "create", "initialize"]):
                                matches = re.findall(r'[1-9A-HJ-NP-Za-km-z]{43,44}', log)
                                for match in matches[:10]:
                                    if match not in SYSTEM_PROGRAMS and len(match) == 44:
                                        try:
                                            Pubkey.from_string(match)
                                            if match not in potential_mints:
                                                potential_mints.append(match)
                                        except:
                                            pass
                    
                    return potential_mints[:5]  # Return max 5 mints
        
        return []
        
    except Exception as e:
        logging.debug(f"[FALLBACK] Error: {e}")
        return []

# ============================================
# QUALITY CHECK FUNCTIONS - FIXED
# ============================================
async def is_quality_token(mint: str, lp_amount: float) -> tuple:
    """
    Enhanced quality check for tokens - FIXED to use proper thresholds
    Returns (is_quality, reason)
    """
    try:
        if mint in already_bought:
            return False, "Already bought"
        
        if mint in recent_buy_attempts:
            time_since_attempt = time.time() - recent_buy_attempts[mint]
            if time_since_attempt < DUPLICATE_CHECK_WINDOW:
                return False, f"Recent buy attempt {time_since_attempt:.0f}s ago"
        
        # FIXED: Use more aggressive threshold
        min_lp_threshold = float(os.getenv("RUG_LP_THRESHOLD", 1.5))
        if lp_amount < min_lp_threshold:
            return False, f"Low liquidity: {lp_amount:.2f} SOL (min: {min_lp_threshold})"
        
        # Try to get token metrics from DexScreener
        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
            async with httpx.AsyncClient(timeout=5, verify=False) as client:
                response = await client.get(url)
                if response.status_code == 200:
                    data = response.json()
                    if "pairs" in data and len(data["pairs"]) > 0:
                        pair = data["pairs"][0]
                        
                        volume_h24 = float(pair.get("volume", {}).get("h24", 0))
                        min_volume = float(os.getenv("MIN_VOLUME_USD", 2000))  # Use env value
                        if volume_h24 < min_volume:
                            logging.info(f"Low volume ${volume_h24:.0f} but proceeding")
                        
                        txns = pair.get("txns", {})
                        buys_h1 = txns.get("h1", {}).get("buys", 1)
                        sells_h1 = txns.get("h1", {}).get("sells", 1)
                        
                        if sells_h1 > 0 and buys_h1 / sells_h1 < 0.3:  # More aggressive
                            return False, f"Bad buy/sell ratio: {buys_h1}/{sells_h1}"
                        
                        price_change_h1 = float(pair.get("priceChange", {}).get("h1", 0))
                        if price_change_h1 < -50:
                            return False, f"Dumping hard: {price_change_h1:.1f}% in 1h"
                        
                        return True, "Quality token"
        except:
            pass
        
        if lp_amount >= min_lp_threshold:
            return True, f"Good liquidity ({lp_amount:.1f} SOL), proceeding without data"
        
        return False, "Failed quality checks"
        
    except Exception as e:
        logging.error(f"Quality check error: {e}")
        if lp_amount >= RUG_LP_THRESHOLD:
            return True, "Quality check error but good LP"
        return False, "Quality check error"

async def check_pumpfun_graduation(mint: str) -> bool:
    """Check if a PumpFun token is ready to graduate"""
    try:
        url = f"https://frontend-api.pump.fun/coins/{mint}"
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(url)
            if response.status_code == 200:
                data = response.json()
                market_cap = data.get("usd_market_cap", 0)
                
                # Use env value for graduation threshold
                graduation_mc = int(os.getenv("PUMPFUN_MAX_MARKET_CAP", 50000))
                if market_cap > graduation_mc * 0.9:
                    logging.info(f"[PumpFun] {mint[:8]}... approaching graduation: ${market_cap:.0f}")
                    return True
    except Exception as e:
        logging.debug(f"[PumpFun] Graduation check error: {e}")
    
    return False

# ============================================
# ENHANCED TOKEN PROCESSING WITH POOL WAITING
# ============================================
async def process_potential_token(potential_mint: str, name: str, pool_id: str = None):
    """Process a single token - wait for pool creation"""
    global tokens_being_processed
    
    try:
        if potential_mint in tokens_being_processed:
            logging.debug(f"[{name}] {potential_mint[:8]}... already being processed")
            return
        
        tokens_being_processed.add(potential_mint)
        
        # Check if this is actually a pool initialization
        if pool_id:
            # This is a confirmed pool creation
            detected_pools[potential_mint] = pool_id
            raydium.register_new_pool(pool_id, potential_mint)
            logging.info(f"[{name}] ✅ Pool {pool_id[:8]}... created for token {potential_mint[:8]}...")
            
            # Process immediately
            await process_raydium_token(potential_mint, name)
        else:
            # Token detected, need to wait for pool
            logging.info(f"[{name}] Token {potential_mint[:8]}... detected, checking for pool...")
            
            # Wait for pool creation
            pool_found, lp_amount = await wait_for_pool_creation(potential_mint, name)
            
            if pool_found and lp_amount > 0:
                logging.info(f"[{name}] Pool found with {lp_amount:.2f} SOL liquidity!")
                
                # Process based on platform
                if name == "PumpFun":
                    await process_pumpfun_token(potential_mint)
                else:
                    await process_raydium_token(potential_mint, name)
            else:
                logging.info(f"[{name}] No pool created for {potential_mint[:8]}... - skipping")
                record_skip("no_pool_created")
            
    except Exception as e:
        logging.error(f"Error processing {potential_mint[:8]}...: {e}")
    finally:
        tokens_being_processed.discard(potential_mint)

async def process_pumpfun_token(potential_mint: str):
    """Process PumpFun token"""
    if potential_mint in BROKEN_TOKENS or potential_mint in BLACKLIST or potential_mint in already_bought:
        return
    
    logging.info(f"[PUMPFUN] Evaluating token: {potential_mint[:8]}...")
    
    graduated = await check_pumpfun_graduation(potential_mint)
    if graduated and potential_mint in pumpfun_tokens:
        pumpfun_tokens[potential_mint]["migrated"] = True
    
    lp_data = await get_liquidity_and_ownership(potential_mint)
    lp_amount = lp_data.get("liquidity", 0) if lp_data else 0
    
    recent_buy_attempts[potential_mint] = time.time()
    
    if graduated:
        buy_amount = PUMPFUN_MIGRATION_BUY
        buy_reason = "PumpFun Graduate"
    else:
        buy_amount = PUMPFUN_EARLY_BUY
        buy_reason = "PumpFun Early Entry"
    
    await send_telegram_alert(
        f"🎯 PUMPFUN TOKEN WITH POOL\n\n"
        f"Token: `{potential_mint}`\n"
        f"Status: {buy_reason}\n"
        f"Liquidity: {lp_amount:.2f} SOL\n"
        f"Buy Amount: {buy_amount} SOL\n\n"
        f"Attempting snipe..."
    )
    
    original_amount = os.getenv("BUY_AMOUNT_SOL")
    os.environ["BUY_AMOUNT_SOL"] = str(buy_amount)
    
    try:
        success = await buy_token(potential_mint)
        
        if success:
            already_bought.add(potential_mint)
            if BLACKLIST_AFTER_BUY:
                BLACKLIST.add(potential_mint)
            
            await send_telegram_alert(
                f"✅ PUMPFUN SNIPE SUCCESS!\n"
                f"Token: {potential_mint[:16]}...\n"
                f"Amount: {buy_amount} SOL\n"
                f"Type: {buy_reason}\n"
                f"Monitoring for profits..."
            )
            asyncio.create_task(wait_and_auto_sell(potential_mint))
        else:
            await send_telegram_alert(
                f"❌ PumpFun snipe failed\n"
                f"Token: {potential_mint[:16]}..."
            )
            mark_broken_token(potential_mint, 0)
    except Exception as e:
        logging.error(f"[PUMPFUN] Buy error: {e}")
        await send_telegram_alert(f"❌ PumpFun buy error: {str(e)[:100]}")
    finally:
        if original_amount:
            os.environ["BUY_AMOUNT_SOL"] = original_amount

async def process_raydium_token(potential_mint: str, name: str):
    """Process Raydium token with confirmed pool"""
    if potential_mint in BROKEN_TOKENS or potential_mint in BLACKLIST or potential_mint in already_bought:
        return
    
    logging.info(f"[{name}] Processing token with confirmed pool: {potential_mint[:8]}...")
    
    # Get liquidity
    lp_data = await get_liquidity_and_ownership(potential_mint)
    lp_amount = lp_data.get("liquidity", 0) if lp_data else 0
    
    if lp_amount < 0.5:
        logging.info(f"[{name}] Very low liquidity ({lp_amount:.2f} SOL) but checking quality")
    
    is_quality, reason = await is_quality_token(potential_mint, lp_amount)
    
    if not is_quality:
        logging.info(f"[{name}] Skipping {potential_mint[:8]}... - {reason}")
        record_skip("quality_check")
        return
    
    # FIXED: Use proper thresholds
    min_lp = float(os.getenv("RUG_LP_THRESHOLD", 1.5))
    
    if lp_amount >= min_lp * 2:
        risk_level = "SAFE"
        buy_amount = SAFE_BUY_AMOUNT
    elif lp_amount >= min_lp:
        risk_level = "MEDIUM"
        buy_amount = RISKY_BUY_AMOUNT
    else:
        risk_level = "HIGH"
        buy_amount = ULTRA_RISKY_BUY_AMOUNT
    
    recent_buy_attempts[potential_mint] = time.time()
    
    await send_telegram_alert(
        f"✅ POOL CREATED - QUALITY TOKEN ✅\n\n"
        f"Platform: {name}\n"
        f"Token: `{potential_mint}`\n"
        f"Liquidity: {lp_amount:.2f} SOL\n"
        f"Risk: {risk_level}\n"
        f"Buy Amount: {buy_amount} SOL\n\n"
        f"Attempting snipe..."
    )
    
    original_amount = os.getenv("BUY_AMOUNT_SOL")
    os.environ["BUY_AMOUNT_SOL"] = str(buy_amount)
    
    try:
        success = await buy_token(potential_mint)
        if success:
            already_bought.add(potential_mint)
            if BLACKLIST_AFTER_BUY:
                BLACKLIST.add(potential_mint)
            
            await send_telegram_alert(
                f"✅ SNIPED AT POOL CREATION!\n"
                f"Token: {potential_mint[:16]}...\n"
                f"Amount: {buy_amount} SOL\n"
                f"Risk: {risk_level}\n"
                f"Monitoring for profits..."
            )
            asyncio.create_task(wait_and_auto_sell(potential_mint))
        else:
            await send_telegram_alert(
                f"❌ Snipe failed\n"
                f"Token: {potential_mint[:16]}..."
            )
            mark_broken_token(potential_mint, 0)
    except Exception as e:
        logging.error(f"[{name}] Buy error: {e}")
        await send_telegram_alert(f"❌ Buy error: {str(e)[:100]}")
    finally:
        if original_amount:
            os.environ["BUY_AMOUNT_SOL"] = original_amount

# ============================================
# ENHANCED MEMPOOL LISTENER WITH POOL DETECTION
# ============================================
async def mempool_listener(name, program_id=None):
    """Enhanced mempool listener that detects POOL CREATION not token creation"""
    if not HELIUS_API:
        logging.warning(f"[{name}] HELIUS_API not set, skipping mempool listener")
        await send_telegram_alert(f"⚠️ {name} listener disabled (no Helius API key)")
        return
    
    if name == "Jupiter" and SKIP_JUPITER_MEMPOOL:
        logging.info(f"[{name}] Mempool monitoring disabled via config")
        await send_telegram_alert(f"📌 {name} mempool disabled (too noisy)")
        return
    
    url = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API}"
    retry_attempts = 0
    max_retries = 10
    retry_delay = 10
    heartbeat_interval = 30
    max_inactive = 300
    
    if program_id is None:
        if name == "Raydium":
            program_id = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
        elif name == "Jupiter":
            program_id = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
        elif name == "PumpFun":
            program_id = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
        elif name == "Moonshot":
            program_id = "MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG"
        else:
            logging.error(f"Unknown listener: {name}")
            return
    
    while retry_attempts < max_retries:
        ws = None
        watchdog_task = None
        
        try:
            import websockets
            ws = await websockets.connect(
                url, 
                ping_interval=20,
                ping_timeout=10,
                close_timeout=10,
                max_size=10**7
            )
            
            await ws.send(json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "logsSubscribe",
                "params": [
                    {"mentions": [program_id]},
                    {"commitment": "processed"}
                ]
            }))
            
            response = await asyncio.wait_for(ws.recv(), timeout=10)
            response_data = json.loads(response)
            
            if "result" not in response_data:
                logging.error(f"[{name}] Failed to subscribe: {response_data}")
                raise Exception("Subscription failed")
            
            subscription_id = response_data["result"]
            logging.info(f"[{name}] Listener subscribed with ID: {subscription_id}")
            
            # Update status
            current_time = time.time()
            last_alert = last_alert_sent.get(name, 0)
            if current_time - last_alert > 1800:
                await send_telegram_alert(f"📡 {name} listener ACTIVE - Monitoring POOL CREATIONS")
                last_alert_sent[name] = current_time
            
            listener_status[name] = "ACTIVE"
            last_seen_token[name] = time.time()
            retry_attempts = 0
            
            # Heartbeat watchdog
            async def heartbeat_watchdog():
                while True:
                    await asyncio.sleep(heartbeat_interval)
                    now = time.time()
                    elapsed = now - last_seen_token[name]
                    
                    if elapsed < heartbeat_interval * 2:
                        logging.debug(f"✅ {name} listener heartbeat OK ({int(elapsed)}s)")
                    elif elapsed > max_inactive:
                        logging.error(f"⚠️ {name} listener inactive for {int(elapsed)}s")
                        raise Exception("ListenerInactive")
            
            watchdog_task = asyncio.create_task(heartbeat_watchdog())
            
            processed_txs = set()
            transaction_counter = 0
            pool_creations_found = 0
            token_creations_found = 0
            
            # Process messages
            while True:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=60)
                    data = json.loads(msg)
                    
                    last_seen_token[name] = time.time()
                    
                    if "params" in data:
                        result = data.get("params", {}).get("result", {})
                        value = result.get("value", {})
                        logs = value.get("logs", [])
                        account_keys = value.get("accountKeys", [])
                        signature = value.get("signature", "")
                        
                        if signature in processed_txs:
                            continue
                        processed_txs.add(signature)
                        
                        # Limit processed_txs size
                        if len(processed_txs) > 1000:
                            processed_txs.clear()
                        
                        transaction_counter += 1
                        
                        if transaction_counter % 100 == 0:
                            logging.info(f"[{name}] Processed {transaction_counter} txs, found {pool_creations_found} pool creations, {token_creations_found} token creations")
                            # Periodic cleanup
                            if transaction_counter % 500 == 0:
                                cleanup_all_caches()
                        
                        # Check if this is a POOL creation (not just token creation)
                        is_pool_creation = is_pool_initialization_transaction(logs, account_keys, name)
                        
                        if is_pool_creation:
                            pool_creations_found += 1
                            logging.info(f"[{name}] 🏊 POOL CREATION DETECTED! Total pools: {pool_creations_found}")
                            
                            # Fetch full transaction if needed
                            if len(account_keys) == 0:
                                logging.info(f"[{name}] Fetching full transaction for pool...")
                                try:
                                    fetch_task = asyncio.create_task(fetch_transaction_accounts(signature))
                                    account_keys = await asyncio.wait_for(fetch_task, timeout=5)
                                except asyncio.TimeoutError:
                                    logging.warning(f"[{name}] Transaction fetch timeout for {signature[:8]}...")
                                    continue
                                
                                if len(account_keys) == 0:
                                    logging.warning(f"[{name}] Could not fetch account keys")
                                    continue
                            
                            # Process pool creation
                            await process_pool_creation(name, account_keys, logs)
                        else:
                            # Check if it's just a token creation (for tracking)
                            is_token_creation = detect_token_creation(name, logs)
                            if is_token_creation:
                                token_creations_found += 1
                                logging.debug(f"[{name}] Token creation detected (no pool yet) - Total: {token_creations_found}")
                
                except asyncio.TimeoutError:
                    continue
                except websockets.exceptions.ConnectionClosed as e:
                    logging.warning(f"[{name}] WebSocket closed: {e}")
                    break
                    
        except Exception as e:
            logging.error(f"[{name} ERROR] {str(e)}")
            listener_status[name] = f"RETRYING ({retry_attempts + 1})"
            
        finally:
            # Proper cleanup
            if watchdog_task and not watchdog_task.done():
                watchdog_task.cancel()
                try:
                    await watchdog_task
                except asyncio.CancelledError:
                    pass
            
            if ws:
                try:
                    await ws.close()
                except:
                    pass
            
            # Force garbage collection
            gc.collect()
            
            retry_attempts += 1
            
            if retry_attempts >= max_retries:
                msg = f"⚠️ {name} listener failed after {max_retries} attempts"
                await send_telegram_alert(msg)
                listener_status[name] = "FAILED"
                break
            
            wait_time = min(retry_delay * (2 ** (retry_attempts - 1)), 300)
            logging.info(f"[{name}] Retrying in {wait_time}s (attempt {retry_attempts}/{max_retries})")
            await asyncio.sleep(wait_time)

def detect_token_creation(name: str, logs: list) -> bool:
    """Detect if this is just a token creation (no pool)"""
    if name == "PumpFun":
        for log in logs:
            log_lower = log.lower()
            if "create" in log_lower and "token" in log_lower and "pool" not in log_lower:
                return True
    return False

async def process_pool_creation(name: str, account_keys: list, logs: list):
    """Process actual pool creation"""
    if CONCURRENT_PROCESSING_ENABLED:
        tasks = []
        tokens_to_process = []
        
        # Extract pool ID and token mint
        pool_id = None
        token_mint = None
        
        for i, key in enumerate(account_keys[:MAX_CONCURRENT_TOKENS + 5]):
            if isinstance(key, dict):
                key = key.get("pubkey", "") or key.get("address", "")
            
            if key in SYSTEM_PROGRAMS or len(key) != 44:
                continue
            
            if key == "So11111111111111111111111111111111111111112":
                continue
            
            # First non-system account is often the pool ID
            if not pool_id and i < 3:
                pool_id = key
            
            # Look for token mint
            if key not in seen_tokens and key not in already_bought:
                try:
                    Pubkey.from_string(key)
                    if not token_mint:
                        token_mint = key
                    seen_tokens.add(key)
                except:
                    continue
        
        if token_mint and pool_id:
            logging.info(f"[{name}] Processing pool {pool_id[:8]}... for token {token_mint[:8]}...")
            task = asyncio.create_task(
                process_potential_token(token_mint, name, pool_id)
            )
            tasks.append(task)
        
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logging.error(f"[{name}] Task {i} failed: {result}")
    else:
        # Sequential processing for pool creation
        pool_id = None
        token_mint = None
        
        for i, key in enumerate(account_keys):
            if isinstance(key, dict):
                key = key.get("pubkey", "") or key.get("address", "")
            
            if key in SYSTEM_PROGRAMS or len(key) != 44:
                continue
            
            if key == "So11111111111111111111111111111111111111112":
                continue
            
            if not pool_id and i < 3:
                pool_id = key
            
            if key not in seen_tokens and key not in already_bought:
                try:
                    Pubkey.from_string(key)
                    if not token_mint:
                        token_mint = key
                    seen_tokens.add(key)
                    
                    if token_mint and pool_id:
                        await process_potential_token(token_mint, name, pool_id)
                        break
                except:
                    continue

# ============================================
# SCANNER FUNCTIONS (keeping all existing ones)
# ============================================

async def raydium_graduation_scanner():
    """Check if PumpFun tokens graduated to Raydium"""
    if not ENABLE_PUMPFUN_MIGRATION:
        logging.info("[Graduation Scanner] Disabled via config")
        return
        
    await send_telegram_alert("🎓 Graduation Scanner ACTIVE - Checking every 30 seconds")
    
    while True:
        try:
            if not is_bot_running():
                await asyncio.sleep(30)
                continue
            
            recent_tokens = list(pumpfun_tokens.keys())[-100:] if pumpfun_tokens else []
            
            for mint in recent_tokens:
                if mint in already_bought:
                    continue
                    
                try:
                    pool = raydium.find_pool_realtime(mint)
                    if pool:
                        logging.info(f"[GRADUATION SCANNER] {mint[:8]}... has Raydium pool!")
                        
                        if mint in pumpfun_tokens:
                            pumpfun_tokens[mint]["migrated"] = True
                        
                        lp_data = await get_liquidity_and_ownership(mint)
                        lp_amount = lp_data.get("liquidity", 0) if lp_data else 0
                        
                        min_lp = float(os.getenv("RUG_LP_THRESHOLD", 1.5))
                        if lp_amount >= min_lp:
                            already_bought.add(mint)
                            
                            await send_telegram_alert(
                                f"🎓 GRADUATION DETECTED!\n\n"
                                f"Token: `{mint}`\n"
                                f"Liquidity: {lp_amount:.2f} SOL\n"
                                f"Action: BUYING NOW!"
                            )
                            
                            original_amount = os.getenv("BUY_AMOUNT_SOL")
                            os.environ["BUY_AMOUNT_SOL"] = str(PUMPFUN_MIGRATION_BUY)
                            
                            try:
                                success = await buy_token(mint)
                                if success:
                                    await send_telegram_alert(
                                        f"✅ GRADUATION SNIPE SUCCESS!\n"
                                        f"Token: {mint[:16]}...\n"
                                        f"Amount: {PUMPFUN_MIGRATION_BUY} SOL\n"
                                        f"Like JOYBAIT - potential 27x!"
                                    )
                                    asyncio.create_task(wait_and_auto_sell(mint))
                                else:
                                    await send_telegram_alert(f"❌ Graduation snipe failed for {mint[:16]}...")
                            finally:
                                if original_amount:
                                    os.environ["BUY_AMOUNT_SOL"] = original_amount
                        else:
                            logging.info(f"[GRADUATION SCANNER] {mint[:8]}... has pool but low LP: {lp_amount:.2f} SOL")
                            
                except Exception as e:
                    pass
            
            await asyncio.sleep(30)
            
        except Exception as e:
            logging.error(f"[Graduation Scanner] Error: {e}")
            await asyncio.sleep(30)

async def pumpfun_migration_monitor():
    """Monitor PumpFun tokens for migration to Raydium"""
    if not ENABLE_PUMPFUN_MIGRATION:
        logging.info("[Migration Monitor] Disabled via config")
        return
        
    await send_telegram_alert("🎯 PumpFun Migration Monitor ACTIVE")
    
    while True:
        try:
            if not is_bot_running():
                await asyncio.sleep(10)
                continue
            
            for mint in list(migration_watch_list):
                if mint in pumpfun_tokens and not pumpfun_tokens[mint].get("migrated", False):
                    lp_data = await get_liquidity_and_ownership(mint)
                    
                    if lp_data and lp_data.get("liquidity", 0) > 0:
                        pumpfun_tokens[mint]["migrated"] = True
                        migration_watch_list.discard(mint)
                        
                        await send_telegram_alert(
                            f"🚨 PUMPFUN MIGRATION DETECTED 🚨\n\n"
                            f"Token: `{mint}`\n"
                            f"Status: Graduated to Raydium!\n"
                            f"Liquidity: {lp_data.get('liquidity', 0):.2f} SOL\n"
                            f"Action: SNIPING NOW!"
                        )
                        
                        original_amount = os.getenv("BUY_AMOUNT_SOL")
                        os.environ["BUY_AMOUNT_SOL"] = str(PUMPFUN_MIGRATION_BUY)
                        
                        try:
                            success = await buy_token(mint)
                            if success:
                                await send_telegram_alert(
                                    f"✅ MIGRATION SNIPE SUCCESS!\n"
                                    f"Token: {mint[:16]}...\n"
                                    f"Amount: {PUMPFUN_MIGRATION_BUY} SOL\n"
                                    f"Type: PumpFun → Raydium Migration"
                                )
                                asyncio.create_task(wait_and_auto_sell(mint))
                            else:
                                await send_telegram_alert(f"❌ Migration snipe failed for {mint[:16]}...")
                        finally:
                            if original_amount:
                                os.environ["BUY_AMOUNT_SOL"] = original_amount
            
            if int(time.time()) % 60 == 0:
                await scan_pumpfun_graduations()
            
            await asyncio.sleep(5)
            
        except Exception as e:
            logging.error(f"[Migration Monitor] Error: {e}")
            await asyncio.sleep(10)

async def scan_pumpfun_graduations():
    """Scan PumpFun for tokens about to graduate"""
    try:
        url = "https://frontend-api.pump.fun/coins?offset=0&limit=50&sort=usd_market_cap&order=desc"
        
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url)
            if response.status_code == 200:
                coins = response.json()
                
                graduation_mc = int(os.getenv("PUMPFUN_MAX_MARKET_CAP", 50000))
                
                for coin in coins[:20]:
                    mint = coin.get("mint")
                    market_cap = coin.get("usd_market_cap", 0)
                    
                    if not mint:
                        continue
                    
                    if market_cap > graduation_mc * 0.8:
                        if mint not in pumpfun_tokens:
                            pumpfun_tokens[mint] = {
                                "discovered": time.time(),
                                "migrated": False,
                                "market_cap": market_cap
                            }
                        
                        if mint not in migration_watch_list:
                            migration_watch_list.add(mint)
                            logging.info(f"[PumpFun] Added {mint[:8]}... to migration watch (MC: ${market_cap:.0f})")
                            
                            if market_cap > graduation_mc * 0.95:
                                await send_telegram_alert(
                                    f"⚠️ GRADUATION IMMINENT\n\n"
                                    f"Token: `{mint}`\n"
                                    f"Market Cap: ${market_cap:,.0f}\n"
                                    f"Graduation at: ${graduation_mc:,.0f}\n"
                                    f"Status: {(market_cap/graduation_mc)*100:.1f}% complete\n\n"
                                    f"Monitoring for Raydium migration..."
                                )
    except Exception as e:
        logging.error(f"[PumpFun Scan] Error: {e}")

# ============================================
# TRENDING SCANNER - FIXED
# ============================================

MIN_LP_USD = float(os.getenv("MIN_LP_USD", 1500))  # Use env value
MIN_VOLUME_USD = float(os.getenv("MIN_VOLUME_USD", 2000))  # Use env value
seen_trending = set()

async def get_trending_pairs_dexscreener():
    """Fetch trending pairs from DexScreener"""
    url = "https://api.dexscreener.com/latest/dex/pairs/solana"
    
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True, verify=False) as client:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/json",
                    "Cache-Control": "no-cache"
                }
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    pairs = data.get("pairs", [])
                    if pairs:
                        logging.info(f"[Trending] DexScreener returned {len(pairs)} pairs")
                    return pairs
                else:
                    logging.debug(f"DexScreener returned status {resp.status_code}")
                    except Exception as e:
            logging.error(f"Birdeye attempt {attempt + 1} failed: {e}")
            await asyncio.sleep(2)
    
    return None

async def trending_scanner():
    """Scan for quality trending tokens"""
    global seen_trending
    consecutive_failures = 0
    max_consecutive_failures = 5
    
    while True:
        try:
            if not is_bot_running():
                await asyncio.sleep(5)
                continue

            pairs = await get_trending_pairs_dexscreener()
            source = "DEXScreener"
            
            if not pairs:
                pairs = await get_trending_pairs_birdeye()
                source = "Birdeye"
            
            if not pairs:
                consecutive_failures += 1
                if consecutive_failures >= max_consecutive_failures:
                    logging.warning(f"[Trending Scanner] Both APIs unavailable")
                    consecutive_failures = 0
                    await asyncio.sleep(TREND_SCAN_INTERVAL * 2)
                    continue
                
                await asyncio.sleep(TREND_SCAN_INTERVAL)
                continue
            
            consecutive_failures = 0
            processed = 0
            quality_finds = 0
            
            for pair in pairs[:10]:
                mint = pair.get("baseToken", {}).get("address")
                lp_usd = float(pair.get("liquidity", {}).get("usd", 0))
                vol_usd = float(pair.get("volume", {}).get("h24", 0) or pair.get("volume", {}).get("h1", 0))
                
                price_change_h1 = float(pair.get("priceChange", {}).get("h1", 0) if isinstance(pair.get("priceChange"), dict) else 0)
                price_change_h24 = float(pair.get("priceChange", {}).get("h24", 0) if isinstance(pair.get("priceChange"), dict) else 0)
                
                if not mint or mint in seen_trending or mint in BLACKLIST or mint in BROKEN_TOKENS or mint in already_bought:
                    continue
                
                is_pumpfun_grad = mint in pumpfun_tokens and pumpfun_tokens[mint].get("migrated", False)
                
                min_lp = MIN_LP_USD / 2 if is_pumpfun_grad else MIN_LP_USD
                min_vol = MIN_VOLUME_USD / 2 if is_pumpfun_grad else MIN_VOLUME_USD
                
                if lp_usd < min_lp:
                    logging.debug(f"[SKIP] {mint[:8]}... - Low LP: ${lp_usd:.0f} (min: ${min_lp})")
                    continue
                    
                if vol_usd < min_vol:
                    logging.debug(f"[SKIP] {mint[:8]}... - Low volume: ${vol_usd:.0f} (min: ${min_vol})")
                    continue
                
                if price_change_h1 < -30 and not is_pumpfun_grad:
                    logging.debug(f"[SKIP] {mint[:8]}... - Dumping: {price_change_h1:.1f}% in 1h")
                    continue
                    
                seen_trending.add(mint)
                processed += 1
                increment_stat("tokens_scanned", 1)
                update_last_activity()
                
                is_mooning = price_change_h1 > 50 or price_change_h24 > 100
                has_momentum = price_change_h1 > 20 and vol_usd > 50000
                
                if is_mooning or has_momentum or is_pumpfun_grad:
                    quality_finds += 1
                    
                    alert_msg = f"🔥 QUALITY TRENDING TOKEN 🔥\n\n"
                    if is_pumpfun_grad:
                        alert_msg = f"🎓 PUMPFUN GRADUATE TRENDING 🎓\n\n"
                    
                    await send_telegram_alert(
                        alert_msg +
                        f"Token: `{mint}`\n"
                        f"Liquidity: ${lp_usd:,.0f}\n"
                        f"Volume 24h: ${vol_usd:,.0f}\n"
                        f"Price Change:\n"
                        f"• 1h: {price_change_h1:+.1f}%\n"
                        f"• 24h: {price_change_h24:+.1f}%\n"
                        f"Source: {source}\n\n"
                        f"Attempting to buy..."
                    )
                    
                    try:
                        original_amount = None
                        if is_pumpfun_grad:
                            original_amount = os.getenv("BUY_AMOUNT_SOL")
                            os.environ["BUY_AMOUNT_SOL"] = str(PUMPFUN_MIGRATION_BUY)
                        
                        success = await buy_token(mint)
                        if success:
                            already_bought.add(mint)
                            if BLACKLIST_AFTER_BUY:
                                BLACKLIST.add(mint)
                            asyncio.create_task(wait_and_auto_sell(mint))
                            
                        if is_pumpfun_grad and original_amount:
                            os.environ["BUY_AMOUNT_SOL"] = original_amount
                    except Exception as e:
                        logging.error(f"[Trending] Buy error: {e}")
                else:
                    logging.info(f"[Trending] {mint[:8]}... good metrics but not enough momentum")
            
            if processed > 0:
                logging.info(f"[Trending Scanner] Processed {processed} tokens, found {quality_finds} quality opportunities")
            
            await asyncio.sleep(TREND_SCAN_INTERVAL)
            
        except Exception as e:
            logging.error(f"[Trending Scanner ERROR] {e}")
            await asyncio.sleep(TREND_SCAN_INTERVAL)

async def rug_filter_passes(mint: str) -> bool:
    """Check if token passes basic rug filters"""
    try:
        data = await get_liquidity_and_ownership(mint)
        min_lp = float(os.getenv("RUG_LP_THRESHOLD", 1.5))
        
        if mint in pumpfun_tokens and pumpfun_tokens[mint].get("migrated", False):
            min_lp = min_lp / 2
        
        if not data or data.get("liquidity", 0) < min_lp:
            logging.info(f"[RUG CHECK] {mint[:8]}... has {data.get('liquidity', 0):.2f} SOL (min: {min_lp})")
            return False
        return True
    except Exception as e:
        logging.error(f"Rug check error for {mint}: {e}")
        return False

# ============================================
# MOMENTUM SCANNER - FIXED
# ============================================

def detect_chart_pattern(price_data: list) -> str:
    """
    Detect if chart shows good or bad patterns
    Returns: 'steady_climb', 'pump_dump', 'vertical', 'consolidating', 'unknown'
    """
    if not price_data or len(price_data) < 5:
        return "unknown"
    
    changes = []
    for i in range(1, len(price_data)):
        change = ((price_data[i] - price_data[i-1]) / price_data[i-1]) * 100
        changes.append(change)
    
    max_change = max(changes) if changes else 0
    avg_change = sum(changes) / len(changes) if changes else 0
    positive_candles = sum(1 for c in changes if c > 0)
    
    if max_change > 100:
        return "vertical"
    
    if len(changes) > 2:
        first_half = changes[:len(changes)//2]
        second_half = changes[len(changes)//2:]
        if sum(first_half) > 50 and sum(second_half) < -30:
            return "pump_dump"
    
    if positive_candles >= len(changes) * 0.6 and 0 < avg_change < 20:
        return "steady_climb"
    
    if -5 < avg_change < 5 and max_change < 20:
        return "consolidating"
    
    return "unknown"

async def score_momentum_token(token_data: dict) -> tuple:
    """
    Score a token based on momentum criteria
    Returns: (score, [list of signals that passed])
    """
    score = 0
    signals = []
    
    try:
        price_change_1h = float(token_data.get("priceChange", {}).get("h1", 0))
        price_change_5m = float(token_data.get("priceChange", {}).get("m5", 0))
        liquidity_usd = float(token_data.get("liquidity", {}).get("usd", 0))
        volume_h24 = float(token_data.get("volume", {}).get("h24", 0))
        market_cap = float(token_data.get("marketCap", 0))
        created_at = token_data.get("pairCreatedAt", 0)
        
        if created_at:
            age_hours = (time.time() * 1000 - created_at) / (1000 * 60 * 60)
        else:
            age_hours = 0
        
        price_history = token_data.get("priceHistory", [])
        pattern = detect_chart_pattern(price_history) if price_history else "unknown"
        
        # Momentum rules
        if MOMENTUM_MIN_1H_GAIN <= price_change_1h <= MOMENTUM_MAX_1H_GAIN:
            score += 1
            signals.append(f"✅ 1h gain: {price_change_1h:.1f}%")
        elif price_change_1h > MOMENTUM_MAX_1H_GAIN:
            signals.append(f"❌ Too late: {price_change_1h:.1f}% gain")
            return (0, signals)
        
        if price_change_5m > 0:
            score += 1
            signals.append(f"✅ Still pumping: {price_change_5m:.1f}% on 5m")
        else:
            signals.append(f"⚠️ Cooling off: {price_change_5m:.1f}% on 5m")
        
        if liquidity_usd > 0:
            vol_liq_ratio = volume_h24 / liquidity_usd
            if vol_liq_ratio > 2:
                score += 1
                signals.append(f"✅ Volume/Liq ratio: {vol_liq_ratio:.1f}")
        
        if liquidity_usd >= MOMENTUM_MIN_LIQUIDITY:
            score += 1
            signals.append(f"✅ Liquidity: ${liquidity_usd:,.0f}")
        else:
            signals.append(f"❌ Low liquidity: ${liquidity_usd:,.0f}")
            return (0, signals)
        
        if market_cap < MOMENTUM_MAX_MC:
            score += 1
            signals.append(f"✅ Room to grow: ${market_cap:,.0f} MC")
        else:
            signals.append(f"⚠️ High MC: ${market_cap:,.0f}")
        
        if MOMENTUM_MIN_AGE_HOURS <= age_hours <= MOMENTUM_MAX_AGE_HOURS:
            score += 0.5
            signals.append(f"✅ Good age: {age_hours:.1f}h old")
        
        if pattern == "steady_climb":
            score += 0.5
            signals.append("✅ Steady climb pattern")
        elif pattern == "consolidating":
            score += 0.25
            signals.append("✅ Consolidating pattern")
        elif pattern in ["vertical", "pump_dump"]:
            signals.append(f"❌ Bad pattern: {pattern}")
            score -= 1
        
        if price_change_5m < 0 and price_change_1h > 50:
            score += 0.25
            signals.append("✅ Pulling back from high")
        
    except Exception as e:
        logging.error(f"Error scoring momentum token: {e}")
        return (0, [f"Error: {str(e)}"])
    
    return (int(score), signals)

async def fetch_top_gainers() -> list:
    """Fetch top gaining tokens from DexScreener"""
    try:
        url = "https://api.dexscreener.com/latest/dex/pairs/solana"
        
        async with httpx.AsyncClient(timeout=10, verify=False) as client:
            response = await client.get(url)
            
            if response.status_code == 200:
                data = response.json()
                pairs = data.get("pairs", [])
                
                filtered_pairs = []
                for pair in pairs:
                    if pair.get("dexId") in ["raydium", "orca"]:
                        price_change_1h = float(pair.get("priceChange", {}).get("h1", 0))
                        liquidity_usd = float(pair.get("liquidity", {}).get("usd", 0))
                        
                        if (MOMENTUM_MIN_1H_GAIN <= price_change_1h <= MOMENTUM_MAX_1H_GAIN * 1.5 and
                            liquidity_usd >= MOMENTUM_MIN_LIQUIDITY * 0.8):
                            filtered_pairs.append(pair)
                
                filtered_pairs.sort(key=lambda x: float(x.get("priceChange", {}).get("h1", 0)), reverse=True)
                
                return filtered_pairs[:MAX_MOMENTUM_TOKENS]
                
    except Exception as e:
        logging.error(f"Error fetching gainers: {e}")
    
    return []

async def momentum_scanner():
    """Elite Momentum Scanner - Finds pumping tokens"""
    if not MOMENTUM_SCANNER_ENABLED:
        logging.info("[Momentum Scanner] Disabled via configuration")
        return
    
    await send_telegram_alert(
        "🔥 MOMENTUM SCANNER ACTIVE 🔥\n\n"
        f"Mode: {'HYBRID AUTO-BUY' if MOMENTUM_AUTO_BUY else 'ALERT ONLY'}\n"
        f"Auto-buy threshold: {MIN_SCORE_AUTO_BUY}/5\n"
        f"Alert threshold: {MIN_SCORE_ALERT}/5\n"
        f"Target: 50-200% gainers\n"
        f"Position sizes: 0.02-0.20 SOL\n"
        f"Concurrent Processing: {'ON' if CONCURRENT_PROCESSING_ENABLED else 'OFF'}\n\n"
        f"Hunting for pumps..."
    )
    
    consecutive_errors = 0
    
    while True:
        try:
            if not is_bot_running():
                await asyncio.sleep(30)
                continue
            
            current_hour = datetime.now().hour
            is_prime_time = current_hour in PRIME_HOURS
            
            if not is_prime_time and current_hour not in REDUCED_HOURS:
                await asyncio.sleep(MOMENTUM_SCAN_INTERVAL)
                continue
            
            top_gainers = await fetch_top_gainers()
            
            if not top_gainers:
                consecutive_errors += 1
                if consecutive_errors > 5:
                    logging.warning("[Momentum Scanner] Multiple fetch failures")
                    await asyncio.sleep(MOMENTUM_SCAN_INTERVAL * 2)
                continue
            
            consecutive_errors = 0
            candidates_found = 0
            
            for token_data in top_gainers:
                try:
                    token_address = token_data.get("baseToken", {}).get("address")
                    token_symbol = token_data.get("baseToken", {}).get("symbol", "Unknown")
                    
                    if not token_address:
                        continue
                    
                    if token_address in momentum_analyzed:
                        last_check = momentum_analyzed[token_address].get("timestamp", 0)
                        if time.time() - last_check < 300:
                            continue
                    
                    if token_address in momentum_bought or token_address in already_bought:
                        continue
                    
                    score, signals = await score_momentum_token(token_data)
                    
                    momentum_analyzed[token_address] = {
                        "score": score,
                        "timestamp": time.time(),
                        "signals": signals,
                        "symbol": token_symbol
                    }
                    
                    if score < MIN_SCORE_ALERT:
                        continue
                    
                    candidates_found += 1
                    
                    if score >= MIN_SCORE_AUTO_BUY and MOMENTUM_AUTO_BUY:
                        position_size = MOMENTUM_POSITION_5_SCORE if score >= 5 else MOMENTUM_POSITION_4_SCORE
                        
                        if not is_prime_time:
                            position_size *= 0.5
                        
                        await send_telegram_alert(
                            f"🎯 MOMENTUM AUTO-BUY 🎯\n\n"
                            f"Token: {token_symbol} ({token_address[:8]}...)\n"
                            f"Score: {score}/5 ⭐\n"
                            f"Position: {position_size} SOL\n\n"
                            f"Signals:\n" + "\n".join(signals[:5]) + "\n\n"
                            f"Executing..."
                        )
                        
                        original_amount = os.getenv("BUY_AMOUNT_SOL")
                        os.environ["BUY_AMOUNT_SOL"] = str(position_size)
                        
                        success = await buy_token(token_address)
                        
                        if original_amount:
                            os.environ["BUY_AMOUNT_SOL"] = original_amount
                        
                        if success:
                            momentum_bought.add(token_address)
                            already_bought.add(token_address)
                            await send_telegram_alert(
                                f"✅ MOMENTUM BUY SUCCESS\n"
                                f"Token: {token_symbol}\n"
                                f"Amount: {position_size} SOL\n"
                                f"Strategy: Momentum Play\n\n"
                                f"Monitoring with your exit rules..."
                            )
                            asyncio.create_task(wait_and_auto_sell(token_address))
                        
                    elif score >= MIN_SCORE_ALERT:
                        await send_telegram_alert(
                            f"🔔 MOMENTUM OPPORTUNITY 🔔\n\n"
                            f"Token: {token_symbol} ({token_address[:8]}...)\n"
                            f"Score: {score}/5 ⭐\n"
                            f"Suggested: {MOMENTUM_POSITION_3_SCORE} SOL\n\n"
                            f"Signals:\n" + "\n".join(signals[:5]) + "\n\n"
                            f"Use /forcebuy {token_address} to execute"
                        )
                    
                    await asyncio.sleep(1)
                    
                except Exception as e:
                    logging.error(f"Error analyzing momentum token: {e}")
                    continue
            
            if candidates_found > 0:
                logging.info(f"[Momentum Scanner] Found {candidates_found} candidates this scan")
            
            await asyncio.sleep(MOMENTUM_SCAN_INTERVAL)
            
        except Exception as e:
            logging.error(f"[Momentum Scanner] Error in main loop: {e}")
            await asyncio.sleep(MOMENTUM_SCAN_INTERVAL)

async def check_momentum_score(mint: str) -> dict:
    """Check momentum score for a specific token"""
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        
        async with httpx.AsyncClient(timeout=10, verify=False) as client:
            response = await client.get(url)
            
            if response.status_code == 200:
                data = response.json()
                pairs = data.get("pairs", [])
                
                if pairs:
                    best_pair = pairs[0]
                    score, signals = await score_momentum_token(best_pair)
                    
                    if score >= 5:
                        recommendation = MOMENTUM_POSITION_5_SCORE
                    elif score >= 4:
                        recommendation = MOMENTUM_POSITION_4_SCORE
                    elif score >= 3:
                        recommendation = MOMENTUM_POSITION_3_SCORE
                    else:
                        recommendation = MOMENTUM_TEST_POSITION
                    
                    return {
                        "score": score,
                        "signals": signals,
                        "recommendation": recommendation
                    }
        
    except Exception as e:
        logging.error(f"Error checking momentum score: {e}")
    
    return {"score": 0, "signals": ["Failed to fetch data"], "recommendation": 0}

# ============================================
# MAIN SNIPER FUNCTIONS
# ============================================

async def start_sniper():
    """Start the ELITE sniper bot with POOL DETECTION"""
    mode_text = "POOL DETECTION Mode + Momentum Scanner + CONCURRENT PROCESSING"
    TASKS.append(asyncio.create_task(start_dexscreener_monitor()))
    
    # Start periodic cleanup
    global CLEANUP_TASK
    CLEANUP_TASK = asyncio.create_task(automated_cache_cleanup())
    TASKS.append(CLEANUP_TASK)
    
    await send_telegram_alert(
        f"💰 POOL DETECTION SNIPER LAUNCHING 💰\n\n"
        f"Mode: {mode_text}\n"
        f"Min LP: {RUG_LP_THRESHOLD} SOL\n"
        f"Min AI Score: {MIN_AI_SCORE}\n"
        f"Momentum Mode: {'HYBRID' if MOMENTUM_AUTO_BUY else 'ALERTS'}\n"
        f"⚡ CONCURRENT PROCESSING: {MAX_CONCURRENT_TOKENS} tokens in parallel\n"
        f"⚡ PERIODIC CLEANUP: Every 60 seconds\n"
        f"🏊 POOL DETECTION: Waiting for actual liquidity pools\n"
        f"⏱️ Pool Check Timeout: {POOL_CHECK_TIMEOUT}s\n\n"
        f"Quality filters: ACTIVE ✅\n"
        f"Pool verification: ACTIVE ✅\n"
        f"Memory management: ACTIVE ✅\n"
        f"Ready to snipe REAL POOLS! 🎯"
    )

    if FORCE_TEST_MINT:
        await send_telegram_alert(f"🚨 Forced Test Buy: {FORCE_TEST_MINT}")
        try:
            success = await buy_token(FORCE_TEST_MINT)
            if success:
                await wait_and_auto_sell(FORCE_TEST_MINT)
        except Exception as e:
            logging.error(f"Force buy error: {e}")

    TASKS.append(asyncio.create_task(daily_stats_reset_loop()))
    
    listeners = ["Raydium", "PumpFun", "Moonshot"]
    if not SKIP_JUPITER_MEMPOOL:
        listeners.append("Jupiter")
    
    for listener in listeners:
        TASKS.append(asyncio.create_task(mempool_listener(listener)))
    
    TASKS.append(asyncio.create_task(trending_scanner()))
    
    if MOMENTUM_SCANNER_ENABLED:
        TASKS.append(asyncio.create_task(momentum_scanner()))
        await send_telegram_alert(
            "🔥 Momentum Scanner: ACTIVE\n"
            "Hunting for 50-200% gainers"
        )
    
    if ENABLE_PUMPFUN_MIGRATION:
        TASKS.append(asyncio.create_task(pumpfun_migration_monitor()))
        TASKS.append(asyncio.create_task(raydium_graduation_scanner()))
    
    await send_telegram_alert(f"🎯 POOL DETECTION SNIPER ACTIVE - {mode_text}!")

async def stop_all_tasks():
    """Stop all running tasks"""
    global CLEANUP_TASK
    
    for task in TASKS:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    
    # Also stop the cleanup task
    if CLEANUP_TASK and not CLEANUP_TASK.done():
        CLEANUP_TASK.cancel()
        try:
            await CLEANUP_TASK
        except asyncio.CancelledError:
            pass
    
    TASKS.clear()
    
    # Final cleanup
    cleanup_all_caches()
    gc.collect()
    
    await send_telegram_alert("🛑 All sniper tasks stopped.")

# ============================================
# FORCE BUY FUNCTION
# ============================================

async def start_sniper_with_forced_token(mint: str):
    """Force buy a specific token with MOMENTUM SCORING"""
    try:
        await send_telegram_alert(f"🚨 FORCE BUY: {mint}")
        
        if not is_bot_running():
            await send_telegram_alert(f"⛔ Bot is paused. Cannot force buy {mint}")
            return

        if mint in BROKEN_TOKENS or mint in BLACKLIST or mint in already_bought:
            await send_telegram_alert(f"❌ {mint} is blacklisted, broken, or already bought")
            return

        # Check if pool exists first
        pool_exists = await verify_pool_exists(mint)
        if not pool_exists:
            await send_telegram_alert(
                f"❌ No pool found for {mint}\n"
                f"Token may not have liquidity yet.\n"
                f"Waiting for pool creation..."
            )
            
            # Wait for pool
            pool_found, lp_amount = await wait_for_pool_creation(mint, "FORCEBUY", timeout=30)
            if not pool_found:
                await send_telegram_alert(f"❌ Pool creation timeout for {mint}")
                return

        is_pumpfun = mint in pumpfun_tokens
        
        momentum_data = await check_momentum_score(mint)
        if momentum_data["score"] > 0:
            await send_telegram_alert(
                f"📊 MOMENTUM SCORE CHECK\n\n"
                f"Token: {mint[:8]}...\n"
                f"Score: {momentum_data['score']}/5 ⭐\n"
                f"Signals:\n" + "\n".join(momentum_data['signals'][:5]) + "\n\n"
                f"Recommended position: {momentum_data['recommendation']} SOL"
            )
            
            if momentum_data['score'] >= 3:
                buy_amount = momentum_data['recommendation']
            else:
                buy_amount = PUMPFUN_MIGRATION_BUY if is_pumpfun else BUY_AMOUNT_SOL
        else:
            buy_amount = PUMPFUN_MIGRATION_BUY if is_pumpfun else BUY_AMOUNT_SOL
        
        logging.info(f"[FORCEBUY] Attempting forced buy for {mint} with {buy_amount} SOL")

        original_amount = os.getenv("BUY_AMOUNT_SOL")
        os.environ["BUY_AMOUNT_SOL"] = str(buy_amount)
        
        try:
            result = await buy_token(mint)
            if result:
                already_bought.add(mint)
                if BLACKLIST_AFTER_BUY:
                    BLACKLIST.add(mint)
                    
                token_type = "PumpFun Graduate" if is_pumpfun else "Standard"
                if momentum_data.get("score", 0) >= 3:
                    token_type = f"Momentum Play (Score: {momentum_data['score']}/5)"
                    
                await send_telegram_alert(
                    f"✅ Force buy successful\n"
                    f"Token: {mint}\n"
                    f"Type: {token_type}\n"
                    f"Amount: {buy_amount} SOL"
                )
                await wait_and_auto_sell(mint)
            else:
                await send_telegram_alert(f"❌ Force buy failed for {mint}")
        finally:
            if original_amount:
                os.environ["BUY_AMOUNT_SOL"] = original_amount
            
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        await send_telegram_alert(f"❌ Force buy error: {e}")
        logging.exception(f"[FORCEBUY] Exception: {e}\n{tb}")

if __name__ == "__main__":
    print("Sniper logic module loaded. This should be imported, not run directly.")
    print("Use main.py to start the bot.")
    
    logging.info("=" * 60)
    logging.info("POOL DETECTION SNIPER CONFIGURATION LOADED")
    logging.info("=" * 60)
    logging.info(f"RPC URL: {RPC_URL[:30] if RPC_URL else 'Not set'}...")
    logging.info(f"Rug LP Threshold: {RUG_LP_THRESHOLD} SOL")
    logging.info(f"Pool Check Timeout: {POOL_CHECK_TIMEOUT}s")
    logging.info(f"Pool Check Delay: {POOL_CHECK_DELAY}s")
    logging.info(f"Momentum Scanner: {'ENABLED' if MOMENTUM_SCANNER_ENABLED else 'DISABLED'}")
    logging.info(f"⚡ CONCURRENT PROCESSING: {'ENABLED' if CONCURRENT_PROCESSING_ENABLED else 'DISABLED'}")
    logging.info(f"⚡ Max Concurrent Tokens: {MAX_CONCURRENT_TOKENS}")
    logging.info(f"⚡ PERIODIC CLEANUP: Every 60 seconds")
    logging.info(f"🏊 POOL DETECTION: ENABLED - Waiting for actual liquidity pools")
    logging.info("=" * 60)
