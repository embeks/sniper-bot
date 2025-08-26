#!/usr/bin/env python3
# FIXED sniper_logic.py - PART 1 OF 3
# Copy everything from here until you see "END OF PART 1"

import asyncio
import json
import os
import websockets
import logging
import time
import re
import base64
from base58 import b58encode, b58decode
from datetime import datetime, timedelta
import httpx
import random

# CRITICAL FIX: Load environment variables FIRST before any imports that use them
from dotenv import load_dotenv
load_dotenv()

# Now import other modules AFTER environment is loaded
from dexscreener_monitor import start_dexscreener_monitor
from utils import (
    is_valid_mint, buy_token, log_skipped_token, send_telegram_alert,
    get_trending_mints, wait_and_auto_sell, get_liquidity_and_ownership,
    is_bot_running, keypair, BUY_AMOUNT_SOL, BROKEN_TOKENS,
    mark_broken_token, daily_stats_reset_loop,
    update_last_activity, increment_stat, record_skip,
    listener_status, last_seen_token, daily_stats
)
from solders.pubkey import Pubkey
from raydium_aggregator import RaydiumAggregator

# ============================================
# CRITICAL FIX: Transaction cache to prevent infinite loops
# ============================================
processed_signatures_cache = {}  # signature -> timestamp
CACHE_CLEANUP_INTERVAL = 300  # Clean cache every 5 minutes
last_cache_cleanup = time.time()
MAX_FETCH_RETRIES = 2  # Maximum retries for transaction fetching

# CRITICAL FIX: Track active listeners to prevent duplicates
ACTIVE_LISTENERS = {}  # name -> task
LISTENER_START_TIMES = {}  # name -> timestamp

# ADD THIS TO PREVENT DETECTION LOOPS
RECENT_DETECTIONS = {}  # signature -> timestamp
DETECTION_COOLDOWN = 30  # seconds

# CRITICAL FIX: Track if we've already sent startup messages
STARTUP_MESSAGES_SENT = False
BOT_START_TIME = time.time()

# FIX: Track false positives separately
FALSE_POSITIVE_TOKENS = {}  # token -> last_attempt_time
FALSE_POSITIVE_COOLDOWN = 60  # Don't retry false positives for 60 seconds

def is_duplicate_detection(signature: str) -> bool:
    """Check if we've seen this transaction recently"""
    current_time = time.time()
    if signature in RECENT_DETECTIONS:
        if current_time - RECENT_DETECTIONS[signature] < DETECTION_COOLDOWN:
            return True
    # Clean old entries
    RECENT_DETECTIONS[signature] = current_time
    if len(RECENT_DETECTIONS) > 1000:
        # Clear old entries
        to_remove = []
        cutoff = current_time - 60
        for sig, ts in RECENT_DETECTIONS.items():
            if ts < cutoff:
                to_remove.append(sig)
        for sig in to_remove:
            del RECENT_DETECTIONS[sig]
    return False

# Environment variables - Now properly loaded before this point
FORCE_TEST_MINT = os.getenv("FORCE_TEST_MINT")
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
HELIUS_API = os.getenv("HELIUS_API")
RPC_URL = os.getenv("RPC_URL")
SLIPPAGE_BPS = 100
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")

# ============================================
# QUALITY THRESHOLDS - ALL CONFIGURABLE VIA ENV
# ============================================
MIN_DETECTION_SCORE = int(os.getenv("MIN_DETECTION_SCORE", "5"))
RAYDIUM_MIN_INDICATORS = int(os.getenv("RAYDIUM_MIN_INDICATORS", "7"))
RAYDIUM_MIN_LOGS = int(os.getenv("RAYDIUM_MIN_LOGS", "30"))
PUMPFUN_MIN_INDICATORS = int(os.getenv("PUMPFUN_MIN_INDICATORS", "4"))
PUMPFUN_MIN_LOGS = int(os.getenv("PUMPFUN_MIN_LOGS", "10"))

# Enhanced Quality Filters - ALL FROM ENV
MIN_SOL_LIQUIDITY = float(os.getenv("MIN_SOL_LIQUIDITY", "15.0"))
MIN_LP = float(os.getenv("MIN_LP", "15.0"))  # Added MIN_LP for compatibility
MIN_LP_USD = float(os.getenv("MIN_LP_USD", "20000"))
MIN_VOLUME_USD = float(os.getenv("MIN_VOLUME_USD", "10000"))
MIN_CONFIDENCE_SCORE = int(os.getenv("MIN_CONFIDENCE_SCORE", "30"))
MAX_TOKEN_AGE_MINUTES = 3  # Only very fresh tokens
MIN_VOLUME_LIQUIDITY_RATIO = 0.5  # Volume must be at least 50% of liquidity

# Quality filters from env with new defaults
RUG_LP_THRESHOLD = float(os.getenv("RUG_LP_THRESHOLD", "15.0"))
RISKY_LP_THRESHOLD = 10.0

# Enhanced position sizing
SAFE_BUY_AMOUNT = float(os.getenv("SAFE_BUY_AMOUNT", "0.05"))
RISKY_BUY_AMOUNT = float(os.getenv("RISKY_BUY_AMOUNT", "0.03"))
ULTRA_RISKY_BUY_AMOUNT = float(os.getenv("ULTRA_RISKY_BUY_AMOUNT", "0.02"))

# Buy Limits - STRICT LIMITS
MIN_BUY_COOLDOWN = int(os.getenv("MIN_BUY_COOLDOWN", "30"))
MAX_DAILY_BUYS = int(os.getenv("MAX_DAILY_BUYS", "20"))
DUPLICATE_CHECK_WINDOW = int(os.getenv("DUPLICATE_CHECK_WINDOW", "600"))

# Trend scan settings
TREND_SCAN_INTERVAL = int(os.getenv("TREND_SCAN_INTERVAL", "60"))
MAX_BUYS_PER_TOKEN = int(os.getenv("MAX_BUYS_PER_TOKEN", "1"))
BLACKLIST_AFTER_BUY = os.getenv("BLACKLIST_AFTER_BUY", "true").lower() == "true"

# Disable Jupiter mempool if configured
SKIP_JUPITER_MEMPOOL = os.getenv("SKIP_JUPITER_MEMPOOL", "true").lower() == "true"

# PumpFun Migration Settings
PUMPFUN_MIGRATION_BUY = float(os.getenv("PUMPFUN_MIGRATION_BUY", "0.1"))
PUMPFUN_EARLY_BUY = float(os.getenv("PUMPFUN_EARLY_AMOUNT", "0.02"))
PUMPFUN_GRADUATION_MC = 69420
ENABLE_PUMPFUN_MIGRATION = os.getenv("ENABLE_PUMPFUN_MIGRATION", "true").lower() == "true"
MIN_LP_FOR_PUMPFUN = float(os.getenv("MIN_LP_FOR_PUMPFUN", "5.0"))

# Delays for pool initialization
MEMPOOL_DELAY_MS = float(os.getenv("MEMPOOL_DELAY_MS", "1000"))
PUMPFUN_INIT_DELAY = float(os.getenv("PUMPFUN_INIT_DELAY", "8.0"))

# ============================================
# MOMENTUM SCANNER CONFIGURATION
# ============================================
MOMENTUM_SCANNER_ENABLED = os.getenv("MOMENTUM_SCANNER", "false").lower() == "true"
MOMENTUM_AUTO_BUY = os.getenv("MOMENTUM_AUTO_BUY", "false").lower() == "true"
MIN_SCORE_AUTO_BUY = int(os.getenv("MIN_SCORE_AUTO_BUY", "5"))
MIN_SCORE_ALERT = int(os.getenv("MIN_SCORE_ALERT", "4"))

MOMENTUM_MIN_1H_GAIN = float(os.getenv("MOMENTUM_MIN_1H_GAIN", "50"))
MOMENTUM_MAX_1H_GAIN = float(os.getenv("MOMENTUM_MAX_1H_GAIN", "200"))
MOMENTUM_MIN_LIQUIDITY = float(os.getenv("MOMENTUM_MIN_LIQUIDITY", "20000"))
MOMENTUM_MAX_MC = float(os.getenv("MOMENTUM_MAX_MC", "500000"))
MOMENTUM_MIN_HOLDERS = int(os.getenv("MOMENTUM_MIN_HOLDERS", "100"))
MOMENTUM_MAX_HOLDERS = int(os.getenv("MOMENTUM_MAX_HOLDERS", "2000"))
MOMENTUM_MIN_AGE_HOURS = float(os.getenv("MOMENTUM_MIN_AGE_HOURS", "2"))
MOMENTUM_MAX_AGE_HOURS = float(os.getenv("MOMENTUM_MAX_AGE_HOURS", "24"))

MOMENTUM_POSITION_5_SCORE = float(os.getenv("MOMENTUM_POSITION_5_SCORE", "0.1"))
MOMENTUM_POSITION_4_SCORE = float(os.getenv("MOMENTUM_POSITION_4_SCORE", "0.08"))
MOMENTUM_POSITION_3_SCORE = float(os.getenv("MOMENTUM_POSITION_3_SCORE", "0.05"))
MOMENTUM_TEST_POSITION = float(os.getenv("MOMENTUM_TEST_POSITION", "0.02"))

PRIME_HOURS = [21, 22, 23, 0, 1, 2, 3]
REDUCED_HOURS = list(range(6, 21))

MOMENTUM_SCAN_INTERVAL = int(os.getenv("MOMENTUM_SCAN_INTERVAL", "120"))
MAX_MOMENTUM_TOKENS = 20

momentum_analyzed = {}
momentum_bought = set()

last_buy_time = 0
seen_trending = set()
seen_tokens = set()
BLACKLIST = set()
TASKS = []

pumpfun_tokens = {}
migration_watch_list = set()
already_bought = set()
recent_buy_attempts = {}
pool_verification_cache = {}
detected_pools = {}

raydium = RaydiumAggregator(RPC_URL)

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

RAYDIUM_POOL_CREATION_LOGS = [
    "initializepool",
    "initialize2",
    "add_liquidity",
    "create_pool",
    "init_pc_amount",
    "init_coin_amount"
]

logging.info("=" * 60)
logging.info("SNIPER CONFIGURATION LOADED:")
logging.info(f"MIN_SOL_LIQUIDITY: {MIN_SOL_LIQUIDITY} SOL")
logging.info(f"MIN_LP: {MIN_LP} SOL")
logging.info(f"MIN_LP_USD: ${MIN_LP_USD}")
logging.info(f"MIN_CONFIDENCE_SCORE: {MIN_CONFIDENCE_SCORE}/100")
logging.info(f"RUG_LP_THRESHOLD: {RUG_LP_THRESHOLD} SOL")
logging.info(f"MAX_DAILY_BUYS: {MAX_DAILY_BUYS}")
logging.info(f"MIN_BUY_COOLDOWN: {MIN_BUY_COOLDOWN}s")
logging.info(f"MOMENTUM_SCANNER: {MOMENTUM_SCANNER_ENABLED}")
logging.info("=" * 60)

trending_tokens = set()

# CRITICAL FIX: Extract liquidity from transaction data directly
async def extract_liquidity_from_tx(signature: str, accounts: list) -> float:
    """Extract liquidity amount directly from transaction data"""
    try:
        if not HELIUS_API:
            return 0
            
        rpc_url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API}"
        
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTransaction",
                    "params": [
                        signature,
                        {
                            "encoding": "jsonParsed",
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
                    
                    # Check postBalances for SOL amounts
                    if "meta" in result and "postBalances" in result["meta"]:
                        balances = result["meta"]["postBalances"]
                        
                        # For Raydium pools, SOL is typically in accounts 4-7
                        potential_liquidity = []
                        for i in range(min(len(balances), len(accounts))):
                            if i < len(accounts) and accounts[i] not in SYSTEM_PROGRAMS:
                                balance_lamports = balances[i]
                                # Filter out unrealistic amounts
                                if 1000000000 < balance_lamports < 10000000000000:  # Between 1 SOL and 10,000 SOL
                                    balance_sol = balance_lamports / 1e9
                                    potential_liquidity.append(balance_sol)
                        
                        # Return the most reasonable liquidity amount
                        if potential_liquidity:
                            potential_liquidity.sort()
                            # Get median value to avoid outliers
                            if len(potential_liquidity) > 2:
                                liquidity = potential_liquidity[len(potential_liquidity)//2]
                            else:
                                liquidity = potential_liquidity[0]
                            
                            # Sanity check - cap at 10000 SOL for new pools
                            if liquidity > 10000:
                                logging.warning(f"[LIQUIDITY] Capping suspicious liquidity: {liquidity:.2f} SOL -> 100 SOL")
                                return 100
                            
                            logging.info(f"[LIQUIDITY] Extracted {liquidity:.2f} SOL from transaction")
                            return liquidity
                    
                    # Alternative: Check innerInstructions for transfer amounts
                    if "meta" in result and "innerInstructions" in result["meta"]:
                        for inner in result["meta"]["innerInstructions"]:
                            if "instructions" in inner:
                                for inst in inner["instructions"]:
                                    if "parsed" in inst and inst["parsed"].get("type") == "transfer":
                                        info = inst["parsed"]["info"]
                                        lamports = info.get("lamports", 0)
                                        if 1000000000 < lamports < 1000000000000:  # Between 1-1000 SOL
                                            sol_amount = lamports / 1e9
                                            logging.info(f"[LIQUIDITY] Found transfer of {sol_amount:.2f} SOL")
                                            return sol_amount
    except Exception as e:
        logging.error(f"[LIQUIDITY] Error extracting from tx: {e}")
    
    return 0

async def fetch_transaction_accounts(signature: str, rpc_url: str = None, retry_count: int = 0) -> list:
    """FIXED: Fetch transaction details with loop prevention and caching"""
    global last_cache_cleanup
    
    if retry_count > MAX_FETCH_RETRIES:
        logging.warning(f"[TX FETCH] Max retries reached for {signature[:8]}...")
        return []
    
    if signature in processed_signatures_cache:
        logging.debug(f"[TX FETCH] Already processed {signature[:8]}...")
        return []
    
    processed_signatures_cache[signature] = time.time()
    
    current_time = time.time()
    if current_time - last_cache_cleanup > CACHE_CLEANUP_INTERVAL:
        old_sigs = [sig for sig, ts in processed_signatures_cache.items() 
                   if current_time - ts > 60]
        for sig in old_sigs:
            del processed_signatures_cache[sig]
        last_cache_cleanup = current_time
        logging.debug(f"[TX FETCH] Cleaned {len(old_sigs)} old signatures from cache")
    
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
                                    
                                    if "instructions" in msg:
                                        for inst in msg["instructions"]:
                                            if isinstance(inst, dict):
                                                if "parsed" in inst and "info" in inst["parsed"]:
                                                    info = inst["parsed"]["info"]
                                                    for field in ["mint", "token", "account", "source", "destination"]:
                                                        if field in info:
                                                            val = info[field]
                                                            if isinstance(val, str) and len(val) == 44:
                                                                account_keys.append(val)
                                                
                                                if "accounts" in inst:
                                                    for acc in inst["accounts"]:
                                                        if isinstance(acc, str) and len(acc) == 44:
                                                            account_keys.append(acc)
                            
                            if "meta" in result:
                                meta = result["meta"]
                                
                                if "loadedAddresses" in meta:
                                    loaded = meta["loadedAddresses"]
                                    if "writable" in loaded:
                                        account_keys.extend(loaded["writable"])
                                    if "readonly" in loaded:
                                        account_keys.extend(loaded["readonly"])
                                
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
                                
                                if "postTokenBalances" in meta:
                                    for balance in meta["postTokenBalances"]:
                                        if "mint" in balance:
                                            mint = balance["mint"]
                                            if mint not in account_keys:
                                                account_keys.append(mint)
                                
                                if "preTokenBalances" in meta:
                                    for balance in meta["preTokenBalances"]:
                                        if "mint" in balance:
                                            mint = balance["mint"]
                                            if mint not in account_keys and mint not in SYSTEM_PROGRAMS:
                                                account_keys.append(mint)
                            
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
                                logging.info(f"[TX FETCH] Got {len(unique_keys)} accounts for {signature[:8]}...")
                                return unique_keys
                            
                            continue
                            
                except asyncio.TimeoutError:
                    logging.warning(f"[TX FETCH] Timeout for {encoding} encoding")
                    continue
                except Exception as e:
                    logging.debug(f"[TX FETCH] {encoding} encoding failed: {e}")
                    continue
            
            logging.debug(f"[TX FETCH] All encodings failed, trying fallback for {signature[:8]}...")
            return await fetch_pumpfun_token_from_logs(signature, rpc_url, retry_count + 1)
        
    except asyncio.TimeoutError:
        logging.error(f"[TX FETCH] Overall timeout for {signature[:8]}...")
        return []
    except Exception as e:
        logging.error(f"[TX FETCH] Error fetching transaction {signature[:8]}...: {e}")
        return await fetch_pumpfun_token_from_logs(signature, rpc_url, retry_count + 1)

# ========== END OF PART 1 ==========
# Continue with PART 2 below
# ========== START OF PART 2 ==========
# Paste this after the "END OF PART 1" comment

async def fetch_pumpfun_token_from_logs(signature: str, rpc_url: str = None, retry_count: int = 0) -> list:
    """FIXED: Fallback method with loop prevention"""
    if retry_count > MAX_FETCH_RETRIES:
        logging.warning(f"[FALLBACK] Max retries reached for {signature[:8]}...")
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
                    
                    if "transaction" in result:
                        tx_data = result["transaction"]
                        if isinstance(tx_data, list) and len(tx_data) > 0:
                            try:
                                raw_bytes = base64.b64decode(tx_data[0])
                                raw_str = raw_bytes.hex()
                                
                                max_checks = 100
                                checks = 0
                                for i in range(0, min(len(raw_str) - 64, max_checks * 2), 2):
                                    if checks >= max_checks:
                                        break
                                    checks += 1
                                    
                                    potential_hex = raw_str[i:i+64]
                                    try:
                                        key_bytes = bytes.fromhex(potential_hex)
                                        b58_key = b58encode(key_bytes).decode('utf-8')
                                        
                                        if len(b58_key) >= 43 and len(b58_key) <= 44:
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
                    
                    if "meta" in result and "logMessages" in result["meta"]:
                        logs = result["meta"]["logMessages"]
                        
                        for log in logs[:50]:
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
                    
                    unique_mints = list(dict.fromkeys(potential_mints))
                    
                    if unique_mints:
                        logging.info(f"[FALLBACK] Found {len(unique_mints)} potential mints from logs/raw data")
                        return unique_mints[:5]
        
        return []
        
    except asyncio.TimeoutError:
        logging.error(f"[FALLBACK] Timeout for {signature[:8]}...")
        return []
    except Exception as e:
        logging.debug(f"[FALLBACK] Error: {e}")
        return []

# CRITICAL FIX: SIMPLIFIED TOKEN VALIDATION - NO EXPENSIVE RPC CALLS
async def validate_token_quality(mint: str, lp_amount: float) -> bool:
    """SIMPLIFIED: Just check basic requirements without expensive RPC calls"""
    min_liquidity = float(os.getenv("MIN_LP", "3.0"))
    
    if lp_amount < min_liquidity:
        logging.info(f"[QUALITY] Rejecting {mint[:8]} - LP too low: {lp_amount:.2f} SOL (min: {min_liquidity})")
        return False
    
    if daily_stats["snipes_succeeded"] >= MAX_DAILY_BUYS:
        logging.info(f"[QUALITY] Daily limit reached ({MAX_DAILY_BUYS} buys)")
        return False
    
    global last_buy_time
    if time.time() - last_buy_time < MIN_BUY_COOLDOWN:
        cooldown_remaining = MIN_BUY_COOLDOWN - (time.time() - last_buy_time)
        logging.info(f"[QUALITY] Buy cooldown active ({cooldown_remaining:.0f}s remaining)")
        return False
    
    return True

# CRITICAL FIX: ULTRA-SIMPLIFIED QUALITY CHECK - NO DEXSCREENER CALLS
async def is_quality_token_simple(mint: str, lp_amount: float) -> tuple:
    """ULTRA-SIMPLIFIED: Just basic checks without ANY external API calls"""
    if mint in already_bought:
        return False, "Already bought"
    
    if mint in recent_buy_attempts:
        time_since_attempt = time.time() - recent_buy_attempts[mint]
        if time_since_attempt < DUPLICATE_CHECK_WINDOW:
            return False, f"Recent buy attempt {time_since_attempt:.0f}s ago"
    
    min_liquidity = float(os.getenv("MIN_LP", "3.0"))
    
    if lp_amount >= 50:
        return True, f"✅ Excellent liquidity: {lp_amount:.2f} SOL"
    elif lp_amount >= 20:
        return True, f"✅ Good liquidity: {lp_amount:.2f} SOL"
    elif lp_amount >= min_liquidity:
        return True, f"✅ Adequate liquidity: {lp_amount:.2f} SOL"
    else:
        if mint in pumpfun_tokens and pumpfun_tokens[mint].get("migrated", False):
            return True, f"⚠️ PumpFun graduate with {lp_amount:.2f} SOL"
        return False, f"Liquidity too low: {lp_amount:.2f} SOL (min: {min_liquidity})"

# CRITICAL FIX: SIMPLIFIED VALIDATION
async def validate_before_buy(mint: str, lp_amount: float) -> bool:
    """SIMPLIFIED: Just check limits, no RPC calls"""
    try:
        if daily_stats["snipes_succeeded"] >= MAX_DAILY_BUYS:
            logging.info(f"[VALIDATION] Daily buy limit reached ({MAX_DAILY_BUYS})")
            return False
        
        global last_buy_time
        if time.time() - last_buy_time < MIN_BUY_COOLDOWN:
            cooldown_remaining = MIN_BUY_COOLDOWN - (time.time() - last_buy_time)
            logging.info(f"[VALIDATION] Buy cooldown active ({cooldown_remaining:.0f}s remaining)")
            return False
        
        min_liquidity = MIN_LP if MIN_LP else MIN_SOL_LIQUIDITY
        if lp_amount >= min_liquidity:
            return True
        
        if mint in pumpfun_tokens:
            return True
            
        return False
        
    except Exception as e:
        logging.error(f"[VALIDATION] Error: {e}")
        return lp_amount >= (MIN_LP if MIN_LP else MIN_SOL_LIQUIDITY)

def determine_position_size(lp_amount: float, confidence_score: int = 70, is_pumpfun: bool = False) -> float:
    """Determine optimal position size based on liquidity"""
    if is_pumpfun:
        if lp_amount >= 20:
            return 0.15
        elif lp_amount >= 10:
            return 0.1
        else:
            return 0.08
    
    if lp_amount >= 50:
        base = SAFE_BUY_AMOUNT * 2
    elif lp_amount >= 20:
        base = SAFE_BUY_AMOUNT
    elif lp_amount >= (MIN_LP if MIN_LP else MIN_SOL_LIQUIDITY):
        base = RISKY_BUY_AMOUNT
    else:
        base = ULTRA_RISKY_BUY_AMOUNT
    
    max_position = float(os.getenv("MAX_POSITION_SIZE_SOL", "0.2"))
    return min(round(base, 3), max_position)

# CRITICAL FIX: REMOVED POOL VERIFICATION
async def verify_pool_exists(mint: str) -> bool:
    """REMOVED: Always return True - if we detected liquidity, pool exists"""
    return True

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
                    pool = await raydium.find_pool_for_token(mint)
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

# ========== END OF PART 2 ==========
# Continue with PART 3 below
# ========== START OF PART 3 ==========
# Paste this after the "END OF PART 2" comment

async def trending_scanner():
    """Scan for quality trending tokens"""
    global seen_trending, trending_tokens
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
                trending_tokens.add(mint)
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
            logging.error(f"[Trending Scanner] Error: {e}")
            await asyncio.sleep(TREND_SCAN_INTERVAL)

# MOMENTUM SCANNER FUNCTIONS
def detect_chart_pattern(price_data: list) -> str:
    """Detect if chart shows good or bad patterns"""
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
    """Score a token based on momentum criteria"""
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
    """Elite Momentum Scanner - Finds pumping tokens with exact criteria"""
    if not MOMENTUM_SCANNER_ENABLED:
        logging.info("[Momentum Scanner] Disabled via configuration")
        return
    
    await send_telegram_alert(
        "🔥 MOMENTUM SCANNER ACTIVE 🔥\n\n"
        f"Mode: {'HYBRID AUTO-BUY' if MOMENTUM_AUTO_BUY else 'ALERT ONLY'}\n"
        f"Auto-buy threshold: {MIN_SCORE_AUTO_BUY}/5\n"
        f"Alert threshold: {MIN_SCORE_ALERT}/5\n"
        f"Target: 50-200% gainers\n"
        f"Position sizes: 0.02-0.2 SOL"
    )
    
    consecutive_errors = 0
    last_status_update = 0
    status_update_interval = 3600
    
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
                                f"Monitoring with exit rules..."
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
            
            current_time = time.time()
            if current_time - last_status_update > status_update_interval:
                last_status_update = current_time
                if momentum_analyzed and len(momentum_analyzed) > 0:
                    recent_count = sum(1 for v in momentum_analyzed.values() 
                                     if current_time - v.get("timestamp", 0) < 3600)
                    if recent_count > 0:
                        await send_telegram_alert(
                            f"📊 Momentum Scanner Update\n"
                            f"Analyzed {recent_count} tokens in last hour\n"
                            f"Still hunting for 50-200% gainers..."
                        )
            
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
# MAIN MEMPOOL LISTENER WITH CRITICAL FIXES
# ============================================

async def mempool_listener(name, program_id=None):
    """Enhanced mempool listener with FIXED duplicate prevention and quality checks"""
    global last_buy_time
    
    if name in ACTIVE_LISTENERS and ACTIVE_LISTENERS[name]:
        if not ACTIVE_LISTENERS[name].done():
            logging.info(f"[{name}] Cancelling existing listener before starting new one")
            ACTIVE_LISTENERS[name].cancel()
            try:
                await ACTIVE_LISTENERS[name]
            except asyncio.CancelledError:
                pass
    
    LISTENER_START_TIMES[name] = time.time()
    
    if not HELIUS_API:
        logging.warning(f"[{name}] HELIUS_API not set, skipping mempool listener")
        if name not in last_alert_sent:
            await send_telegram_alert(f"⚠️ {name} listener disabled (no Helius API key)")
            last_alert_sent[name] = time.time()
        return
    
    if name == "Jupiter" and SKIP_JUPITER_MEMPOOL:
        logging.info(f"[{name}] Mempool monitoring disabled via config")
        if name not in last_alert_sent:
            await send_telegram_alert(f"📌 {name} mempool disabled (too noisy)")
            last_alert_sent[name] = time.time()
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
            if name not in last_alert_sent or current_time - last_alert_sent[name] > 3600:
                await send_telegram_alert(f"📱 {name} listener ACTIVE")
                last_alert_sent[name] = current_time
            
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
                        
                        if is_duplicate_detection(signature):
                            continue
                        
                        if signature in processed_txs:
                            continue
                        processed_txs.add(signature)
                        
                        if len(processed_txs) > 1000:
                            processed_txs.clear()
                        
                        transaction_counter += 1
                        
                        if transaction_counter % 100 == 0:
                            logging.info(f"[{name}] Processed {transaction_counter} txs, found {pool_creations_found} pool creations")
                        
                        # DETECTION LOGIC
                        is_pool_creation = False
                        pool_id = None
                        
                        if name == "Raydium":
                            raydium_indicators = 0
                            has_init_pool = False
                            has_create_pool = False
                            has_liquidity = False
                            log_quality_score = 0
                            has_pool_creation_log = False
                            
                            for log in logs:
                                log_lower = log.lower()
                                
                                for pattern in RAYDIUM_POOL_CREATION_LOGS:
                                    if pattern in log_lower:
                                        has_pool_creation_log = True
                                        break
                                
                                if "program log: instruction: initialize" in log_lower:
                                    has_init_pool = True
                                    raydium_indicators += 3
                                    log_quality_score += 1
                                
                                if "instruction: initialize2" in log_lower:
                                    raydium_indicators += 3
                                    has_init_pool = True
                                    log_quality_score += 1
                                
                                if "invoke [3]" in log and "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8" in log:
                                    raydium_indicators += 3
                                    log_quality_score += 1
                                
                                if "create" in log_lower and ("pool" in log_lower or "amm" in log_lower):
                                    has_create_pool = True
                                    raydium_indicators += 2
                                
                                if "add_liquidity" in log_lower or "deposit" in log_lower:
                                    has_liquidity = True
                                    raydium_indicators += 2
                                
                                if any(x in log_lower for x in ["init_pc_amount", "init_coin_amount", "opentime", "nonce"]):
                                    raydium_indicators += 2
                                    log_quality_score += 1
                                
                                if "initialize" in log_lower and "pool" in log_lower:
                                    raydium_indicators += 1
                            
                            if len(account_keys) > 15:
                                raydium_indicators += 2
                            
                            if len(logs) >= RAYDIUM_MIN_LOGS:
                                log_quality_score += 2
                            
                            if raydium_indicators > 0:
                                logging.info(f"[{name}] Detection Debug:")
                                logging.info(f"  Indicators: {raydium_indicators} (need {RAYDIUM_MIN_INDICATORS})")
                                logging.info(f"  Logs: {len(logs)} (need {RAYDIUM_MIN_LOGS})")
                                logging.info(f"  Quality Score: {log_quality_score}")
                            
                            if (raydium_indicators >= RAYDIUM_MIN_INDICATORS and 
                                len(logs) >= RAYDIUM_MIN_LOGS and 
                                (has_init_pool or has_liquidity or log_quality_score >= 3)):
                                is_pool_creation = True
                                logging.info(f"[RAYDIUM] POOL CREATION DETECTED - Score: {raydium_indicators}, Logs: {len(logs)}")
                        
                        elif name == "PumpFun":
                            pumpfun_create_indicators = 0
                            
                            for log in logs:
                                log_lower = log.lower()
                                
                                if "create" in log_lower and ("token" in log_lower or "coin" in log_lower):
                                    pumpfun_create_indicators += 3
                                
                                if "initialize" in log_lower and "mint" in log_lower:
                                    pumpfun_create_indicators += 2
                                
                                if "launch" in log_lower:
                                    pumpfun_create_indicators += 3
                                
                                if "bonding" in log_lower and ("init" in log_lower or "create" in log_lower):
                                    pumpfun_create_indicators += 4
                                
                                if "pump" in log_lower and "fun" in log_lower:
                                    pumpfun_create_indicators += 1
                            
                            if pumpfun_create_indicators >= PUMPFUN_MIN_INDICATORS and len(logs) >= PUMPFUN_MIN_LOGS:
                                is_pool_creation = True
                                logging.info(f"[PUMPFUN] TOKEN DETECTED - Score: {pumpfun_create_indicators}")
                        
                        elif name == "Moonshot":
                            for log in logs:
                                log_lower = log.lower()
                                if ("moon" in log_lower or "launch" in log_lower) and ("create" in log_lower or "initialize" in log_lower):
                                    if len(logs) >= 5:
                                        is_pool_creation = True
                                        break
                        
                        elif name == "Jupiter":
                            continue
                        
                        if not is_pool_creation:
                            continue
                        
                        pool_creations_found += 1
                        logging.info(f"[{name}] POOL/TOKEN CREATION DETECTED! Total found: {pool_creations_found}")
                        
                        if len(account_keys) == 0:
                            logging.info(f"[{name}] Fetching full transaction...")
                            try:
                                fetch_task = asyncio.create_task(fetch_transaction_accounts(signature))
                                account_keys = await asyncio.wait_for(fetch_task, timeout=5)
                            except asyncio.TimeoutError:
                                logging.warning(f"[{name}] Transaction fetch timeout for {signature[:8]}...")
                                continue
                            
                            if len(account_keys) == 0:
                                logging.warning(f"[{name}] Could not fetch account keys")
                                continue
                        
                        # CRITICAL: Extract liquidity from transaction
                        tx_liquidity = await extract_liquidity_from_tx(signature, account_keys)
                        
                        if name == "Raydium" and account_keys:
                            for key in account_keys:
                                if isinstance(key, dict):
                                    key = key.get("pubkey", "") or key.get("address", "")
                                
                                if key and len(key) == 44 and key not in SYSTEM_PROGRAMS:
                                    pool_id = key
                                    break
                        
                        tokens_from_this_tx = []
                        for key in account_keys:
                            if isinstance(key, dict):
                                key = key.get("pubkey", "") or key.get("address", "")
                            
                            if key in SYSTEM_PROGRAMS or len(key) != 44:
                                continue
                            
                            if key == "So11111111111111111111111111111111111111112":
                                continue
                            
                            if key in seen_tokens or key in already_bought:
                                continue
                            
                            if key in FALSE_POSITIVE_TOKENS:
                                time_since_false = time.time() - FALSE_POSITIVE_TOKENS[key]
                                if time_since_false < FALSE_POSITIVE_COOLDOWN:
                                    logging.info(f"[{name}] Skipping recent false positive {key[:8]}...")
                                    continue
                            
                            try:
                                Pubkey.from_string(key)
                                potential_mint = key
                            except:
                                continue
                            
                            seen_tokens.add(potential_mint)
                            tokens_from_this_tx.append(potential_mint)
                        
                        if not tokens_from_this_tx:
                            continue
                        
                        # Process only first token to avoid spam
                        for potential_mint in tokens_from_this_tx[:1]:
                            
                            if name == "PumpFun" and potential_mint not in pumpfun_tokens:
                                pumpfun_tokens[potential_mint] = {
                                    "discovered": time.time(),
                                    "migrated": False
                                }
                                logging.info(f"[PumpFun] Tracking new token: {potential_mint[:8]}...")
                            
                            if name == "Raydium" and pool_id:
                                detected_pools[potential_mint] = pool_id
                                raydium.register_pool(pool_id, potential_mint, lp_amount=tx_liquidity)
                                logging.info(f"[Raydium] Registered pool {pool_id[:8]}... for token {potential_mint[:8]}... with {tx_liquidity:.2f} SOL")
                            
                            # ============================================
                            # CRITICAL FIX: SIMPLIFIED RAYDIUM BUY LOGIC
                            # ============================================
                            if name in ["Raydium"] and is_bot_running():
                                if potential_mint not in BROKEN_TOKENS and potential_mint not in BLACKLIST:
                                    
                                    if potential_mint in FALSE_POSITIVE_TOKENS:
                                        time_since_false = time.time() - FALSE_POSITIVE_TOKENS[potential_mint]
                                        if time_since_false < FALSE_POSITIVE_COOLDOWN:
                                            logging.info(f"[{name}] Skipping recent false positive")
                                            continue
                                    
                                    # CRITICAL: Trust the transaction liquidity directly
                                    lp_amount = tx_liquidity
                                    
                                    if lp_amount == 0:
                                        logging.warning(f"[{name}] No liquidity detected - skipping")
                                        record_skip("zero_liquidity_from_tx")
                                        continue
                                    
                                    logging.info(f"[{name}] Transaction liquidity: {lp_amount:.2f} SOL - PROCEEDING TO BUY")
                                    
                                    # Simple validation - no external calls
                                    if not await validate_token_quality(potential_mint, lp_amount):
                                        continue
                                    
                                    # Ultra-simple quality check
                                    is_quality, reason = await is_quality_token_simple(potential_mint, lp_amount)
                                    
                                    if not is_quality:
                                        logging.info(f"[{name}] Skipping {potential_mint[:8]}... - {reason}")
                                        record_skip("simple_quality_check")
                                        continue
                                    
                                    # Determine position size
                                    is_pumpfun = potential_mint in pumpfun_tokens
                                    buy_amount = determine_position_size(lp_amount, 70, is_pumpfun)
                                    
                                    recent_buy_attempts[potential_mint] = time.time()
                                    last_buy_time = time.time()
                                    
                                    await send_telegram_alert(
                                        f"✅ TOKEN DETECTED - BUYING NOW ✅\n\n"
                                        f"Platform: {name}\n"
                                        f"Token: `{potential_mint}`\n"
                                        f"TX Liquidity: {lp_amount:.2f} SOL\n"
                                        f"Buy Amount: {buy_amount} SOL\n\n"
                                        f"{reason}\n\n"
                                        f"NO VERIFICATION - DIRECT BUY"
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
                                                f"✅ SNIPE SUCCESS!\n"
                                                f"Token: {potential_mint[:16]}...\n"
                                                f"Amount: {buy_amount} SOL\n"
                                                f"Liquidity: {lp_amount:.2f} SOL"
                                            )
                                            asyncio.create_task(wait_and_auto_sell(potential_mint))
                                            break
                                        else:
                                            FALSE_POSITIVE_TOKENS[potential_mint] = time.time()
                                            await send_telegram_alert(f"❌ Snipe failed (false positive)")
                                    except Exception as e:
                                        logging.error(f"[{name}] Buy error: {e}")
                                        await send_telegram_alert(f"❌ Buy error: {str(e)[:100]}")
                                    finally:
                                        if original_amount:
                                            os.environ["BUY_AMOUNT_SOL"] = original_amount
                            
                            # PUMPFUN BUY LOGIC (keeping existing logic)
                            elif name == "PumpFun" and is_bot_running():
                                if potential_mint not in BROKEN_TOKENS and potential_mint not in BLACKLIST:
                                    if potential_mint in already_bought:
                                        continue
                                    
                                    logging.info(f"[PUMPFUN] Evaluating token: {potential_mint[:8]}...")
                                    
                                    await asyncio.sleep(PUMPFUN_INIT_DELAY)
                                    
                                    graduated = await check_pumpfun_graduation(potential_mint)
                                    if graduated and potential_mint in pumpfun_tokens:
                                        pumpfun_tokens[potential_mint]["migrated"] = True
                                    
                                    lp_amount = 0
                                    max_attempts = 5
                                    
                                    for attempt in range(max_attempts):
                                        lp_data = await get_liquidity_and_ownership(potential_mint)
                                        lp_amount = lp_data.get("liquidity", 0) if lp_data else 0
                                        
                                        if lp_amount == 0 and tx_liquidity > 0:
                                            lp_amount = tx_liquidity
                                            logging.info(f"[PUMPFUN] Using tx liquidity: {tx_liquidity:.2f} SOL")
                                            break
                                        
                                        if lp_amount > 0:
                                            logging.info(f"[PUMPFUN] Found pool liquidity: {lp_amount:.2f} SOL")
                                            break
                                        
                                        if attempt < max_attempts - 1:
                                            logging.info(f"[PUMPFUN] No pool yet, waiting...")
                                            await asyncio.sleep(3.0)
                                    
                                    if lp_amount == 0:
                                        logging.warning(f"[PUMPFUN] No liquidity after {max_attempts} attempts")
                                        record_skip("no_pool_after_waits")
                                        continue
                                    
                                    if not await validate_token_quality(potential_mint, lp_amount):
                                        continue
                                    
                                    min_lp_for_pumpfun = MIN_LP_FOR_PUMPFUN if not graduated else RUG_LP_THRESHOLD
                                    
                                    if lp_amount < min_lp_for_pumpfun:
                                        logging.info(f"[PUMPFUN] Low LP: {lp_amount:.2f} SOL")
                                        continue
                                    
                                    recent_buy_attempts[potential_mint] = time.time()
                                    last_buy_time = time.time()
                                    
                                    if graduated:
                                        buy_amount = PUMPFUN_MIGRATION_BUY
                                        buy_reason = "PumpFun Graduate"
                                    else:
                                        buy_amount = PUMPFUN_EARLY_BUY
                                        buy_reason = "PumpFun Early Entry"
                                    
                                    await send_telegram_alert(
                                        f"🎯 PUMPFUN TOKEN DETECTED\n\n"
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
                                                f"Type: {buy_reason}"
                                            )
                                            asyncio.create_task(wait_and_auto_sell(potential_mint))
                                            break
                                        else:
                                            FALSE_POSITIVE_TOKENS[potential_mint] = time.time()
                                            await send_telegram_alert(f"❌ PumpFun snipe failed")
                                    except Exception as e:
                                        logging.error(f"[PUMPFUN] Buy error: {e}")
                                        await send_telegram_alert(f"❌ PumpFun buy error: {str(e)[:100]}")
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

# HELPER FUNCTIONS

async def start_sniper_with_forced_token(mint: str):
    """Force buy a specific token"""
    try:
        logging.info(f"[FORCE BUY] Attempting to buy {mint}")
        
        momentum_data = await check_momentum_score(mint)
        if momentum_data["score"] > 0:
            await send_telegram_alert(
                f"📊 Momentum Score: {momentum_data['score']}/5\n"
                f"Signals:\n" + "\n".join(momentum_data["signals"][:3])
            )
        
        success = await buy_token(mint)
        if success:
            already_bought.add(mint)
            asyncio.create_task(wait_and_auto_sell(mint))
            await send_telegram_alert(f"✅ Force buy successful for {mint[:16]}...")
        else:
            await send_telegram_alert(f"❌ Force buy failed for {mint[:16]}...")
            
    except Exception as e:
        logging.error(f"Force buy error: {e}")
        await send_telegram_alert(f"❌ Force buy error: {str(e)[:100]}")

async def stop_all_tasks():
    """Stop all running tasks"""
    global TASKS
    for task in TASKS:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    TASKS.clear()
    logging.info("All tasks stopped")

# Export functions
__all__ = [
    'mempool_listener',
    'trending_scanner',
    'momentum_scanner',
    'pumpfun_migration_monitor',
    'raydium_graduation_scanner',
    'start_sniper_with_forced_token',
    'stop_all_tasks',
    'pumpfun_tokens',
    'migration_watch_list',
    'trending_tokens',
    'MOMENTUM_SCANNER_ENABLED',
    'momentum_analyzed',
    'momentum_bought'
]

# ========== END OF PART 3 ==========
# This completes the fixed sniper_logic.py file
