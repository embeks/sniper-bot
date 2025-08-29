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

from utils import (
    is_valid_mint, buy_token, log_skipped_token, send_telegram_alert,
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
# CRITICAL FIX: Transaction cache to prevent infinite loops
# ============================================
processed_signatures_cache = {}  # signature -> timestamp
CACHE_CLEANUP_INTERVAL = 300  # Clean cache every 5 minutes
last_cache_cleanup = time.time()
MAX_FETCH_RETRIES = 2  # Maximum retries for transaction fetching

FORCE_TEST_MINT = os.getenv("FORCE_TEST_MINT")
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
HELIUS_API = os.getenv("HELIUS_API")
RUG_LP_THRESHOLD = float(os.getenv("RUG_LP_THRESHOLD", 2.0))
RISKY_LP_THRESHOLD = 1.5
TREND_SCAN_INTERVAL = int(os.getenv("TREND_SCAN_INTERVAL", 60))
RPC_URL = os.getenv("RPC_URL")
SLIPPAGE_BPS = 100
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")

# Enhanced position sizing
SAFE_BUY_AMOUNT = float(os.getenv("SAFE_BUY_AMOUNT", 0.05))
RISKY_BUY_AMOUNT = float(os.getenv("RISKY_BUY_AMOUNT", 0.03))
ULTRA_RISKY_BUY_AMOUNT = float(os.getenv("ULTRA_RISKY_BUY_AMOUNT", 0.01))

# Quality filters - keeping your original values
MIN_AI_SCORE = float(os.getenv("MIN_AI_SCORE", 0.10))
MIN_HOLDER_COUNT = int(os.getenv("MIN_HOLDER_COUNT", 10))
MAX_TOP_HOLDER_PERCENT = float(os.getenv("MAX_TOP_HOLDER_PERCENT", 35))
MIN_BUYS_COUNT = int(os.getenv("MIN_BUYS_COUNT", 5))
MIN_BUY_SELL_RATIO = float(os.getenv("MIN_BUY_SELL_RATIO", 1.2))

# FIX #2: Use ENV variables properly - don't override them
RAYDIUM_MIN_INDICATORS = int(os.getenv("RAYDIUM_MIN_INDICATORS", "3"))
RAYDIUM_MIN_LOGS = int(os.getenv("RAYDIUM_MIN_LOGS", "10"))
PUMPFUN_MIN_INDICATORS = int(os.getenv("PUMPFUN_MIN_INDICATORS", "3"))
PUMPFUN_MIN_LOGS = int(os.getenv("PUMPFUN_MIN_LOGS", "5"))

# Anti-duplicate settings
DUPLICATE_CHECK_WINDOW = int(os.getenv("DUPLICATE_CHECK_WINDOW", 300))
MAX_BUYS_PER_TOKEN = int(os.getenv("MAX_BUYS_PER_TOKEN", 1))
BLACKLIST_AFTER_BUY = os.getenv("BLACKLIST_AFTER_BUY", "true").lower() == "true"

# Disable Jupiter mempool if configured
SKIP_JUPITER_MEMPOOL = os.getenv("SKIP_JUPITER_MEMPOOL", "true").lower() == "true"

# PumpFun Migration Settings
PUMPFUN_MIGRATION_BUY = float(os.getenv("PUMPFUN_MIGRATION_BUY", 0.1))
PUMPFUN_EARLY_BUY = float(os.getenv("PUMPFUN_EARLY_AMOUNT", 0.02))
PUMPFUN_GRADUATION_MC = 69420
ENABLE_PUMPFUN_MIGRATION = os.getenv("ENABLE_PUMPFUN_MIGRATION", "true").lower() == "true"
MIN_LP_FOR_PUMPFUN = float(os.getenv("MIN_LP_FOR_PUMPFUN", 0.5))

# FIXED: Proper delays for pool initialization
MEMPOOL_DELAY_MS = float(os.getenv("MEMPOOL_DELAY_MS", 200))
PUMPFUN_INIT_DELAY = float(os.getenv("PUMPFUN_INIT_DELAY", 1.0))

# ============================================
# MOMENTUM SCANNER CONFIGURATION (YOUR ELITE STRATEGY)
# ============================================

# Core Settings
MOMENTUM_SCANNER_ENABLED = os.getenv("MOMENTUM_SCANNER", "true").lower() == "true"
MOMENTUM_AUTO_BUY = os.getenv("MOMENTUM_AUTO_BUY", "true").lower() == "true"
MIN_SCORE_AUTO_BUY = int(os.getenv("MIN_SCORE_AUTO_BUY", 3))
MIN_SCORE_ALERT = int(os.getenv("MIN_SCORE_ALERT", 2))

# Your Golden Rules
MOMENTUM_MIN_1H_GAIN = float(os.getenv("MOMENTUM_MIN_1H_GAIN", 50))  # 50% minimum
MOMENTUM_MAX_1H_GAIN = float(os.getenv("MOMENTUM_MAX_1H_GAIN", 200))  # 200% maximum
MOMENTUM_MIN_LIQUIDITY = float(os.getenv("MOMENTUM_MIN_LIQUIDITY", 2000))
MOMENTUM_MAX_MC = float(os.getenv("MOMENTUM_MAX_MC", 500000))  # $500k max market cap
MOMENTUM_MIN_HOLDERS = int(os.getenv("MOMENTUM_MIN_HOLDERS", 100))
MOMENTUM_MAX_HOLDERS = int(os.getenv("MOMENTUM_MAX_HOLDERS", 2000))
MOMENTUM_MIN_AGE_HOURS = float(os.getenv("MOMENTUM_MIN_AGE_HOURS", 2))
MOMENTUM_MAX_AGE_HOURS = float(os.getenv("MOMENTUM_MAX_AGE_HOURS", 24))

# Position Sizing
MOMENTUM_POSITION_5_SCORE = float(os.getenv("MOMENTUM_POSITION_5_SCORE", 0.1))
MOMENTUM_POSITION_4_SCORE = float(os.getenv("MOMENTUM_POSITION_4_SCORE", 0.1))
MOMENTUM_POSITION_3_SCORE = float(os.getenv("MOMENTUM_POSITION_3_SCORE", 0.05))
MOMENTUM_TEST_POSITION = float(os.getenv("MOMENTUM_TEST_POSITION", 0.02))

# Trading Hours (AEST)
PRIME_HOURS = [21, 22, 23, 0, 1, 2, 3]  # 9 PM - 3 AM AEST (US market active)
REDUCED_HOURS = list(range(6, 21))  # 6 AM - 9 PM AEST (be pickier)

# Scan Settings
MOMENTUM_SCAN_INTERVAL = int(os.getenv("MOMENTUM_SCAN_INTERVAL", 120))
MAX_MOMENTUM_TOKENS = 20  # Check top 20 gainers

# Track momentum tokens
momentum_analyzed = {}  # token -> {score, timestamp, bought}
momentum_bought = set()  # Prevent duplicate buys

seen_tokens = set()
BLACKLIST = set()
TASKS = []

# Enhanced tracking
pumpfun_tokens = {}
migration_watch_list = set()
already_bought = set()
recent_buy_attempts = {}  # token -> timestamp
pool_verification_cache = {}  # token -> is_verified
detected_pools = {}  # Store pool IDs for tokens

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

async def fetch_transaction_accounts(signature: str, rpc_url: str = None, retry_count: int = 0) -> list:
    """
    FIXED: Fetch transaction details with loop prevention and caching
    """
    global processed_signatures_cache, last_cache_cleanup
    
    # CRITICAL FIX 1: Prevent infinite loops
    if retry_count > MAX_FETCH_RETRIES:
        logging.warning(f"[TX FETCH] Max retries reached for {signature[:8]}...")
        return []
    
    # CRITICAL FIX 2: Check cache first to prevent reprocessing
    if signature in processed_signatures_cache:
        logging.debug(f"[TX FETCH] Already processed {signature[:8]}...")
        return []  # Don't reprocess
    
    # Mark as processing
    processed_signatures_cache[signature] = time.time()
    
    # FIX #6: Improved cache cleanup
    current_time = time.time()
    if current_time - last_cache_cleanup > CACHE_CLEANUP_INTERVAL:
        # Keep only recent signatures (last 5 minutes)
        cutoff_time = current_time - 300
        old_sigs = [sig for sig, ts in processed_signatures_cache.items() 
                   if ts < cutoff_time]
        
        for sig in old_sigs:
            del processed_signatures_cache[sig]
        
        # Also limit total size
        if len(processed_signatures_cache) > 500:
            # Keep only newest 250
            sorted_items = sorted(processed_signatures_cache.items(), 
                                key=lambda x: x[1], reverse=True)
            processed_signatures_cache = dict(sorted_items[:250])
        
        last_cache_cleanup = current_time
        if old_sigs:
            logging.debug(f"[CACHE] Cleaned {len(old_sigs)} old signatures")
    
    try:
        if not rpc_url:
            rpc_url = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
            if HELIUS_API:
                rpc_url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API}"
        
        # CRITICAL FIX 4: Add timeout for HTTP client
        async with httpx.AsyncClient(timeout=5) as client:  # Reduced timeout from 10 to 5
            # Try jsonParsed first for better structure
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
                            
                            # Extract from transaction message
                            if "transaction" in result:
                                tx = result["transaction"]
                                if "message" in tx:
                                    msg = tx["message"]
                                    
                                    # Get static account keys
                                    if "accountKeys" in msg:
                                        for key in msg["accountKeys"]:
                                            if isinstance(key, str):
                                                account_keys.append(key)
                                            elif isinstance(key, dict):
                                                # Handle parsed format
                                                pubkey = key.get("pubkey") or key.get("address")
                                                if pubkey:
                                                    account_keys.append(pubkey)
                                    
                                    # Get instructions for additional accounts
                                    if "instructions" in msg:
                                        for inst in msg["instructions"]:
                                            if isinstance(inst, dict):
                                                # Check parsed instructions
                                                if "parsed" in inst and "info" in inst["parsed"]:
                                                    info = inst["parsed"]["info"]
                                                    for field in ["mint", "token", "account", "source", "destination"]:
                                                        if field in info:
                                                            val = info[field]
                                                            if isinstance(val, str) and len(val) == 44:
                                                                account_keys.append(val)
                                                
                                                # Check accounts array
                                                if "accounts" in inst:
                                                    for acc in inst["accounts"]:
                                                        if isinstance(acc, str) and len(acc) == 44:
                                                            account_keys.append(acc)
                            
                            # Get loaded addresses (from address lookup tables)
                            if "meta" in result:
                                meta = result["meta"]
                                
                                # Check loadedAddresses
                                if "loadedAddresses" in meta:
                                    loaded = meta["loadedAddresses"]
                                    if "writable" in loaded:
                                        account_keys.extend(loaded["writable"])
                                    if "readonly" in loaded:
                                        account_keys.extend(loaded["readonly"])
                                
                                # Check innerInstructions for nested accounts
                                if "innerInstructions" in meta:
                                    for inner in meta["innerInstructions"]:
                                        if "instructions" in inner:
                                            for inst in inner["instructions"]:
                                                if "parsed" in inst and "info" in inst["parsed"]:
                                                    info = inst["parsed"]["info"]
                                                    for field in ["mint", "token", "account", "authority", "destination"]:
                                                        if field in info:
                                                            val = info[field]
                                                            if isinstance(val, str) and len(val) == 44:
                                                                account_keys.append(val)
                                
                                # IMPORTANT: Check postTokenBalances for new mints
                                if "postTokenBalances" in meta:
                                    for balance in meta["postTokenBalances"]:
                                        if "mint" in balance:
                                            mint = balance["mint"]
                                            if mint not in account_keys:
                                                account_keys.append(mint)
                                
                                # Also check preTokenBalances (sometimes new tokens appear here)
                                if "preTokenBalances" in meta:
                                    for balance in meta["preTokenBalances"]:
                                        if "mint" in balance:
                                            mint = balance["mint"]
                                            # Check if this might be a new token
                                            if mint not in account_keys and mint not in SYSTEM_PROGRAMS:
                                                account_keys.append(mint)
                            
                            # Deduplicate while preserving order
                            seen = set()
                            unique_keys = []
                            for key in account_keys:
                                if key and key not in seen and len(key) == 44 and key not in SYSTEM_PROGRAMS:
                                    try:
                                        # Validate it's a real pubkey
                                        Pubkey.from_string(key)
                                        seen.add(key)
                                        unique_keys.append(key)
                                    except:
                                        pass
                            
                            if unique_keys:
                                logging.info(f"[TX FETCH] Got {len(unique_keys)} accounts for {signature[:8]}...")
                                return unique_keys
                            
                            # If no accounts found with this encoding, try the next one
                            continue
                            
                except asyncio.TimeoutError:
                    logging.warning(f"[TX FETCH] Timeout for {encoding} encoding")
                    continue
                except Exception as e:
                    logging.debug(f"[TX FETCH] {encoding} encoding failed: {e}")
                    continue
            
            # FIX #3: Pass retry_count to prevent infinite recursion
            logging.debug(f"[TX FETCH] All encodings failed, trying fallback for {signature[:8]}...")
            return await fetch_pumpfun_token_from_logs(signature, rpc_url, retry_count + 1)
        
    except asyncio.TimeoutError:
        logging.error(f"[TX FETCH] Overall timeout for {signature[:8]}...")
        return []
    except Exception as e:
        logging.error(f"[TX FETCH] Error fetching transaction {signature[:8]}...: {e}")
        # Try fallback method for PumpFun with retry count
        return await fetch_pumpfun_token_from_logs(signature, rpc_url, retry_count + 1)

async def fetch_pumpfun_token_from_logs(signature: str, rpc_url: str = None, retry_count: int = 0) -> list:
    """
    FIXED: Fallback method with loop prevention
    """
    global processed_signatures_cache
    # FIX #3: Prevent infinite loops
    if retry_count >= MAX_FETCH_RETRIES:
        logging.warning(f"[FALLBACK] Max retries exhausted for {signature[:8]}...")
        return []
    
    # Check if already processed
    if signature in processed_signatures_cache:
        return []
    
    try:
        if not rpc_url:
            rpc_url = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
            if HELIUS_API:
                rpc_url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API}"
        
        # Add timeout
        async with httpx.AsyncClient(timeout=5) as client:
            # Get transaction with base64 encoding for raw data
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
                    
                    # Try to decode base64 and find addresses
                    if "transaction" in result:
                        # Get the base64 transaction data
                        tx_data = result["transaction"]
                        if isinstance(tx_data, list) and len(tx_data) > 0:
                            try:
                                # Decode base64
                                raw_bytes = base64.b64decode(tx_data[0])
                                # Convert to string to search for patterns
                                raw_str = raw_bytes.hex()
                                
                                # Look for potential public keys in hex (32 bytes = 64 hex chars)
                                # Limit search to prevent excessive processing
                                max_checks = 100  # CRITICAL FIX: Limit iterations
                                checks = 0
                                for i in range(0, min(len(raw_str) - 64, max_checks * 2), 2):
                                    if checks >= max_checks:
                                        break
                                    checks += 1
                                    
                                    potential_hex = raw_str[i:i+64]
                                    try:
                                        # Convert hex to bytes then to base58
                                        key_bytes = bytes.fromhex(potential_hex)
                                        b58_key = b58encode(key_bytes).decode('utf-8')
                                        
                                        if len(b58_key) >= 43 and len(b58_key) <= 44:
                                            # Validate it's a real pubkey
                                            try:
                                                Pubkey.from_string(b58_key)
                                                if b58_key not in SYSTEM_PROGRAMS:
                                                    potential_mints.append(b58_key)
                                            except:
                                                pass
                                    except:
                                        pass
                            except Exception as e:
                                logging.debug(f"[FALLBACK] Base64 decode error: {e}")
                    
                    # Also check logs for addresses
                    if "meta" in result and "logMessages" in result["meta"]:
                        logs = result["meta"]["logMessages"]
                        
                        # Limit log processing
                        for log in logs[:50]:  # CRITICAL FIX: Process max 50 logs
                            # Look for mint/token mentions
                            if any(keyword in log.lower() for keyword in ["mint", "token", "create", "initialize"]):
                                # Extract base58 addresses from log
                                # Match Solana address pattern
                                matches = re.findall(r'[1-9A-HJ-NP-Za-km-z]{43,44}', log)
                                for match in matches[:10]:  # CRITICAL FIX: Limit matches per log
                                    if match not in SYSTEM_PROGRAMS and len(match) == 44:
                                        try:
                                            # Validate it's a valid pubkey
                                            Pubkey.from_string(match)
                                            if match not in potential_mints:
                                                potential_mints.append(match)
                                        except:
                                            pass
                    
                    # Deduplicate
                    unique_mints = list(dict.fromkeys(potential_mints))
                    
                    if unique_mints:
                        logging.info(f"[FALLBACK] Found {len(unique_mints)} potential mints from logs/raw data")
                        return unique_mints[:5]  # Return top 5 to avoid spam
        
        return []
        
    except asyncio.TimeoutError:
        logging.error(f"[FALLBACK] Timeout for {signature[:8]}...")
        return []
    except Exception as e:
        logging.debug(f"[FALLBACK] Error: {e}")
        return []

async def is_quality_token(mint: str, lp_amount: float) -> tuple:
    """
    Enhanced quality check for tokens - FIXED to be less strict
    Returns (is_quality, reason)
    """
    try:
        # Check if already bought
        if mint in already_bought:
            return False, "Already bought"
        
        # Check recent buy attempts (anti-spam)
        if mint in recent_buy_attempts:
            time_since_attempt = time.time() - recent_buy_attempts[mint]
            if time_since_attempt < DUPLICATE_CHECK_WINDOW:
                return False, f"Recent buy attempt {time_since_attempt:.0f}s ago"
        
        # FIXED: Use actual threshold from config
        if lp_amount < RUG_LP_THRESHOLD:
            return False, f"Low liquidity: {lp_amount:.2f} SOL (min: {RUG_LP_THRESHOLD})"
        
        # Try to get token metrics from DexScreener
        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
            async with httpx.AsyncClient(timeout=5, verify=False) as client:
                response = await client.get(url)
                if response.status_code == 200:
                    data = response.json()
                    if "pairs" in data and len(data["pairs"]) > 0:
                        pair = data["pairs"][0]  # Get best pair
                        
                        # FIXED: Lower volume requirements
                        volume_h24 = float(pair.get("volume", {}).get("h24", 0))
                        min_volume = float(os.getenv("MIN_VOLUME_USD", 300))  # FIXED: Much lower
                        if volume_h24 < min_volume:
                            # Don't reject, just note it
                            logging.info(f"Low volume ${volume_h24:.0f} but proceeding")
                        
                        # Check buy/sell ratio - more lenient
                        txns = pair.get("txns", {})
                        buys_h1 = txns.get("h1", {}).get("buys", 1)  # Default to 1 to avoid division
                        sells_h1 = txns.get("h1", {}).get("sells", 1)
                        
                        if sells_h1 > 0 and buys_h1 / sells_h1 < 0.5:  # FIXED: Much more lenient
                            return False, f"Bad buy/sell ratio: {buys_h1}/{sells_h1}"
                        
                        # Check price change (avoid massive dumps only)
                        price_change_h1 = float(pair.get("priceChange", {}).get("h1", 0))
                        if price_change_h1 < -50:  # FIXED: Only avoid major dumps
                            return False, f"Dumping hard: {price_change_h1:.1f}% in 1h"
                        
                        # Passed all checks
                        return True, "Quality token"
        except:
            pass
        
        # If we can't get DexScreener data but LP is good, allow it
        if lp_amount >= RUG_LP_THRESHOLD:
            return True, f"Good liquidity ({lp_amount:.1f} SOL), proceeding without data"
        
        return False, "Failed quality checks"
        
    except Exception as e:
        logging.error(f"Quality check error: {e}")
        # Be lenient on errors
        if lp_amount >= RUG_LP_THRESHOLD:
            return True, "Quality check error but good LP"
        return False, "Quality check error"

async def verify_pool_exists(mint: str) -> bool:
    """
    Verify that a real trading pool exists for this token
    """
    try:
        # Check cache first
        if mint in pool_verification_cache:
            return pool_verification_cache[mint]
        
        # Check if we have a detected pool ID
        if mint in detected_pools:
            pool_verification_cache[mint] = True
            return True
        
        # Check Raydium
        pool = raydium.find_pool_realtime(mint)
        if pool:
            pool_verification_cache[mint] = True
            return True
        
        # Check Jupiter
        try:
            url = f"https://price.jup.ag/v4/price?ids={mint}"
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    if mint in data.get("data", {}):
                        pool_verification_cache[mint] = True
                        return True
        except:
            pass
        
        pool_verification_cache[mint] = False
        return False
        
    except Exception as e:
        logging.error(f"Pool verification error: {e}")
        return False

async def check_pumpfun_graduation(mint: str) -> bool:
    """Check if a PumpFun token is ready to graduate"""
    try:
        url = f"https://frontend-api.pump.fun/coins/{mint}"
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(url)
            if response.status_code == 200:
                data = response.json()
                market_cap = data.get("usd_market_cap", 0)
                
                if market_cap > PUMPFUN_GRADUATION_MC * 0.9:
                    logging.info(f"[PumpFun] {mint[:8]}... approaching graduation: ${market_cap:.0f}")
                    return True
    except Exception as e:
        logging.debug(f"[PumpFun] Graduation check error: {e}")
    
    return False

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
                        
                        if lp_amount >= RUG_LP_THRESHOLD:
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
                
                for coin in coins[:20]:
                    mint = coin.get("mint")
                    market_cap = coin.get("usd_market_cap", 0)
                    
                    if not mint:
                        continue
                    
                    if market_cap > PUMPFUN_GRADUATION_MC * 0.8:
                        if mint not in pumpfun_tokens:
                            pumpfun_tokens[mint] = {
                                "discovered": time.time(),
                                "migrated": False,
                                "market_cap": market_cap
                            }
                        
                        if mint not in migration_watch_list:
                            migration_watch_list.add(mint)
                            logging.info(f"[PumpFun] Added {mint[:8]}... to migration watch (MC: ${market_cap:.0f})")
                            
                            if market_cap > PUMPFUN_GRADUATION_MC * 0.95:
                                await send_telegram_alert(
                                    f"⚠️ GRADUATION IMMINENT\n\n"
                                    f"Token: `{mint}`\n"
                                    f"Market Cap: ${market_cap:,.0f}\n"
                                    f"Graduation at: $69,420\n"
                                    f"Status: {(market_cap/PUMPFUN_GRADUATION_MC)*100:.1f}% complete\n\n"
                                    f"Monitoring for Raydium migration..."
                                )
    except Exception as e:
        logging.error(f"[PumpFun Scan] Error: {e}")
        async def mempool_listener(name, program_id=None):
    """Enhanced mempool listener with FIXED detection logic and pool validation"""
    if not HELIUS_API:
        logging.warning(f"[{name}] HELIUS_API not set, skipping mempool listener")
        await send_telegram_alert(f"⚠️ {name} listener disabled (no Helius API key)")
        return
    
    # Skip Jupiter mempool if configured
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
            current_time = time.time()
            last_alert = last_alert_sent.get(name, 0)
            if current_time - last_alert > 1800:  # 30 minutes
                await send_telegram_alert(f"📡 {name} listener ACTIVE")
                last_alert_sent[name] = current_time
            else:
                logging.info(f"[{name}] Reconnected successfully (alert suppressed)")
            
            listener_status[name] = "ACTIVE"
            last_seen_token[name] = time.time()
            retry_attempts = 0
            
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
                        
                        if len(processed_txs) > 1000:
                            processed_txs.clear()
                        
                        transaction_counter += 1
                        
                        if transaction_counter % 100 == 0:
                            logging.info(f"[{name}] Processed {transaction_counter} txs, found {pool_creations_found} pool creations")
                        
                        # ================== FIXED DETECTION LOGIC ==================
                        is_pool_creation = False
                        is_token_creation = False
                        pool_id = None  # Track the pool ID
                        
                        if name == "Raydium":
                            # FIXED: More comprehensive Raydium pool creation detection
                            raydium_indicators = 0
                            has_init_pool = False
                            has_create_pool = False
                            has_liquidity = False
                            
                            for log in logs:
                                log_lower = log.lower()
                                
                                # Look for specific Raydium pool initialization
                                if "initialize" in log_lower:
                                    raydium_indicators += 1
                                    if "pool" in log_lower or "amm" in log_lower:
                                        has_init_pool = True
                                        raydium_indicators += 2
                                
                                # FIXED: Add more detection patterns
                                if "program log: instruction: initialize" in log_lower:
                                    has_init_pool = True
                                    raydium_indicators += 3
                                
                                # Direct Raydium invocation
                                if "invoke [3]" in log and "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8" in log:
                                    raydium_indicators += 3
                                
                                if "create" in log_lower and ("pool" in log_lower or "amm" in log_lower):
                                    has_create_pool = True
                                    raydium_indicators += 3
                                
                                if "add_liquidity" in log_lower or "deposit" in log_lower:
                                    has_liquidity = True
                                    raydium_indicators += 2
                                
                                # Raydium-specific instruction names
                                if any(x in log_lower for x in ["init_pc_amount", "init_coin_amount", "opentime", "nonce"]):
                                    raydium_indicators += 2
                                
                                # Initialize2 is common for Raydium V4
                                if "instruction: initialize2" in log_lower:
                                    raydium_indicators += 3
                                    has_init_pool = True
                            
                            # Account count is also an indicator
                            if len(account_keys) > 10:
                                raydium_indicators += 1
                            
                            # DEBUG: Log detection scores
                            if raydium_indicators > 0:
                                logging.info(f"[{name}] Detection Debug:")
                                logging.info(f"  Indicators: {raydium_indicators} (need {RAYDIUM_MIN_INDICATORS})")
                                logging.info(f"  Logs: {len(logs)} (need {RAYDIUM_MIN_LOGS})")
                                logging.info(f"  Has init: {has_init_pool}, Has create: {has_create_pool}, Has liquidity: {has_liquidity}")
                            
                            # Use the ENV variable thresholds
                            if raydium_indicators >= RAYDIUM_MIN_INDICATORS and len(logs) >= RAYDIUM_MIN_LOGS:
                                is_pool_creation = True
                                logging.info(f"[RAYDIUM] POOL CREATION DETECTED - Score: {raydium_indicators}, Logs: {len(logs)}")
                        
                        elif name == "PumpFun":
                            # FIXED: Better PumpFun token creation detection
                            pumpfun_create_indicators = 0
                            has_mint_creation = False
                            has_bonding = False
                            
                            for log in logs:
                                log_lower = log.lower()
                                
                                # CRITICAL: Look for actual token CREATION, not trades
                                if "program log: instruction: create" in log_lower:
                                    is_token_creation = True
                                    pumpfun_create_indicators += 5
                                
                                # PumpFun specific creation patterns
                                if "initialize" in log_lower and ("mint" in log_lower or "token" in log_lower):
                                    pumpfun_create_indicators += 3
                                    has_mint_creation = True
                                
                                # Bonding curve initialization is key indicator for NEW tokens
                                if "bonding" in log_lower and ("init" in log_lower or "create" in log_lower):
                                    pumpfun_create_indicators += 4
                                    has_bonding = True
                                    is_token_creation = True
                                
                                # Look for "launch" which indicates new token
                                if "launch" in log_lower or "deploy" in log_lower:
                                    pumpfun_create_indicators += 3
                                    is_token_creation = True
                            
                            # DEBUG
                            if pumpfun_create_indicators > 0:
                                logging.info(f"[{name}] PumpFun Debug:")
                                logging.info(f"  Indicators: {pumpfun_create_indicators} (need {PUMPFUN_MIN_INDICATORS})")
                                logging.info(f"  Logs: {len(logs)} (need {PUMPFUN_MIN_LOGS})")
                                logging.info(f"  Is Creation: {is_token_creation}")
                            
                            # CRITICAL: Only process if it's actually a token CREATION
                            if not is_token_creation:
                                logging.debug(f"[{name}] Not a token creation, skipping")
                                continue
                            
                            # Use ENV variable thresholds
                            if pumpfun_create_indicators >= PUMPFUN_MIN_INDICATORS and len(logs) >= PUMPFUN_MIN_LOGS:
                                is_pool_creation = True
                                logging.info(f"[PUMPFUN] NEW TOKEN CREATION DETECTED - Score: {pumpfun_create_indicators}")
                        
                        elif name == "Moonshot":
                            # Moonshot token launches
                            for log in logs:
                                log_lower = log.lower()
                                if ("moon" in log_lower or "launch" in log_lower) and ("create" in log_lower or "initialize" in log_lower):
                                    if len(logs) >= 5:  # Real launches have multiple logs
                                        is_pool_creation = True
                                        break
                        
                        elif name == "Jupiter":
                            # Skip Jupiter entirely - too noisy and unreliable
                            continue
                        
                        # ================== END FIXED DETECTION LOGIC ==================
                        
                        if not is_pool_creation:
                            continue
                        
                        pool_creations_found += 1
                        logging.info(f"[{name}] POOL/TOKEN CREATION DETECTED! Total found: {pool_creations_found}")
                        
                        # Fetch full transaction if needed - WITH TIMEOUT
                        if len(account_keys) == 0:
                            logging.info(f"[{name}] Fetching full transaction...")
                            try:
                                # CRITICAL FIX: Add timeout to transaction fetching
                                fetch_task = asyncio.create_task(fetch_transaction_accounts(signature))
                                account_keys = await asyncio.wait_for(fetch_task, timeout=5)
                            except asyncio.TimeoutError:
                                logging.warning(f"[{name}] Transaction fetch timeout for {signature[:8]}...")
                                continue
                            
                            if len(account_keys) == 0:
                                logging.warning(f"[{name}] Could not fetch account keys")
                                continue
                        
                        # For Raydium, try to identify the pool account
                        if name == "Raydium" and account_keys:
                            # The pool account is usually one of the writable accounts
                            # Look for accounts that aren't system programs or token mints
                            for key in account_keys:
                                if isinstance(key, dict):
                                    key = key.get("pubkey", "") or key.get("address", "")
                                
                                if key and len(key) == 44 and key not in SYSTEM_PROGRAMS:
                                    # This might be the pool account
                                    # We'll register it when we find the token mint
                                    pool_id = key
                                    break
                        
                        # Process potential mints with QUALITY CHECKS
                        for key in account_keys:
                            if isinstance(key, dict):
                                key = key.get("pubkey", "") or key.get("address", "")
                            
                            if key in SYSTEM_PROGRAMS or len(key) != 44:
                                continue
                            
                            if key == "So11111111111111111111111111111111111111112":
                                continue
                            
                            # Check if already processed
                            if key in seen_tokens or key in already_bought:
                                continue
                            
                            try:
                                Pubkey.from_string(key)
                                potential_mint = key
                            except:
                                continue
                            
                            # Mark as seen
                            seen_tokens.add(potential_mint)
                            
                            # Track if PumpFun
                            if name == "PumpFun" and potential_mint not in pumpfun_tokens:
                                pumpfun_tokens[potential_mint] = {
                                    "discovered": time.time(),
                                    "migrated": False
                                }
                                logging.info(f"[PumpFun] Tracking new token: {potential_mint[:8]}...")
                            
                            # Register Raydium pool if detected
                            if name == "Raydium" and pool_id:
                                detected_pools[potential_mint] = pool_id
                                raydium.register_new_pool(pool_id, potential_mint)
                                logging.info(f"[Raydium] Registered pool {pool_id[:8]}... for token {potential_mint[:8]}...")
                            
                            # ========== FIX 1: ADD TOKEN AGE VERIFICATION ==========
                            if name == "PumpFun" and is_bot_running():
                                if potential_mint not in BROKEN_TOKENS and potential_mint not in BLACKLIST:
                                    # Check if token is actually new
                                    try:
                                        # Verify token age
                                        from solana.rpc.api import Client
                                        temp_client = Client(RPC_URL)
                                        mint_account = temp_client.get_account_info(Pubkey.from_string(potential_mint))
                                        
                                        if mint_account and mint_account.value:
                                            # Check if token is old
                                            current_slot = temp_client.get_slot().value
                                            # Get first signature for this account to estimate age
                                            sigs = temp_client.get_signatures_for_address(
                                                Pubkey.from_string(potential_mint),
                                                limit=1
                                            )
                                            
                                            if sigs and sigs.value:
                                                first_sig = sigs.value[-1]  # Oldest signature
                                                if hasattr(first_sig, 'slot'):
                                                    token_age_slots = current_slot - first_sig.slot
                                                    # If older than ~10 minutes (1500 slots), skip
                                                    if token_age_slots > 1500:
                                                        logging.info(f"[SKIP] {potential_mint[:8]}... is {token_age_slots} slots old - NOT A NEW TOKEN")
                                                        record_skip("old_token")
                                                        continue
                                    except Exception as e:
                                        logging.debug(f"Age check error: {e}, proceeding anyway")
                                    
                                    # Skip if already bought
                                    if potential_mint in already_bought:
                                        continue
                                    
                                    logging.info(f"[PUMPFUN] Evaluating token: {potential_mint[:8]}...")
                                    
                                    # Shorter delay for faster execution
                                    await asyncio.sleep(PUMPFUN_INIT_DELAY)
                                    
                                    # Check if graduated or about to graduate
                                    graduated = await check_pumpfun_graduation(potential_mint)
                                    if graduated and potential_mint in pumpfun_tokens:
                                        pumpfun_tokens[potential_mint]["migrated"] = True
                                    
                                    # Get liquidity (may be 0 for bonding curve tokens)
                                    lp_data = await get_liquidity_and_ownership(potential_mint)
                                    lp_amount = lp_data.get("liquidity", 0) if lp_data else 0
                                    
                                    # FIXED: Be more lenient with PumpFun liquidity
                                    if lp_amount == 0:
                                        # For brand new PumpFun tokens, this might be normal
                                        logging.info(f"[PUMPFUN] New token {potential_mint[:8]}... - No LP yet, checking if tradeable")
                                        # Try a small test buy anyway for very new tokens
                                        lp_amount = 0.1  # Pretend there's minimal liquidity
                                    
                                    # For PumpFun tokens, require minimum liquidity
                                    min_lp_for_pumpfun = MIN_LP_FOR_PUMPFUN if not graduated else RUG_LP_THRESHOLD
                                    
                                    # Skip if liquidity too low (but be lenient)
                                    if lp_amount < min_lp_for_pumpfun and lp_amount > 0:
                                        logging.info(f"[PUMPFUN] Low LP: {lp_amount:.2f} SOL but proceeding cautiously")
                                    
                                    # Mark as attempted
                                    recent_buy_attempts[potential_mint] = time.time()
                                    
                                    # Determine buy amount based on graduation status
                                    if graduated:
                                        buy_amount = PUMPFUN_MIGRATION_BUY  # 0.1 SOL for graduates
                                        buy_reason = "PumpFun Graduate"
                                    else:
                                        # For early PumpFun tokens, use small amount
                                        buy_amount = PUMPFUN_EARLY_BUY  # 0.02 SOL for bonding curve
                                        buy_reason = "PumpFun Early Entry"
                                    
                                    # Alert before buying
                                    await send_telegram_alert(
                                        f"🎯 PUMPFUN TOKEN DETECTED\n\n"
                                        f"Token: `{potential_mint}`\n"
                                        f"Status: {buy_reason}\n"
                                        f"Liquidity: {lp_amount:.2f} SOL\n"
                                        f"Buy Amount: {buy_amount} SOL\n\n"
                                        f"Attempting snipe..."
                                    )
                                    
                                    # Store original amount and set PumpFun amount
                                    original_amount = os.getenv("BUY_AMOUNT_SOL")
                                    os.environ["BUY_AMOUNT_SOL"] = str(buy_amount)
                                    
                                    try:
                                        # Execute buy
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
                                            break  # Don't buy more from this transaction
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
                            
                            # Only buy from Raydium with enhanced validation
                            elif name in ["Raydium"] and is_bot_running():
                                if potential_mint not in BROKEN_TOKENS and potential_mint not in BLACKLIST:
                                    
                                    # Add delay to let pool settle
                                    await asyncio.sleep(MEMPOOL_DELAY_MS / 1000)
                                    
                                    # Get liquidity with timeout
                                    lp_amount = 0
                                    try:
                                        lp_check_task = asyncio.create_task(get_liquidity_and_ownership(potential_mint))
                                        lp_data = await asyncio.wait_for(lp_check_task, timeout=2.0)
                                        
                                        if lp_data:
                                            lp_amount = lp_data.get("liquidity", 0)
                                    except asyncio.TimeoutError:
                                        logging.info(f"[{name}] LP check timeout")
                                        continue
                                    except Exception as e:
                                        logging.debug(f"[{name}] LP check error: {e}")
                                        continue
                                    
                                    # FIXED: Be more lenient with liquidity
                                    if lp_amount < 0.5:  # Very minimal threshold
                                        logging.info(f"[{name}] Very low liquidity ({lp_amount:.2f} SOL) but checking quality")
                                    
                                    # Quality check
                                    is_quality, reason = await is_quality_token(potential_mint, lp_amount)
                                    
                                    if not is_quality:
                                        logging.info(f"[{name}] Skipping {potential_mint[:8]}... - {reason}")
                                        record_skip("quality_check")
                                        continue
                                    
                                    # Determine risk level and buy amount
                                    if lp_amount >= RUG_LP_THRESHOLD * 2:
                                        risk_level = "SAFE"
                                        buy_amount = SAFE_BUY_AMOUNT
                                    elif lp_amount >= RUG_LP_THRESHOLD:
                                        risk_level = "MEDIUM"
                                        buy_amount = RISKY_BUY_AMOUNT
                                    else:
                                        risk_level = "HIGH"
                                        buy_amount = ULTRA_RISKY_BUY_AMOUNT
                                    
                                    # Mark as attempted
                                    recent_buy_attempts[potential_mint] = time.time()
                                    
                                    await send_telegram_alert(
                                        f"✅ QUALITY TOKEN DETECTED ✅\n\n"
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
                                                f"✅ SNIPED QUALITY TOKEN!\n"
                                                f"Token: {potential_mint[:16]}...\n"
                                                f"Amount: {buy_amount} SOL\n"
                                                f"Risk: {risk_level}\n"
                                                f"Monitoring for profits..."
                                            )
                                            asyncio.create_task(wait_and_auto_sell(potential_mint))
                                            break  # Don't buy more from this transaction
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
                
                except asyncio.TimeoutError:
                    continue
                except websockets.exceptions.ConnectionClosed as e:
                    logging.warning(f"[{name}] WebSocket closed: {e}")
                    break
                    
        except Exception as e:
            logging.error(f"[{name} ERROR] {str(e)}")
            listener_status[name] = f"RETRYING ({retry_attempts + 1})"
            
        finally:
            if watchdog_task and not watchdog_task.done():
                watchdog_task.cancel()
                try:
                    await watchdog_task
                except asyncio.CancelledError:
                    pass
            
            if ws:
                await ws.close()
            
            retry_attempts += 1
            
            if retry_attempts >= max_retries:
                msg = f"⚠️ {name} listener failed after {max_retries} attempts"
                await send_telegram_alert(msg)
                listener_status[name] = "FAILED"
                break
            
            wait_time = min(retry_delay * (2 ** (retry_attempts - 1)), 300)
            logging.info(f"[{name}] Retrying in {wait_time}s (attempt {retry_attempts}/{max_retries})")
            await asyncio.sleep(wait_time)

MIN_LP_USD = float(os.getenv("MIN_LP_USD", 1500))
MIN_VOLUME_USD = float(os.getenv("MIN_VOLUME_USD", 300))
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
            logging.error(f"DexScreener attempt {attempt + 1} failed: {e}")
            await asyncio.sleep(2)
    
    return None

async def get_trending_pairs_birdeye():
    """Fetch trending pairs from Birdeye"""
    if not BIRDEYE_API_KEY:
        return None
        
    url = "https://public-api.birdeye.so/defi/tokenlist"
    
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=30, verify=False) as client:
                headers = {
                    "X-API-KEY": BIRDEYE_API_KEY,
                    "accept": "application/json"
                }
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    pairs = []
                    for tok in data.get("data", {}).get("tokens", [])[:20]:
                        mint = tok.get("address")
                        if not mint:
                            continue
                        lp_usd = float(tok.get("liquidity", 0))
                        vol_usd = float(tok.get("v24hUSD", 0))
                        pair = {
                            "baseToken": {"address": mint},
                            "liquidity": {"usd": lp_usd},
                            "volume": {"h24": vol_usd},
                        }
                        pairs.append(pair)
                    if pairs:
                        logging.info(f"[Trending] Birdeye returned {len(pairs)} tokens")
                    return pairs
                else:
                    logging.debug(f"Birdeye returned status {resp.status_code}")
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
        min_lp = float(os.getenv("RUG_LP_THRESHOLD", 3.0))
        
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
# MOMENTUM SCANNER - ELITE TRADING STRATEGY
# ============================================

def detect_chart_pattern(price_data: list) -> str:
    """
    Detect if chart shows good or bad patterns
    Returns: 'steady_climb', 'pump_dump', 'vertical', 'consolidating', 'unknown'
    """
    if not price_data or len(price_data) < 5:
        return "unknown"
    
    # Calculate changes between candles
    changes = []
    for i in range(1, len(price_data)):
        change = ((price_data[i] - price_data[i-1]) / price_data[i-1]) * 100
        changes.append(change)
    
    # Detect patterns
    max_change = max(changes) if changes else 0
    avg_change = sum(changes) / len(changes) if changes else 0
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

async def score_momentum_token(token_data: dict) -> tuple:
    """
    Score a token based on your exact momentum criteria
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
        
        # ===== MOMENTUM RULES (YOUR CRITERIA) =====
        
        # 1. Hour gain in sweet spot (50-200%)
        if MOMENTUM_MIN_1H_GAIN <= price_change_1h <= MOMENTUM_MAX_1H_GAIN:
            score += 1
            signals.append(f"✅ 1h gain: {price_change_1h:.1f}%")
        elif price_change_1h > MOMENTUM_MAX_1H_GAIN:
            signals.append(f"❌ Too late: {price_change_1h:.1f}% gain")
            return (0, signals)  # Automatic disqualification
        
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
        
        # 4. Safe liquidity
        if liquidity_usd >= MOMENTUM_MIN_LIQUIDITY:
            score += 1
            signals.append(f"✅ Liquidity: ${liquidity_usd:,.0f}")
        else:
            signals.append(f"❌ Low liquidity: ${liquidity_usd:,.0f}")
            return (0, signals)  # Automatic disqualification
        
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
            signals.append(f"❌ Bad pattern: {pattern}")
            score -= 1
        
        # 8. Check if NOT at ATH (bonus)
        # Simple check: if 5m is negative but 1h is positive, might be pulling back
        if price_change_5m < 0 and price_change_1h > 50:
            score += 0.25
            signals.append("✅ Pulling back from high")
        
    except Exception as e:
        logging.error(f"Error scoring momentum token: {e}")
        return (0, [f"Error: {str(e)}"])
    
    return (int(score), signals)

async def fetch_top_gainers() -> list:
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
                        
                        # Pre-filter
                        if (MOMENTUM_MIN_1H_GAIN <= price_change_1h <= MOMENTUM_MAX_1H_GAIN * 1.5 and
                            liquidity_usd >= MOMENTUM_MIN_LIQUIDITY * 0.8):
                            filtered_pairs.append(pair)
                
                # Sort by 1h gain
                filtered_pairs.sort(key=lambda x: float(x.get("priceChange", {}).get("h1", 0)), reverse=True)
                
                # Return top candidates
                return filtered_pairs[:MAX_MOMENTUM_TOKENS]
                
    except Exception as e:
        logging.error(f"Error fetching gainers: {e}")
    
    return []

async def momentum_scanner():
    """
    Elite Momentum Scanner - Finds pumping tokens with your exact criteria
    Implements the hybrid strategy for 70% win rate momentum plays
    """
    if not MOMENTUM_SCANNER_ENABLED:
        logging.info("[Momentum Scanner] Disabled via configuration")
        return
    
    await send_telegram_alert(
        "🔥 MOMENTUM SCANNER ACTIVE 🔥\n\n"
        f"Mode: {'HYBRID AUTO-BUY' if MOMENTUM_AUTO_BUY else 'ALERT ONLY'}\n"
        f"Auto-buy threshold: {MIN_SCORE_AUTO_BUY}/5\n"
        f"Alert threshold: {MIN_SCORE_ALERT}/5\n"
        f"Target: 50-200% gainers\n"
        f"Position sizes: 0.02-0.1 SOL\n\n"
        f"Hunting for pumps..."
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
                await asyncio.sleep(MOMENTUM_SCAN_INTERVAL)
                continue  # Skip dead hours
            
            # Fetch top gainers
            top_gainers = await fetch_top_gainers()
            
            if not top_gainers:
                consecutive_errors += 1
                if consecutive_errors > 5:
                    logging.warning("[Momentum Scanner] Multiple fetch failures")
                    await asyncio.sleep(MOMENTUM_SCAN_INTERVAL * 2)
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
                    if token_address in momentum_analyzed:
                        last_check = momentum_analyzed[token_address].get("timestamp", 0)
                        if time.time() - last_check < 300:  # 5 minutes
                            continue
                    
                    # Skip if already bought
                    if token_address in momentum_bought or token_address in already_bought:
                        continue
                    
                    # Score the token
                    score, signals = await score_momentum_token(token_data)
                    
                    # Store analysis
                    momentum_analyzed[token_address] = {
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
                        # AUTO BUY - Perfect setup
                        position_size = MOMENTUM_POSITION_5_SCORE if score >= 5 else MOMENTUM_POSITION_4_SCORE
                        
                        # Extra caution during off-hours
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
                        
                        # Execute buy
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
                            # Start auto-sell
                            asyncio.create_task(wait_and_auto_sell(token_address))
                        
                    elif score >= MIN_SCORE_ALERT:
                        # ALERT ONLY - Good setup needs approval
                        await send_telegram_alert(
                            f"🔔 MOMENTUM OPPORTUNITY 🔔\n\n"
                            f"Token: {token_symbol} ({token_address[:8]}...)\n"
                            f"Score: {score}/5 ⭐\n"
                            f"Suggested: {MOMENTUM_POSITION_3_SCORE} SOL\n\n"
                            f"Signals:\n" + "\n".join(signals[:5]) + "\n\n"
                            f"Use /forcebuy {token_address} to execute"
                        )
                    
                    # Rate limit between checks
                    await asyncio.sleep(1)
                    
                except Exception as e:
                    logging.error(f"Error analyzing momentum token: {e}")
                    continue
            
            # Summary log
            if candidates_found > 0:
                logging.info(f"[Momentum Scanner] Found {candidates_found} candidates this scan")
            
            # Wait before next scan
            await asyncio.sleep(MOMENTUM_SCAN_INTERVAL)
            
        except Exception as e:
            logging.error(f"[Momentum Scanner] Error in main loop: {e}")
            await asyncio.sleep(MOMENTUM_SCAN_INTERVAL)

async def check_momentum_score(mint: str) -> dict:
    """
    Check momentum score for a specific token (used by forcebuy)
    """
    try:
        # Fetch token data from DexScreener
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        
        async with httpx.AsyncClient(timeout=10, verify=False) as client:
            response = await client.get(url)
            
            if response.status_code == 200:
                data = response.json()
                pairs = data.get("pairs", [])
                
                if pairs:
                    # Get best pair
                    best_pair = pairs[0]
                    score, signals = await score_momentum_token(best_pair)
                    
                    # Get position recommendation
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

async def start_sniper():
    """Start the ELITE sniper bot with MOMENTUM SCANNER"""
    mode_text = "ELITE Money Printer Mode + Momentum Scanner"
    TASKS.append(asyncio.create_task(start_dexscreener_monitor()))
    
    await send_telegram_alert(
        f"💰 MONEY PRINTER LAUNCHING 💰\n\n"
        f"Mode: {mode_text}\n"
        f"Min LP: {RUG_LP_THRESHOLD} SOL\n"
        f"Min AI Score: {MIN_AI_SCORE}\n"
        f"Min Volume: ${MIN_VOLUME_USD:,.0f}\n"
        f"Migration Snipe: {PUMPFUN_MIGRATION_BUY} SOL\n"
        f"Momentum Mode: {'HYBRID' if MOMENTUM_AUTO_BUY else 'ALERTS'}\n\n"
        f"Quality filters: ACTIVE ✅\n"
        f"Duplicate prevention: ACTIVE ✅\n"
        f"Pool verification: ACTIVE ✅\n"
        f"MOMENTUM SCANNER: ACTIVE 🔥\n\n"
        f"Ready to print money! 🎯"
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
    
    # Start listeners (skip Jupiter if configured)
    listeners = ["Raydium", "PumpFun", "Moonshot"]
    if not SKIP_JUPITER_MEMPOOL:
        listeners.append("Jupiter")
    
    for listener in listeners:
        TASKS.append(asyncio.create_task(mempool_listener(listener)))
    
    TASKS.append(asyncio.create_task(trending_scanner()))
    
    # ADD MOMENTUM SCANNER - YOUR ELITE STRATEGY
    if MOMENTUM_SCANNER_ENABLED:
        TASKS.append(asyncio.create_task(momentum_scanner()))
        await send_telegram_alert(
            "🔥 Momentum Scanner: ACTIVE\n"
            "Hunting for 50-200% gainers\n"
            "Auto-buy score: 3/5\n"
            "Alert score: 2+/5"
        )
    
    if ENABLE_PUMPFUN_MIGRATION:
        TASKS.append(asyncio.create_task(pumpfun_migration_monitor()))
        TASKS.append(asyncio.create_task(raydium_graduation_scanner()))
        await send_telegram_alert("🎯 PumpFun Migration Monitor: ACTIVE")
        await send_telegram_alert("🎓 Graduation Scanner: ACTIVE")
    
    await send_telegram_alert(f"🎯 MONEY PRINTER ACTIVE - {mode_text}!")

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

        # Initialize is_pumpfun first
        is_pumpfun = mint in pumpfun_tokens
        
        # CHECK MOMENTUM SCORE FOR FORCE BUYS
        momentum_data = await check_momentum_score(mint)
        if momentum_data["score"] > 0:
            await send_telegram_alert(
                f"📊 MOMENTUM SCORE CHECK\n\n"
                f"Token: {mint[:8]}...\n"
                f"Score: {momentum_data['score']}/5 ⭐\n"
                f"Signals:\n" + "\n".join(momentum_data['signals'][:5]) + "\n\n"
                f"Recommended position: {momentum_data['recommendation']} SOL"
            )
            
            # Use momentum recommendation if score is good
            if momentum_data['score'] >= 3:
                buy_amount = momentum_data['recommendation']
            else:
                buy_amount = PUMPFUN_MIGRATION_BUY if is_pumpfun else BUY_AMOUNT_SOL
        else:
            # Use default amounts
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

async def stop_all_tasks():
    """Stop all running tasks"""
    for task in TASKS:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    TASKS.clear()
    await send_telegram_alert("🛑 All sniper tasks stopped.")

# ============================================
# MAIN EXECUTION
# ============================================

if __name__ == "__main__":
    print("Sniper logic module loaded. This should be imported, not run directly.")
    print("Use main.py to start the bot.")
    
    # Log configuration summary
    logging.info("=" * 60)
    logging.info("SNIPER CONFIGURATION LOADED")
    logging.info("=" * 60)
    logging.info(f"RPC URL: {RPC_URL[:30]}...")
    logging.info(f"Rug LP Threshold: {RUG_LP_THRESHOLD} SOL")
    logging.info(f"Safe Buy Amount: {SAFE_BUY_AMOUNT} SOL")
    logging.info(f"Risky Buy Amount: {RISKY_BUY_AMOUNT} SOL")
    logging.info(f"Ultra Risky Buy Amount: {ULTRA_RISKY_BUY_AMOUNT} SOL")
    logging.info(f"PumpFun Early Buy: {PUMPFUN_EARLY_BUY} SOL")
    logging.info(f"PumpFun Migration Buy: {PUMPFUN_MIGRATION_BUY} SOL")
    logging.info(f"Momentum Scanner: {'ENABLED' if MOMENTUM_SCANNER_ENABLED else 'DISABLED'}")
    logging.info(f"Momentum Auto-Buy: {'ENABLED' if MOMENTUM_AUTO_BUY else 'DISABLED'}")
    logging.info(f"Jupiter Mempool: {'DISABLED' if SKIP_JUPITER_MEMPOOL else 'ENABLED'}")
    logging.info("=" * 60)

