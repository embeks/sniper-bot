# sniper_logic.py - COMPLETE FIXED VERSION WITH ULTRA-FRESH PUMPFUN SUPPORT
from shared_state import pumpfun_tokens, already_bought, recent_buy_attempts, migration_watch_list, detected_pools, momentum_analyzed, momentum_bought
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
from dotenv import load_dotenv
import httpx
import random
from dexscreener_monitor import start_dexscreener_monitor

from utils import (
    is_valid_mint, buy_token, log_skipped_token, send_telegram_alert,
    get_trending_mints, wait_and_auto_sell, get_liquidity_and_ownership,
    is_bot_running, keypair, BROKEN_TOKENS,
    mark_broken_token, daily_stats_reset_loop,
    update_last_activity, increment_stat, record_skip,
    listener_status, last_seen_token,
    is_fresh_token, verify_token_age_on_chain, is_pumpfun_launch,
    HTTPManager, notify, raydium, rpc  # Import shared raydium and rpc instances
)
from solders.pubkey import Pubkey

load_dotenv()

# ============================================
# CRITICAL FIX: Transaction cache to prevent infinite loops
# ============================================
processed_signatures_cache = {}
CACHE_CLEANUP_INTERVAL = 300
last_cache_cleanup = time.time()
MAX_FETCH_RETRIES = 2

# ============================================
# ENHANCED DUPLICATE PREVENTION WITH ANTI-SPAM
# ============================================
ACTIVE_LISTENERS = set()
LISTENER_TASKS = {}
last_listener_status_msg = {}
STATUS_MSG_COOLDOWN = 300

# Telegram anti-spam tracking
telegram_message_cache = {}
TELEGRAM_COOLDOWN = 60

FORCE_TEST_MINT = os.getenv("FORCE_TEST_MINT")
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
HELIUS_API = os.getenv("HELIUS_API")

# FIXED: Read thresholds from env with better defaults
RUG_LP_THRESHOLD = float(os.getenv("RUG_LP_THRESHOLD", 0.3))  # Lowered for fresh tokens
RISKY_LP_THRESHOLD = 1.5
TREND_SCAN_INTERVAL = int(os.getenv("TREND_SCAN_INTERVAL", 60))
RPC_URL = os.getenv("RPC_URL")
SLIPPAGE_BPS = 100
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")

# Enhanced position sizing
SAFE_BUY_AMOUNT = float(os.getenv("SAFE_BUY_AMOUNT", 0.02))
RISKY_BUY_AMOUNT = float(os.getenv("RISKY_BUY_AMOUNT", 0.02))
ULTRA_RISKY_BUY_AMOUNT = float(os.getenv("ULTRA_RISKY_BUY_AMOUNT", 0.01))

# Quality filters - ADJUSTED FOR BETTER DETECTION
MIN_AI_SCORE = float(os.getenv("MIN_AI_SCORE", 0.05))
MIN_HOLDER_COUNT = int(os.getenv("MIN_HOLDER_COUNT", 3))
MAX_TOP_HOLDER_PERCENT = float(os.getenv("MAX_TOP_HOLDER_PERCENT", 35))
MIN_BUYS_COUNT = int(os.getenv("MIN_BUYS_COUNT", 2))
MIN_BUY_SELL_RATIO = float(os.getenv("MIN_BUY_SELL_RATIO", 1.5))

# FIXED: Better detection thresholds
RAYDIUM_MIN_INDICATORS = int(os.getenv("RAYDIUM_MIN_INDICATORS", 2))
RAYDIUM_MIN_LOGS = int(os.getenv("RAYDIUM_MIN_LOGS", 20))
PUMPFUN_MIN_INDICATORS = int(os.getenv("PUMPFUN_MIN_INDICATORS", 2))
PUMPFUN_MIN_LOGS = int(os.getenv("PUMPFUN_MIN_LOGS", 1))

# Anti-duplicate settings
DUPLICATE_CHECK_WINDOW = int(os.getenv("DUPLICATE_CHECK_WINDOW", 300))
MAX_BUYS_PER_TOKEN = int(os.getenv("MAX_BUYS_PER_TOKEN", 1))
BLACKLIST_AFTER_BUY = os.getenv("BLACKLIST_AFTER_BUY", "true").lower() == "true"

# Disable Jupiter mempool if configured
SKIP_JUPITER_MEMPOOL = os.getenv("SKIP_JUPITER_MEMPOOL", "true").lower() == "true"

# PumpFun Migration Settings - FIXED TO CHECK ENV
PUMPFUN_MIGRATION_BUY = float(os.getenv("PUMPFUN_MIGRATION_BUY", 0.1))
PUMPFUN_EARLY_BUY = float(os.getenv("PUMPFUN_EARLY_AMOUNT", 0.02))
PUMPFUN_GRADUATION_MC = 69420
ENABLE_PUMPFUN_MIGRATION = os.getenv("ENABLE_PUMPFUN_MIGRATION", "false").lower() == "true"  # Changed default to false
MIN_LP_FOR_PUMPFUN = float(os.getenv("MIN_LP_FOR_PUMPFUN", 0.2))

# Delays for pool initialization
MEMPOOL_DELAY_MS = float(os.getenv("MEMPOOL_DELAY_MS", 100))
PUMPFUN_INIT_DELAY = float(os.getenv("PUMPFUN_INIT_DELAY", 1.0))  # Increased to give tokens more time to initialize

# ============================================
# AGE CHECKING CONFIGURATION - USING ENV VARS
# ============================================
STRICT_AGE_CHECK = os.getenv("STRICT_AGE_CHECK", "true").lower() == "true"
MAX_TOKEN_AGE_SECONDS = int(os.getenv("MAX_TOKEN_AGE_SECONDS", 60))  # FIXED: Default to 60
SKIP_TRENDING_SCANNER = os.getenv("SKIP_TRENDING_SCANNER", "true").lower() == "true"  # Default to skip

# ============================================
# MOMENTUM SCANNER CONFIGURATION - CHECK ENV
# ============================================
MOMENTUM_SCANNER_ENABLED = os.getenv("MOMENTUM_SCANNER", "false").lower() == "true"  # Default to false
MOMENTUM_AUTO_BUY = os.getenv("MOMENTUM_AUTO_BUY", "true").lower() == "true"
MIN_SCORE_AUTO_BUY = int(os.getenv("MIN_SCORE_AUTO_BUY", 3))
MIN_SCORE_ALERT = int(os.getenv("MIN_SCORE_ALERT", 3))

# Momentum rules - FROM ENV with better defaults
MOMENTUM_MIN_1H_GAIN = float(os.getenv("MOMENTUM_MIN_1H_GAIN", 50))
MOMENTUM_MAX_1H_GAIN = float(os.getenv("MOMENTUM_MAX_1H_GAIN", 200))
MOMENTUM_MIN_LIQUIDITY = float(os.getenv("MOMENTUM_MIN_LIQUIDITY", 2000))
MOMENTUM_MAX_MC = float(os.getenv("MOMENTUM_MAX_MC", 500000))
MOMENTUM_MIN_HOLDERS = int(os.getenv("MOMENTUM_MIN_HOLDERS", 100))
MOMENTUM_MAX_HOLDERS = int(os.getenv("MOMENTUM_MAX_HOLDERS", 2000))
MOMENTUM_MIN_AGE_HOURS = float(os.getenv("MOMENTUM_MIN_AGE_HOURS", 2))
MOMENTUM_MAX_AGE_HOURS = float(os.getenv("MOMENTUM_MAX_AGE_HOURS", 24))

# Position sizing based on score
MOMENTUM_POSITION_5_SCORE = float(os.getenv("MOMENTUM_POSITION_5_SCORE", 0.20))
MOMENTUM_POSITION_4_SCORE = float(os.getenv("MOMENTUM_POSITION_4_SCORE", 0.15))
MOMENTUM_POSITION_3_SCORE = float(os.getenv("MOMENTUM_POSITION_3_SCORE", 0.10))
MOMENTUM_POSITION_2_SCORE = float(os.getenv("MOMENTUM_POSITION_2_SCORE", 0.05))
MOMENTUM_TEST_POSITION = float(os.getenv("MOMENTUM_TEST_POSITION", 0.02))

# Trading hours (AEST)
PRIME_HOURS = [21, 22, 23, 0, 1, 2, 3]
REDUCED_HOURS = list(range(6, 21))

# Scan settings
MOMENTUM_SCAN_INTERVAL = int(os.getenv("MOMENTUM_SCAN_INTERVAL", 120))
MAX_MOMENTUM_TOKENS = 20

seen_tokens = set()
BLACKLIST = set()
TASKS = []

# Enhanced tracking
pool_verification_cache = {}

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
# CRITICAL FIX: Instruction discriminators for actual token/pool creation
# ============================================
PUMPFUN_CREATE_DISCRIMINATOR = bytes([181, 157, 89, 67, 207, 21, 162, 103])  # Actual create instruction
RAYDIUM_INIT_DISCRIMINATOR = bytes([175, 175, 109, 31, 13, 152, 155, 237])  # Initialize pool V4
PUMPFUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

MIN_LP_USD = float(os.getenv("MIN_LP_USD", 500))  # Lowered for fresh tokens
MIN_VOLUME_USD = float(os.getenv("MIN_VOLUME_USD", 500))  # Lowered for fresh tokens
seen_trending = set()

# ============================================
# SNIPERBOT CLASS WITH TRANSACTION PROCESSING
# ============================================
class SniperBot:
    """Enhanced Solana sniper bot with PumpFun support"""
    
    def __init__(self):
        self.rpc = rpc  # Use shared RPC client
        self.processed_signatures = set()
        self.http_manager = HTTPManager
    
    async def get_signatures_for_program(self, program_id: str, limit: int = 100):
        """Get recent signatures for a program"""
        try:
            response = self.rpc.get_signatures_for_address(
                Pubkey.from_string(program_id),
                limit=limit
            )
            return response.value if response else []
        except Exception as e:
            logging.error(f"Error getting signatures: {e}")
            return []
    
    async def get_transaction(self, signature: str):
        """Get transaction details"""
        try:
            response = self.rpc.get_transaction(
                signature,
                encoding="jsonParsed",
                max_supported_transaction_version=0
            )
            if response and response.value:
                return response.value.transaction
            return None
        except Exception as e:
            logging.debug(f"Error getting transaction: {e}")
            return None
    
    async def get_token_age(self, token_address: str) -> int:
        """Get token age in seconds"""
        try:
            # This is a simplified version - you'd need to implement actual token age checking
            # by looking at the token creation transaction timestamp
            return await verify_token_age_on_chain(token_address, 999999)
        except Exception as e:
            logging.error(f"Error getting token age: {e}")
            return None
    
    def _is_pumpfun_creation(self, tx) -> bool:
        """Check if transaction is a PumpFun token creation"""
        try:
            # Check logs for PumpFun creation indicators
            if 'meta' in tx and 'logMessages' in tx['meta']:
                logs = tx['meta']['logMessages']
                pumpfun_indicators = 0
                
                for log in logs:
                    log_lower = log.lower()
                    if "create" in log_lower and ("token" in log_lower or "coin" in log_lower):
                        pumpfun_indicators += 3
                    if "launch" in log_lower:
                        pumpfun_indicators += 3
                    if "bonding" in log_lower and ("init" in log_lower or "create" in log_lower):
                        pumpfun_indicators += 4
                
                if pumpfun_indicators >= PUMPFUN_MIN_INDICATORS:
                    logging.info(f"[PUMPFUN] Token creation detected via logs (score: {pumpfun_indicators})")
                    token_mint = self._extract_pumpfun_token(tx)
                    if token_mint and token_mint not in pumpfun_tokens:
                        pumpfun_tokens[token_mint] = {
                            "discovered": time.time(),
                            "verified": True,
                            "migrated": False
                        }
                        logging.info(f"[PUMPFUN] Pre-registered token {token_mint[:8]}... in global dict")
                    return True
            
            return False
            
        except Exception as e:
            logging.error(f"Error checking PumpFun creation: {e}")
            return False
    
    def _extract_pumpfun_token(self, tx) -> str:
        """Extract token address from PumpFun transaction"""
        try:
            # Get accounts from transaction
            accounts = []
            if 'message' in tx:
                msg = tx['message']
                if 'accountKeys' in msg:
                    accounts = msg['accountKeys']
            
            # Find the token mint (usually first non-system account)
            for account in accounts:
                if isinstance(account, dict):
                    account = account.get('pubkey', '')
                
                if account and len(account) >= 43 and account not in SYSTEM_PROGRAMS:
                    if account != "So11111111111111111111111111111111111111112":
                        return account
            
            return None
            
        except Exception as e:
            logging.error(f"Error extracting PumpFun token: {e}")
            return None
    
    def _get_bonding_curve(self, token_address: str):
        """Get bonding curve account for a PumpFun token"""
        try:
            mint_pubkey = Pubkey.from_string(token_address)
            bonding_curve_seed = b"bonding-curve"
            pumpfun_program = Pubkey.from_string(PUMPFUN_PROGRAM_ID)
            
            bonding_curve, _ = Pubkey.find_program_address(
                [bonding_curve_seed, bytes(mint_pubkey)],
                pumpfun_program
            )
            
            bc_info = self.rpc.get_account_info(bonding_curve)
            if bc_info and bc_info.value:
                # Return bonding curve data
                return {
                    'address': str(bonding_curve),
                    'virtualSolReserves': bc_info.value.lamports  # lamports are already the SOL reserves
                }
            
            return None
            
        except Exception as e:
            logging.error(f"Error getting bonding curve: {e}")
            return None
    
    async def should_buy_token(self, token_address: str) -> bool:
        """Check if token meets buy criteria"""
        try:
            # Check if already bought
            if token_address in already_bought:
                return False
            
            # Check freshness
            is_fresh = await is_fresh_token(token_address, MAX_TOKEN_AGE_SECONDS)
            if not is_fresh:
                return False
            
            # Additional checks can go here
            return True
            
        except Exception as e:
            logging.error(f"Error checking buy criteria: {e}")
            return False
    
    async def process_pumpfun_transactions(self, max_signatures=1000):
        """Process PumpFun transactions to detect new token creations - FIXED VERSION"""
        try:
            logging.info("[PumpFun] Starting transaction scan...")
            
            # Get recent signatures for PumpFun program
            signatures = await self.get_signatures_for_program(
                PUMPFUN_PROGRAM_ID, 
                limit=max_signatures
            )
            
            if not signatures:
                logging.warning("[PumpFun] No signatures found")
                return []
            
            logging.info(f"[PumpFun] Processing {len(signatures)} transactions...")
            created_tokens = []
            processed_count = 0
            
            # Process in batches for efficiency
            batch_size = 50
            for i in range(0, len(signatures), batch_size):
                batch = signatures[i:i+batch_size]
                
                for sig_info in batch:
                    processed_count += 1
                    
                    # Log progress periodically
                    if processed_count % 100 == 0:
                        logging.info(f"[PumpFun] Processed {processed_count} txs, found {len(created_tokens)} pool creations")
                    
                    try:
                        signature = sig_info.get('signature')
                        if not signature:
                            continue
                        
                        # Check if we've already processed this transaction
                        if signature in self.processed_signatures:
                            continue
                        
                        # Get transaction details
                        tx = await self.get_transaction(signature)
                        if not tx or 'meta' not in tx or tx['meta'].get('err'):
                            continue
                        
                        # Look for PumpFun pool creation in logs
                        if not self._is_pumpfun_creation(tx):
                            continue
                        
                        logging.info(f"[PumpFun] POOL/TOKEN CREATION DETECTED! Total found: {len(created_tokens) + 1}")
                        logging.info(f"[PumpFun] Fetching full transaction...")
                        
                        # Extract token address from accounts
                        token_address = self._extract_pumpfun_token(tx)
                        if not token_address:
                            continue
                        
                        # Check if bonding curve has been created
                        bonding_curve = self._get_bonding_curve(token_address)
                        if not bonding_curve:
                            logging.warning(f"[PumpFun] Token {token_address} detected but NO bonding curve found - SKIPPING")
                            continue
                        
                        # For ultra-fresh tokens, accept any bonding curve
                        # Check token age first
                        token_age = await self.get_token_age(token_address)
                        sol_amount = bonding_curve.get('virtualSolReserves', 0) / 1e9
                        
                        if token_age and token_age <= 30:
                            # Ultra-fresh token - accept ANY bonding curve
                            logging.info(f"[PumpFun] Ultra-fresh token {token_address} ({token_age}s old) - accepting bonding curve with {sol_amount:.6f} SOL")
                        elif sol_amount < 0.001:
                            # Older token needs SOL in bonding curve
                            logging.warning(f"[PumpFun] Token {token_address} detected but insufficient SOL in bonding curve ({sol_amount:.6f} SOL) - SKIPPING")
                            continue
                        
                        # Mark as processed
                        self.processed_signatures.add(signature)

                        if token_address not in pumpfun_tokens:
                            pumpfun_tokens[token_address] = {
                                "discovered": time.time(),
                                "verified": True,
                                "migrated": False
                            }
                            logging.info(f"[PumpFun] Registered token {token_address[:8]}... in global dict")
                        
                        # Get token details
                        if token_address not in pumpfun_tokens:
                            pumpfun_tokens[token_address] = {
                                "discovered": time.time(),
                                "verified": True,
                                "migrated": False
                            }
                            logging.info(f"[PumpFun] Registered token {token_address[:8]}... in global dict")
                        token_info = {
                            'address': token_address,
                            'source': 'pumpfun',
                            'timestamp': sig_info.get('blockTime', time.time()),
                            'signature': signature,
                            'bonding_curve': bonding_curve
                        }
                        
                        created_tokens.append(token_info)
                        logging.info(f"[PumpFun] Found new token: {token_address}")
                        
                        # CRITICAL FIX: Actually buy the token here instead of deferring
                        if await self.should_buy_token(token_address):
                            logging.info(f"[PumpFun] Token {token_address} meets buy criteria - BUYING NOW!")
                            
                            # Check if it's already bought to avoid duplicates
                            if token_address in already_bought:
                                logging.info(f"[PumpFun] Token {token_address} already bought, skipping")
                                continue
                            
                            # Determine buy amount based on token state
                            graduated = await check_pumpfun_graduation(token_address)
                            buy_amount = PUMPFUN_MIGRATION_BUY if graduated else PUMPFUN_EARLY_BUY
                            buy_reason = "PumpFun Graduate" if graduated else "Fresh PumpFun"
                            
                            # Mark as attempting buy
                            recent_buy_attempts[token_address] = time.time()
                            
                            # Send alert
                            if should_send_telegram(f"pumpfun_detect_{token_address}"):
                                await send_telegram_alert(
                                    f"🎯 VERIFIED PUMPFUN TOKEN\n\n"
                                    f"Token: `{token_address}`\n"
                                    f"Status: {buy_reason}\n"
                                    f"Age: {token_age}s\n"
                                    f"Bonding Curve: {sol_amount:.6f} SOL\n"
                                    f"Buy Amount: {buy_amount} SOL\n\n"
                                    f"Attempting snipe..."
                                )
                            
                            # CRITICAL: Execute the buy with is_pumpfun=True
                            try:
                                success = await buy_token(
                                    token_address, 
                                    amount=buy_amount, 
                                    is_pumpfun=True,  # THIS IS CRITICAL!
                                    is_migration=graduated
                                )
                                
                                if success:
                                    already_bought.add(token_address)
                                    if BLACKLIST_AFTER_BUY:
                                        BLACKLIST.add(token_address)
                                    
                                    if should_send_telegram(f"pumpfun_success_{token_address}"):
                                        await send_telegram_alert(
                                            f"✅ PUMPFUN SNIPE SUCCESS!\n"
                                            f"Token: {token_address[:16]}...\n"
                                            f"Amount: {buy_amount} SOL\n"
                                            f"Type: {buy_reason}"
                                        )
                                    asyncio.create_task(wait_and_auto_sell(token_address))
                                else:
                                    if should_send_telegram(f"pumpfun_fail_{token_address}"):
                                        await send_telegram_alert(
                                            f"❌ PumpFun snipe failed\n"
                                            f"Token: {token_address[:16]}..."
                                        )
                                    mark_broken_token(token_address, 0)
                            except Exception as e:
                                logging.error(f"[PUMPFUN] Buy error: {e}")
                                if should_send_telegram(f"pumpfun_error_{token_address}"):
                                    await send_telegram_alert(f"❌ PumpFun buy error: {str(e)[:100]}")
                            
                    except Exception as e:
                        logging.error(f"[PumpFun] Error processing tx {signature}: {e}")
                        continue
            
            logging.info(f"[PumpFun] Processed {processed_count} txs, found {len(created_tokens)} pool creations")
            return created_tokens
            
        except Exception as e:
            logging.error(f"[PumpFun] Error in transaction processing: {e}")
            return []

# Create global instance
sniper_bot = SniperBot()

# ============================================
# NEW: Background PumpFun transaction scanner task
# ============================================
async def pumpfun_tx_scanner_task():
    """Background task to continuously scan PumpFun transactions"""
    logging.info("[PumpFun Scanner] Starting background transaction scanner...")
    
    if should_send_telegram("pumpfun_scanner_start"):
        await send_telegram_alert("🔍 PumpFun Transaction Scanner ACTIVE - Scanning every 5 seconds")
    
    while True:
        try:
            if not is_bot_running():
                await asyncio.sleep(5)
                continue
            
            # Process recent transactions
            await sniper_bot.process_pumpfun_transactions(max_signatures=100)
            
            # Wait before next scan
            await asyncio.sleep(5)
            
        except Exception as e:
            logging.error(f"[PumpFun Scanner] Error in scanner loop: {e}")
            await asyncio.sleep(10)

# ============================================
# CRITICAL FIX: ULTRA-FRESH PUMPFUN TOKEN VERIFICATION
# ============================================
async def is_pumpfun_token(mint: str) -> bool:
    """Verify if a token is actually created by PumpFun with an active and tradeable bonding curve
    
    CRITICAL FIX: For ultra-fresh tokens (≤30s old), accept ANY bonding curve even with 0 SOL
    since PumpFun tokens start with 0 SOL and get funded through initial transactions.
    """
    try:
        mint_pubkey = Pubkey.from_string(mint)
        
        # Check if bonding curve exists (MOST RELIABLE METHOD)
        bonding_curve_seed = b"bonding-curve"
        pumpfun_program = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
        bonding_curve, _ = Pubkey.find_program_address(
            [bonding_curve_seed, bytes(mint_pubkey)],
            pumpfun_program
        )
        
        bc_info = rpc.get_account_info(bonding_curve)
        if bc_info and bc_info.value:
            # CRITICAL FIX: Check token age FIRST before checking SOL amount
            try:
                is_ultra_fresh = await is_fresh_token(mint, max_age_seconds=30)
            except Exception as e:
                logging.debug(f"[PumpFun Verify] Could not check freshness: {e}")
                is_ultra_fresh = False
            
            lamports = bc_info.value.lamports
            
            if is_ultra_fresh:
                # ULTRA-FRESH TOKENS: Accept ANY bonding curve, even with 0 SOL
                # Brand new PumpFun tokens start with 0 SOL and get funded through first transactions
                logging.info(f"[PumpFun Verify] {mint[:8]}... ULTRA-FRESH (<30s) with bonding curve ({lamports/1e9:.6f} SOL) - ACCEPTING!")
                return True
            else:
                # OLDER TOKENS: Require minimum SOL to ensure it's actually tradeable
                min_lamports = 1000000  # Minimum 0.001 SOL for non-fresh tokens
                
                if lamports >= min_lamports:
                    logging.info(f"[PumpFun Verify] {mint[:8]}... has active bonding curve with {lamports/1e9:.4f} SOL!")
                    return True
                else:
                    logging.warning(f"[PumpFun Verify] {mint[:8]}... has bonding curve but insufficient SOL ({lamports/1e9:.6f} SOL) for non-fresh token")
                    return False
        else:
            logging.debug(f"[PumpFun Verify] {mint[:8]}... NO bonding curve found")
            
        # Method 2: Check if mint authority is PumpFun program (fallback)
        mint_info = rpc.get_account_info(mint_pubkey)
        if mint_info and mint_info.value:
            mint_authority = mint_info.value.owner
            if str(mint_authority) == "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P":
                logging.debug(f"[PumpFun Verify] {mint[:8]}... has PumpFun mint authority")
                # Double-check bonding curve exists for fresh tokens
                if await is_fresh_token(mint, max_age_seconds=60):
                    return True
        
        # Method 3: Check if token was tracked as PumpFun (fallback)
        if mint in pumpfun_tokens and pumpfun_tokens[mint].get("verified", False):
            # Re-verify it still has bonding curve
            bc_info = rpc.get_account_info(bonding_curve)
            if bc_info and bc_info.value:
                # For tracked tokens, be lenient with SOL requirements if fresh
                if bc_info.value.lamports > 0 or await is_fresh_token(mint, max_age_seconds=60):
                    return True
            # Remove from verified if no longer tradeable
            if mint in pumpfun_tokens:
                pumpfun_tokens[mint]["verified"] = False
            return False
            
    except Exception as e:
        logging.debug(f"PumpFun verification error for {mint[:8]}...: {e}")
    
    return False

# ============================================
# HELPER FUNCTIONS FOR FRESHNESS TIERS
# ============================================
async def freshness_tier(mint: str) -> str:
    """Check token freshness tier: ultra (≤30s), fresh (≤60s), or old"""
    try:
        if await is_fresh_token(mint, max_age_seconds=30):
            return "ultra"
        elif await is_fresh_token(mint, max_age_seconds=60):
            return "fresh"
        else:
            return "old"
    except Exception:
        return "old"

def min_lp_for_tier(base_threshold: float, tier: str) -> float:
    """Get minimum LP requirement based on freshness tier"""
    if tier == "ultra":
        return 0.0  # Buy path validates
    elif tier == "fresh":
        return min(base_threshold, 0.10)  # Fresh tokens get 0.10 SOL floor
    else:
        return base_threshold

async def is_ultra_fresh(mint: str) -> bool:
    """Check if token is ultra-fresh (≤30s old)"""
    try:
        return await is_fresh_token(mint, max_age_seconds=30)
    except Exception:
        return False

def grace_min_lp(base_threshold: float, is_ultra_fresh: bool) -> float:
    """Apply grace floor for ultra-fresh tokens (legacy compatibility)"""
    return min(base_threshold, 0.1) if is_ultra_fresh else base_threshold

def should_send_telegram(key: str) -> bool:
    """Check if we should send a telegram message (anti-spam)"""
    now = time.time()
    if key in telegram_message_cache:
        last_sent = telegram_message_cache[key]
        if (now - last_sent) < TELEGRAM_COOLDOWN:
            return False
    telegram_message_cache[key] = now
    return True

async def fetch_transaction_accounts(signature: str, rpc_url: str = None, retry_count: int = 0) -> list:
    """Fetch transaction details with loop prevention and caching"""
    global last_cache_cleanup
    
    if retry_count > MAX_FETCH_RETRIES:
        logging.warning(f"[TX FETCH] Max retries reached for {signature[:8]}...")
        return []
    
    # FIX 1: Check cache but don't add yet
    if signature in processed_signatures_cache:
        logging.debug(f"[TX FETCH] Already processed {signature[:8]}...")
        return []
    
    current_time = time.time()
    if current_time - last_cache_cleanup > CACHE_CLEANUP_INTERVAL:
        old_sigs = [sig for sig, ts in processed_signatures_cache.items() 
                   if current_time - ts > CACHE_CLEANUP_INTERVAL]
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
                                                            # FIX 2: Accept 43-44 length keys
                                                            if isinstance(val, str) and 43 <= len(val) <= 44:
                                                                account_keys.append(val)
                                                
                                                if "accounts" in inst:
                                                    for acc in inst["accounts"]:
                                                        # FIX 2: Accept 43-44 length keys
                                                        if isinstance(acc, str) and 43 <= len(acc) <= 44:
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
                                                            # FIX 2: Accept 43-44 length keys
                                                            if isinstance(val, str) and 43 <= len(val) <= 44:
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
                                # FIX 2: Accept 43-44 length keys
                                if key and key not in seen and 43 <= len(key) <= 44 and key not in SYSTEM_PROGRAMS:
                                    try:
                                        Pubkey.from_string(key)
                                        seen.add(key)
                                        unique_keys.append(key)
                                    except:
                                        pass
                            
                            if unique_keys:
                                logging.info(f"[TX FETCH] Got {len(unique_keys)} accounts for {signature[:8]}...")
                                # FIX 1: Only cache on success with non-empty results
                                processed_signatures_cache[signature] = time.time()
                                return unique_keys
                            
                            continue
                            
                # FIX 3: Catch both timeout types
                except (asyncio.TimeoutError, httpx.TimeoutException):
                    logging.warning(f"[TX FETCH] Timeout for {encoding} encoding")
                    continue
                except Exception as e:
                    logging.debug(f"[TX FETCH] {encoding} encoding failed: {e}")
                    continue
            
            logging.debug(f"[TX FETCH] All encodings failed, trying fallback for {signature[:8]}...")
            return await fetch_pumpfun_token_from_logs(signature, rpc_url, retry_count + 1)
        
    # FIX 3: Catch both timeout types
    except (asyncio.TimeoutError, httpx.TimeoutException, Exception) as e:
        if isinstance(e, (asyncio.TimeoutError, httpx.TimeoutException)):
            logging.error(f"[TX FETCH] Overall timeout for {signature[:8]}...")
        else:
            logging.error(f"[TX FETCH] Error fetching transaction {signature[:8]}...: {e}")
        return await fetch_pumpfun_token_from_logs(signature, rpc_url, retry_count + 1)

async def fetch_pumpfun_token_from_logs(signature: str, rpc_url: str = None, retry_count: int = 0) -> list:
    """Fallback method with loop prevention"""
    if retry_count > MAX_FETCH_RETRIES:
        logging.warning(f"[FALLBACK] Max retries reached for {signature[:8]}...")
        return []
    
    # Check cache but don't add yet
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
                                        
                                        # FIX 2: Accept 43-44 length keys
                                        if 43 <= len(b58_key) <= 44:
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
                                    # FIX 2: Accept 43-44 length keys
                                    if match not in SYSTEM_PROGRAMS and 43 <= len(match) <= 44:
                                        try:
                                            Pubkey.from_string(match)
                                            if match not in potential_mints:
                                                potential_mints.append(match)
                                        except:
                                            pass
                    
                    unique_mints = list(dict.fromkeys(potential_mints))
                    
                    if unique_mints:
                        logging.info(f"[FALLBACK] Found {len(unique_mints)} potential mints from logs/raw data")
                        # FIX 1: Only cache on success with non-empty results
                        processed_signatures_cache[signature] = time.time()
                        return unique_mints[:5]
        
        return []
        
    # FIX 3: Catch both timeout types
    except (asyncio.TimeoutError, httpx.TimeoutException):
        logging.error(f"[FALLBACK] Timeout for {signature[:8]}...")
        return []
    except Exception as e:
        logging.debug(f"[FALLBACK] Error: {e}")
        return []

async def is_quality_token(mint: str, lp_amount: float) -> tuple:
    """Enhanced quality check for tokens with freshness tiers"""
    try:
        if mint in already_bought:
            return False, "Already bought"
        
        if mint in recent_buy_attempts:
            time_since_attempt = time.time() - recent_buy_attempts[mint]
            if time_since_attempt < DUPLICATE_CHECK_WINDOW:
                return False, f"Recent buy attempt {time_since_attempt:.0f}s ago"
        
        # Check freshness tier
        tier = await freshness_tier(mint)
        eff_min = min_lp_for_tier(RUG_LP_THRESHOLD, tier)
        
        # Ultra-fresh with LP=0: defer to buy path
        if tier == "ultra" and lp_amount == 0:
            return True, "Ultra-fresh; defer LP check to buy path"
        
        # Fresh tier (30-60s): allow reduced floor
        if tier == "fresh" and lp_amount >= 0.10:
            logging.info(f"Fresh token (30-60s) with {lp_amount:.2f} SOL LP - proceeding")
            return True, "Fresh token with acceptable LP"
        
        if lp_amount < eff_min:
            return False, f"Low liquidity: {lp_amount:.2f} SOL (min: {eff_min:.2f} for {tier} tier)"
        
        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
            response = await HTTPManager.request(url, timeout=5)
            if response:
                data = response.json()
                if "pairs" in data and len(data["pairs"]) > 0:
                    pair = data["pairs"][0]
                    
                    volume_h24 = float(pair.get("volume", {}).get("h24", 0))
                    if volume_h24 > 0:
                        logging.info(f"Volume 24h: ${volume_h24:.0f}")
                    
                    txns = pair.get("txns", {})
                    buys_h1 = txns.get("h1", {}).get("buys", 1)
                    sells_h1 = txns.get("h1", {}).get("sells", 1)
                    
                    if sells_h1 > 0 and buys_h1 / sells_h1 < 0.5:
                        return False, f"Bad buy/sell ratio: {buys_h1}/{sells_h1}"
                    
                    return True, "Quality token"
        except:
            pass
        
        if lp_amount >= eff_min:
            return True, f"Good liquidity ({lp_amount:.1f} SOL), proceeding without data"
        
        return False, "Failed quality checks"
        
    except Exception as e:
        logging.error(f"Quality check error: {e}")
        # Check tier even in error cases
        tier = await freshness_tier(mint)
        eff_min = min_lp_for_tier(RUG_LP_THRESHOLD, tier)
        
        if lp_amount >= eff_min:
            return True, "Quality check error but good LP"
        return False, "Quality check error"

async def verify_pool_exists(mint: str) -> bool:
    """Verify that a real trading pool exists for this token"""
    try:
        if mint in pool_verification_cache:
            return pool_verification_cache[mint]
        
        if mint in detected_pools:
            pool_verification_cache[mint] = True
            return True
        
        pool = raydium.find_pool_realtime(mint)
        if pool:
            pool_verification_cache[mint] = True
            return True
        
        try:
            url = f"https://price.jup.ag/v4/price?ids={mint}"
            response = await HTTPManager.request(url, timeout=5)
            if response:
                data = response.json()
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
        response = await HTTPManager.request(url, timeout=5)
        if response:
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
        
    if should_send_telegram("graduation_scanner_start"):
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
                            
                            if should_send_telegram(f"graduation_{mint}"):
                                await send_telegram_alert(
                                    f"🎓 GRADUATION DETECTED!\n\n"
                                    f"Token: `{mint}`\n"
                                    f"Liquidity: {lp_amount:.2f} SOL\n"
                                    f"Action: BUYING NOW!"
                                )
                            
                            success = await buy_token(mint, amount=PUMPFUN_MIGRATION_BUY)
                            
                            if success:
                                if should_send_telegram(f"graduation_success_{mint}"):
                                    await send_telegram_alert(
                                        f"✅ GRADUATION SNIPE SUCCESS!\n"
                                        f"Token: {mint[:16]}...\n"
                                        f"Amount: {PUMPFUN_MIGRATION_BUY} SOL"
                                    )
                                asyncio.create_task(wait_and_auto_sell(mint))
                            else:
                                if should_send_telegram(f"graduation_fail_{mint}"):
                                    await send_telegram_alert(f"❌ Graduation snipe failed for {mint[:16]}...")
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
        
    if should_send_telegram("migration_monitor_start"):
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
                        
                        if should_send_telegram(f"migration_{mint}"):
                            await send_telegram_alert(
                                f"🚨 PUMPFUN MIGRATION DETECTED 🚨\n\n"
                                f"Token: `{mint}`\n"
                                f"Status: Graduated to Raydium!\n"
                                f"Liquidity: {lp_data.get('liquidity', 0):.2f} SOL\n"
                                f"Action: SNIPING NOW!"
                            )
                        
                        success = await buy_token(mint, amount=PUMPFUN_MIGRATION_BUY)
                        
                        if success:
                            if should_send_telegram(f"migration_success_{mint}"):
                                await send_telegram_alert(
                                    f"✅ MIGRATION SNIPE SUCCESS!\n"
                                    f"Token: {mint[:16]}...\n"
                                    f"Amount: {PUMPFUN_MIGRATION_BUY} SOL\n"
                                    f"Type: PumpFun → Raydium Migration"
                                )
                            asyncio.create_task(wait_and_auto_sell(mint))
                        
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
        
        response = await HTTPManager.request(url)
        if response:
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
                            "market_cap": market_cap,
                            "verified": True  # Mark as verified since it's from PumpFun API
                        }
                    
                    if mint not in migration_watch_list:
                        migration_watch_list.add(mint)
                        logging.info(f"[PumpFun] Added {mint[:8]}... to migration watch (MC: ${market_cap:.0f})")
                        
                        if market_cap > PUMPFUN_GRADUATION_MC * 0.95:
                            if should_send_telegram(f"graduation_imminent_{mint}"):
                                await send_telegram_alert(
                                    f"⚠️ GRADUATION IMMINENT\n\n"
                                    f"Token: `{mint}`\n"
                                    f"Market Cap: ${market_cap:,.0f}\n"
                                    f"Graduation at: $69,420\n"
                                    f"Status: {(market_cap/PUMPFUN_GRADUATION_MC)*100:.1f}% complete\n\n"
                                    "Monitoring for Raydium migration..."
                                )
    except Exception as e:
        logging.error(f"[PumpFun Scan] Error: {e}")

async def mempool_listener(name, program_id=None):
    """Enhanced mempool listener with EARLY PUMPFUN VERIFICATION"""
    
    listener_id = f"{name}_{program_id or 'default'}"
    
    if listener_id in ACTIVE_LISTENERS:
        if listener_id in LISTENER_TASKS and not LISTENER_TASKS[listener_id].done():
            logging.warning(f"[{name}] Listener already running, skipping duplicate")
            return
        else:
            logging.info(f"[{name}] Cleaning up dead listener task")
            ACTIVE_LISTENERS.discard(listener_id)
            if listener_id in LISTENER_TASKS:
                del LISTENER_TASKS[listener_id]
    
    ACTIVE_LISTENERS.add(listener_id)
    LISTENER_TASKS[listener_id] = asyncio.current_task()
    
    try:
        if not HELIUS_API:
            logging.warning(f"[{name}] HELIUS_API not set, skipping mempool listener")
            if should_send_telegram(f"{name}_no_api"):
                await send_telegram_alert(f"⚠️ {name} listener disabled (no Helius API key)")
            return
        
        if name == "Jupiter" and SKIP_JUPITER_MEMPOOL:
            logging.info(f"[{name}] Mempool monitoring disabled via config")
            if should_send_telegram(f"{name}_disabled"):
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
                ws = await websockets.connect(
                    url, 
                    ping_interval=15,  # Relaxed from 20
                    ping_timeout=20,    # Relaxed from 10
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
                if listener_id not in last_listener_status_msg or \
                   current_time - last_listener_status_msg.get(listener_id, 0) > STATUS_MSG_COOLDOWN:
                    if should_send_telegram(f"{name}_listener_active"):
                        await send_telegram_alert(f"📱 {name} listener ACTIVE")
                    last_listener_status_msg[listener_id] = current_time
                
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
                            
                            # Get instruction data to check discriminators
                            instruction_data = None
                            if "transaction" in value:
                                tx = value.get("transaction", {})
                                if "message" in tx:
                                    msg_data = tx.get("message", {})
                                    if "instructions" in msg_data:
                                        for inst in msg_data["instructions"]:
                                            if "data" in inst:
                                                try:
                                                    inst_bytes = b58decode(inst["data"])
                                                    if len(inst_bytes) >= 8:
                                                        instruction_data = inst_bytes[:8]
                                                        break
                                                except:
                                                    pass
                            
                            if signature in processed_txs:
                                continue
                            processed_txs.add(signature)
                            
                            if len(processed_txs) > 1000:
                                processed_txs.clear()
                            
                            transaction_counter += 1
                            
                            if transaction_counter % 100 == 0:
                                logging.info(f"[{name}] Processed {transaction_counter} txs, found {pool_creations_found} pool creations")
                            
                            # Detection logic with discriminators
                            is_pool_creation = False
                            pool_id = None
                            
                            if name == "Raydium":
                                # Check for Raydium init discriminator FIRST
                                if instruction_data == RAYDIUM_INIT_DISCRIMINATOR:
                                    is_pool_creation = True
                                    logging.info(f"[RAYDIUM] Pool initialization detected via discriminator!")
                                else:
                                    # Fallback to enhanced log-based detection
                                    raydium_indicators = 0
                                    for log in logs:
                                        log_lower = log.lower()
                                        if "program log: instruction: initialize" in log_lower:
                                            raydium_indicators += 3
                                        if "initialize2" in log_lower:
                                            raydium_indicators += 3
                                        if "init_pc_amount" in log_lower or "init_coin_amount" in log_lower:
                                            raydium_indicators += 2
                                    
                                    # Only trigger if we have strong indicators AND enough logs
                                    if raydium_indicators >= RAYDIUM_MIN_INDICATORS and len(logs) >= RAYDIUM_MIN_LOGS:
                                        is_pool_creation = True
                                        logging.info(f"[RAYDIUM] Pool creation detected via logs (score: {raydium_indicators}, logs: {len(logs)})")
                            
                            elif name == "PumpFun":
                                # Check for PumpFun create discriminator FIRST
                                if instruction_data == PUMPFUN_CREATE_DISCRIMINATOR:
                                    is_pool_creation = True
                                    logging.info(f"[PUMPFUN] Token creation detected via discriminator!")
                                    
                                    token_mint = self._extract_pumpfun_token(tx) if hasattr(self, '_extract_pumpfun_token') else None
                                    if not token_mint:
                                        for key in account_keys:
                                            if isinstance(key, dict):
                                                key = key.get("pubkey", "") or key.get("address", "")
                                            if key and len(key) >= 43 and key not in SYSTEM_PROGRAMS:
                                                if key != "So11111111111111111111111111111111111111112":
                                                    token_mint = key
                                                    break
                                    if token_mint and token_mint not in pumpfun_tokens:
                                        pumpfun_tokens[token_mint] = {
                                            "discovered": time.time(),
                                            "verified": True,
                                            "migrated": False
                                        }
                                        logging.info(f"[PUMPFUN] Pre-registered token {token_mint[:8]}... via discriminator")
                                else:
                                    # Fallback to log-based detection with stricter requirements
                                    pumpfun_create_indicators = 0
                                    for log in logs:
                                        log_lower = log.lower()
                                        if "create" in log_lower and ("token" in log_lower or "coin" in log_lower):
                                            pumpfun_create_indicators += 3
                                        if "launch" in log_lower:
                                            pumpfun_create_indicators += 3
                                        if "bonding" in log_lower and ("init" in log_lower or "create" in log_lower):
                                            pumpfun_create_indicators += 4
                                    
                                    if pumpfun_create_indicators >= PUMPFUN_MIN_INDICATORS:
                                        is_pool_creation = True
                                        logging.info(f"[PUMPFUN] Token creation detected via logs (score: {pumpfun_create_indicators})")
                            
                            elif name == "Moonshot":
                                for log in logs:
                                    log_lower = log.lower()
                                    if ("moon" in log_lower or "launch" in log_lower) and ("create" in log_lower or "initialize" in log_lower):
                                        if len(logs) >= 5:
                                            is_pool_creation = True
                                            break
                            
                            elif name == "Jupiter":
                                continue  # Skip Jupiter entirely
                            
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
                            
                            # FIXED: Extract the actual token mint, not the pool ID
                            token_mint = None
                            
                            if name == "Raydium":
                                # For Raydium, find the pool ID and token mint separately
                                for i, key in enumerate(account_keys):
                                    if isinstance(key, dict):
                                        key = key.get("pubkey", "") or key.get("address", "")
                                    
                                    # FIX 2: Accept 43-44 length keys
                                    if key and 43 <= len(key) <= 44 and key not in SYSTEM_PROGRAMS:
                                        # First non-system account is often the pool
                                        if pool_id is None:
                                            pool_id = key
                                        # Look for token mint (not SOL, not pool)
                                        elif key != "So11111111111111111111111111111111111111112" and key != pool_id:
                                            token_mint = key
                                            break
                            else:
                                # For other platforms, first valid non-system account is usually the token
                                for key in account_keys:
                                    if isinstance(key, dict):
                                        key = key.get("pubkey", "") or key.get("address", "")
                                    
                                    # FIX 2: Accept 43-44 length keys
                                    if key and 43 <= len(key) <= 44 and key not in SYSTEM_PROGRAMS:
                                        if key != "So11111111111111111111111111111111111111112":
                                            token_mint = key
                                            break
                            
                            if not token_mint:
                                logging.warning(f"[{name}] Could not extract token mint from transaction")
                                continue

                            if name == "PumpFun" and token_mint not in pumpfun_tokens:
                                pumpfun_tokens[token_mint] = {
                                    "discovered": time.time(),
                                    "verified": True,
                                    "migrated": False
                                }
                                logging.info(f"[PumpFun] PRE-REGISTERED token {token_mint[:8]}... in global dict")
                            
                            # Validate it's a proper mint
                            try:
                                Pubkey.from_string(token_mint)
                            except:
                                continue
                            
                            if token_mint in seen_tokens or token_mint in already_bought:
                                continue
                            
                            seen_tokens.add(token_mint)
                            
                            # ============================================
                            # CRITICAL FIX: EARLY PUMPFUN VERIFICATION WITH ULTRA-FRESH SUPPORT
                            # ============================================
                            if name == "PumpFun":
                                # Skip if already processed
                                if token_mint in already_bought:
                                    continue
                                
                                # First check if token is actually ultra-fresh (≤30s old)
                                is_ultra_fresh = False
                                token_age_seconds = None
                                
                                try:
                                    # Use is_fresh_token to check if under 30 seconds
                                    is_under_30 = await is_fresh_token(token_mint, 30)
                                    
                                    if is_under_30:
                                        # Token is definitely under 30s - ultra-fresh!
                                        is_ultra_fresh = True
                                        token_age_seconds = 15  # Estimate for logging
                                        logging.info(f"[PumpFun] Token {token_mint[:8]}... confirmed <30s old - ULTRA-FRESH!")
                                    else:
                                        # Not ultra-fresh, check if at least under 60s
                                        is_under_60 = await is_fresh_token(token_mint, 60)
                                        if is_under_60:
                                            # Between 30-60 seconds
                                            is_ultra_fresh = False
                                            token_age_seconds = 45  # Estimate
                                            logging.info(f"[PumpFun] Token {token_mint[:8]}... is 30-60s old - fresh but not ultra")
                                        else:
                                            # Older than 60s - definitely too old
                                            logging.info(f"[PumpFun] Token {token_mint[:8]}... is >60s old - TOO OLD, skipping")
                                            record_skip("old_token")
                                            continue  # SKIP OLD TOKENS IMMEDIATELY
                                            
                                except Exception as e:
                                    logging.debug(f"[PumpFun] Could not check age for {token_mint[:8]}...: {e}")
                                    # If we can't verify age, assume it's old and skip
                                    logging.warning(f"[PumpFun] Cannot verify age for {token_mint[:8]}... - SKIPPING")
                                    record_skip("age_check_failed")
                                    continue
                                
                                # Check bonding curve for all fresh tokens (under 60s)
                                has_bonding_curve = False
                                sol_amount_in_curve = 0
                                
                                try:
                                    mint_pubkey = Pubkey.from_string(token_mint)
                                    bonding_curve_seed = b"bonding-curve"
                                    pumpfun_program = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
                                    bonding_curve, _ = Pubkey.find_program_address(
                                        [bonding_curve_seed, bytes(mint_pubkey)],
                                        pumpfun_program
                                    )
                                    
                                    bc_info = rpc.get_account_info(bonding_curve)
                                    if bc_info and bc_info.value:
                                        has_bonding_curve = True
                                        sol_amount_in_curve = bc_info.value.lamports / 1e9
                                        logging.info(f"[PumpFun] Token {token_mint[:8]}... has bonding curve with {sol_amount_in_curve:.6f} SOL")
                                    else:
                                        logging.warning(f"[PumpFun] Token {token_mint[:8]}... NO bonding curve found - SKIPPING")
                                        record_skip("no_bonding_curve")
                                        continue
                                except Exception as e:
                                    logging.error(f"[PumpFun] Error checking bonding curve: {e}")
                                    record_skip("bonding_curve_error")
                                    continue
                                
                                # Determine if we should buy based on freshness and bonding curve
                                should_buy = False
                                buy_reason = ""
                                
                                if is_ultra_fresh and has_bonding_curve:
                                    # Ultra-fresh with ANY bonding curve = BUY
                                    should_buy = True
                                    buy_reason = "Ultra-fresh PumpFun"
                                    logging.info(f"[PumpFun] ✅ ULTRA-FRESH token {token_mint[:8]}... (<30s) with bonding curve - WILL BUY!")
                                elif not is_ultra_fresh and has_bonding_curve and sol_amount_in_curve >= 0.001:
                                    # 30-60s old needs SOL in bonding curve
                                    should_buy = True
                                    buy_reason = "Fresh PumpFun with SOL"
                                    logging.info(f"[PumpFun] ✅ Fresh token {token_mint[:8]}... (30-60s) with {sol_amount_in_curve:.6f} SOL - WILL BUY!")
                                else:
                                    # Don't buy
                                    logging.warning(f"[PumpFun] Token {token_mint[:8]}... doesn't meet buy criteria - SKIPPING")
                                    record_skip("invalid_pumpfun")
                                    continue
                                if token_mint not in pumpfun_tokens:
                                    pumpfun_tokens[token_mint] = {
                                        "discovered": time.time(),
                                        "verified": True,
                                        "migrated": False,
                                        "ultra_fresh": is_ultra_fresh,
                                        "bonding_curve_sol": sol_amount_in_curve
                                    }
                                    logging.info(f"[PumpFun] Registered verified token: {token_mint[:8]}...")
                                # Track the token
                                else:
                                    pumpfun_tokens[token_mint].update({
                                        "tradeable": True,
                                        "ultra_fresh": is_ultra_fresh,
                                        "bonding_curve_sol": sol_amount_in_curve
                                    })
                                        
                                # Now proceed with the buy
                                if should_buy:
                                    # Check if graduated
                                    graduated = await check_pumpfun_graduation(token_mint)
                                    if graduated and token_mint in pumpfun_tokens:
                                        pumpfun_tokens[token_mint]["migrated"] = True
                                    
                                    # Determine buy amount
                                    if graduated:
                                        buy_amount = PUMPFUN_MIGRATION_BUY
                                        buy_reason = "PumpFun Graduate"
                                    else:
                                        buy_amount = PUMPFUN_EARLY_BUY
                                    
                                    # Mark as attempting buy
                                    recent_buy_attempts[token_mint] = time.time()
                                    
                                    # Send alert
                                    if should_send_telegram(f"pumpfun_detect_{token_mint}"):
                                        await send_telegram_alert(
                                            f"🎯 VERIFIED PUMPFUN TOKEN\n\n"
                                            f"Token: `{token_mint}`\n"
                                            f"Status: {buy_reason}\n"
                                            f"Age: {'<30s' if is_ultra_fresh else '30-60s'}\n"
                                            f"Bonding Curve: {sol_amount_in_curve:.6f} SOL\n"
                                            f"Buy Amount: {buy_amount} SOL\n\n"
                                            f"Attempting snipe..."
                                        )
                                    
                                    # CRITICAL: Pass is_pumpfun=True to buy_token
                                    try:
                                        success = await buy_token(token_mint, amount=buy_amount, is_pumpfun=True, is_migration=graduated)
                                        
                                        if success:
                                            already_bought.add(token_mint)
                                            if BLACKLIST_AFTER_BUY:
                                                BLACKLIST.add(token_mint)
                                            
                                            if should_send_telegram(f"pumpfun_success_{token_mint}"):
                                                await send_telegram_alert(
                                                    f"✅ PUMPFUN SNIPE SUCCESS!\n"
                                                    f"Token: {token_mint[:16]}...\n"
                                                    f"Amount: {buy_amount} SOL\n"
                                                    f"Type: {buy_reason}"
                                                )
                                            asyncio.create_task(wait_and_auto_sell(token_mint))
                                        else:
                                            if should_send_telegram(f"pumpfun_fail_{token_mint}"):
                                                await send_telegram_alert(
                                                    f"❌ PumpFun snipe failed\n"
                                                    f"Token: {token_mint[:16]}..."
                                                )
                                            mark_broken_token(token_mint, 0)
                                    except Exception as e:
                                        logging.error(f"[PUMPFUN] Buy error: {e}")
                                        if should_send_telegram(f"pumpfun_error_{token_mint}"):
                                            await send_telegram_alert(f"❌ PumpFun buy error: {str(e)[:100]}")
                                
                                # End of PumpFun processing - skip to next token
                                continue
                            
                            if name == "Raydium" and pool_id and token_mint:
                                detected_pools[token_mint] = pool_id
                                raydium.register_new_pool(pool_id, token_mint)
                                logging.info(f"[Raydium] Registered pool {pool_id[:8]}... for token {token_mint[:8]}...")
                            
                            # ============================================
                            # CRITICAL AGE ENFORCEMENT
                            # ============================================
                            if is_bot_running():
                                if token_mint not in BROKEN_TOKENS and token_mint not in BLACKLIST:
                                    if token_mint in already_bought:
                                        continue
                                    
                                    # PumpFun tokens are handled in their own section above
                                    if name == "PumpFun":
                                        continue  # Already handled
                                    
                                    # STRICT AGE CHECK - USE ENV VARIABLE
                                    if STRICT_AGE_CHECK:
                                        is_fresh = await is_fresh_token(token_mint, MAX_TOKEN_AGE_SECONDS)
                                        if not is_fresh:
                                            logging.info(f"[{name}] REJECTED: Token {token_mint[:8]}... older than {MAX_TOKEN_AGE_SECONDS}s")
                                            record_skip("old_token")
                                            continue
                                        
                                        # Optional: Double-check on blockchain
                                        is_fresh_chain = await verify_token_age_on_chain(token_mint, MAX_TOKEN_AGE_SECONDS)
                                        if not is_fresh_chain:
                                            logging.info(f"[{name}] REJECTED: Blockchain age check failed for {token_mint[:8]}...")
                                            record_skip("old_token")
                                            continue
                                    
                                    logging.info(f"[{name}] Token {token_mint[:8]}... PASSED age check (<{MAX_TOKEN_AGE_SECONDS}s)")
                                    
                                    # Continue with buying logic for FRESH tokens only
                                    if name in ["Raydium"]:
                                        await asyncio.sleep(MEMPOOL_DELAY_MS / 1000)
                                        
                                        lp_amount = 0
                                        try:
                                            lp_check_task = asyncio.create_task(get_liquidity_and_ownership(token_mint))
                                            lp_data = await asyncio.wait_for(lp_check_task, timeout=5.0)
                                            
                                            if lp_data:
                                                lp_amount = lp_data.get("liquidity", 0)
                                        except asyncio.TimeoutError:
                                            logging.info(f"[{name}] LP check timeout, assuming minimal liquidity")
                                            lp_amount = 0.1  # FIXED: Assume minimal
                                        except Exception as e:
                                            logging.debug(f"[{name}] LP check error: {e}")
                                            lp_amount = 0.1
                                        
                                        # Check freshness tier and adjust min LP
                                        tier = await freshness_tier(token_mint)
                                        effective_min_lp = min_lp_for_tier(RUG_LP_THRESHOLD, tier)
                                        
                                        if lp_amount < effective_min_lp:
                                            # Check if it's actually a PumpFun token before using PumpFun buy
                                            is_pumpfun = await is_pumpfun_token(token_mint)
                                            is_migration = False  # Fix: Define is_migration
                                            ultra_fresh = await is_ultra_fresh(token_mint)
                                            if is_pumpfun and ultra_fresh:
                                                logging.info(f"[{name}] Verified PumpFun token - using PumpFun Direct")
                                                success = await buy_token(token_mint, amount=PUMPFUN_EARLY_BUY, is_pumpfun=True, is_migration=is_migration)
                                                if success:
                                                    already_bought.add(token_mint)
                                                    if BLACKLIST_AFTER_BUY:
                                                        BLACKLIST.add(token_mint)
                                                    asyncio.create_task(wait_and_auto_sell(token_mint))
                                                continue
                                            else:
                                                logging.info(f"[{name}] Not a PumpFun token or not fresh enough, skipping")
                                                log_skipped_token(token_mint, f"Low liquidity and not PumpFun: {lp_amount:.2f} SOL")
                                                record_skip("low_lp")
                                                continue
                                        
                                        is_quality, reason = await is_quality_token(token_mint, lp_amount)
                                        
                                        if not is_quality:
                                            logging.info(f"[{name}] Skipping {token_mint[:8]}... - {reason}")
                                            record_skip("quality_check")
                                            continue
                                        
                                        if lp_amount >= RUG_LP_THRESHOLD * 2:
                                            risk_level = "SAFE"
                                            buy_amount = SAFE_BUY_AMOUNT
                                        elif lp_amount >= effective_min_lp:
                                            risk_level = "MEDIUM"
                                            buy_amount = RISKY_BUY_AMOUNT
                                        else:
                                            risk_level = "HIGH"
                                            buy_amount = ULTRA_RISKY_BUY_AMOUNT
                                        
                                        recent_buy_attempts[token_mint] = time.time()
                                        
                                        if should_send_telegram(f"raydium_detect_{token_mint}"):
                                            await send_telegram_alert(
                                                f"✅ FRESH QUALITY TOKEN DETECTED ✅\n\n"
                                                f"Platform: {name}\n"
                                                f"Token: `{token_mint}`\n"
                                                f"Liquidity: {lp_amount:.2f} SOL\n"
                                                f"Risk: {risk_level}\n"
                                                f"Tier: {tier.upper()}\n"
                                                f"Buy Amount: {buy_amount} SOL\n\n"
                                                f"Attempting snipe..."
                                            )
                                        
                                        try:
                                            success = await buy_token(token_mint, amount=buy_amount)
                                            
                                            if success:
                                                already_bought.add(token_mint)
                                                if BLACKLIST_AFTER_BUY:
                                                    BLACKLIST.add(token_mint)
                                                
                                                if should_send_telegram(f"raydium_success_{token_mint}"):
                                                    await send_telegram_alert(
                                                        f"✅ SNIPED FRESH QUALITY TOKEN!\n"
                                                        f"Token: {token_mint[:16]}...\n"
                                                        f"Amount: {buy_amount} SOL\n"
                                                        f"Risk: {risk_level}"
                                                    )
                                                asyncio.create_task(wait_and_auto_sell(token_mint))
                                            else:
                                                if should_send_telegram(f"raydium_fail_{token_mint}"):
                                                    await send_telegram_alert(
                                                        f"❌ Snipe failed\n"
                                                        f"Token: {token_mint[:16]}..."
                                                    )
                                                mark_broken_token(token_mint, 0)
                                        except Exception as e:
                                            logging.error(f"[{name}] Buy error: {e}")
                                            if should_send_telegram(f"raydium_error_{token_mint}"):
                                                await send_telegram_alert(f"❌ Buy error: {str(e)[:100]}")
                    
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
                    if should_send_telegram(f"{name}_listener_failed"):
                        await send_telegram_alert(msg)
                    listener_status[name] = "FAILED"
                    break
                
                wait_time = min(retry_delay * (2 ** (retry_attempts - 1)), 300)
                logging.info(f"[{name}] Retrying in {wait_time}s (attempt {retry_attempts}/{max_retries})")
                await asyncio.sleep(wait_time)
    
    finally:
        ACTIVE_LISTENERS.discard(listener_id)
        if listener_id in LISTENER_TASKS:
            del LISTENER_TASKS[listener_id]
        if listener_id in last_listener_status_msg:
            del last_listener_status_msg[listener_id]

async def get_trending_pairs_dexscreener():
    """Fetch trending pairs from DexScreener"""
    url = "https://api.dexscreener.com/latest/dex/pairs/solana"
    
    for attempt in range(3):
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json",
                "Cache-Control": "no-cache"
            }
            response = await HTTPManager.request(url, headers=headers, timeout=30)
            if response:
                data = response.json()
                pairs = data.get("pairs", [])
                if pairs:
                    logging.info(f"[Trending] DexScreener returned {len(pairs)} pairs")
                return pairs
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
            headers = {
                "X-API-KEY": BIRDEYE_API_KEY,
                "accept": "application/json"
            }
            response = await HTTPManager.request(url, headers=headers, timeout=30)
            if response:
                data = response.json()
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
        except Exception as e:
            logging.error(f"Birdeye attempt {attempt + 1} failed: {e}")
            await asyncio.sleep(2)
    
    return None

async def trending_scanner():
    """Scan for quality trending tokens - DISABLED BY DEFAULT"""
    if SKIP_TRENDING_SCANNER:
        logging.info("[Trending Scanner] Disabled - focusing on fresh launches only")
        return
        
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
                
                # ALWAYS CHECK AGE FOR TRENDING TOKENS TOO
                if STRICT_AGE_CHECK:
                    is_fresh = await is_fresh_token(mint, MAX_TOKEN_AGE_SECONDS)
                    if not is_fresh:
                        logging.info(f"[Trending] Skipping old token {mint[:8]}...")
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
                    
                    # Get default buy amount from config
                    from utils import CONFIG
                    default_buy_amount = CONFIG.BUY_AMOUNT_SOL
                    
                    if should_send_telegram(f"trending_{mint}"):
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
                        buy_amount = PUMPFUN_MIGRATION_BUY if is_pumpfun_grad else default_buy_amount
                        
                        success = await buy_token(mint, amount=buy_amount)
                        if success:
                            already_bought.add(mint)
                            if BLACKLIST_AFTER_BUY:
                                BLACKLIST.add(mint)
                            asyncio.create_task(wait_and_auto_sell(mint))
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
        
        # Check freshness tier and use appropriate min LP
        tier = await freshness_tier(mint)
        min_lp = min_lp_for_tier(RUG_LP_THRESHOLD, tier)
        
        if mint in pumpfun_tokens and pumpfun_tokens[mint].get("migrated", False):
            min_lp = min_lp / 2
        
        if not data or data.get("liquidity", 0) < min_lp:
            logging.info(f"[RUG CHECK] {mint[:8]}... has {data.get('liquidity', 0):.2f} SOL (min: {min_lp:.2f} for {tier} tier)")
            return False
        return True
    except Exception as e:
        logging.error(f"Rug check error for {mint}: {e}")
        return False

def detect_chart_pattern(price_data: list) -> str:
    """Detect chart patterns"""
    if not price_data or len(price_data) < 5:
        return "unknown"
    
    changes = []
    for i in range(1, len(price_data)):
        if price_data[i-1] != 0:
            change = ((price_data[i] - price_data[i-1]) / price_data[i-1]) * 100
            changes.append(change)
    
    if not changes:
        return "unknown"
    
    max_change = max(changes)
    avg_change = sum(changes) / len(changes)
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
            signals.append(f"⚠️ High gain: {price_change_1h:.1f}% (still ok)")
        
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
            signals.append(f"⚠️ Low liquidity: ${liquidity_usd:,.0f}")
        
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
            signals.append(f"⚠️ Pattern: {pattern}")
        
        if price_change_5m < 0 and price_change_1h > 30:
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
        
        response = await HTTPManager.request(url, timeout=10)
        if response:
            data = response.json()
            pairs = data.get("pairs", [])
            
            filtered_pairs = []
            for pair in pairs:
                if pair.get("dexId") in ["raydium", "orca"]:
                    price_change_1h = float(pair.get("priceChange", {}).get("h1", 0))
                    liquidity_usd = float(pair.get("liquidity", {}).get("usd", 0))
                    
                    if (price_change_1h >= MOMENTUM_MIN_1H_GAIN * 0.8 and
                        liquidity_usd >= MOMENTUM_MIN_LIQUIDITY * 0.5):
                        filtered_pairs.append(pair)
            
            filtered_pairs.sort(key=lambda x: float(x.get("priceChange", {}).get("h1", 0)), reverse=True)
            
            return filtered_pairs[:MAX_MOMENTUM_TOKENS]
                
    except Exception as e:
        logging.error(f"Error fetching gainers: {e}")
    
    return []

async def momentum_scanner():
    """Elite Momentum Scanner - DISABLED BY DEFAULT"""
    if not MOMENTUM_SCANNER_ENABLED:
        logging.info("[Momentum Scanner] Disabled via configuration")
        return
    
    if should_send_telegram("momentum_scanner_start"):
        await send_telegram_alert(
            "🔥 MOMENTUM SCANNER ACTIVE 🔥\n\n"
            f"Mode: {'HYBRID AUTO-BUY' if MOMENTUM_AUTO_BUY else 'ALERT ONLY'}\n"
            f"Auto-buy threshold: {MIN_SCORE_AUTO_BUY}/5\n"
            f"Alert threshold: {MIN_SCORE_ALERT}/5\n"
            f"Target: {MOMENTUM_MIN_1H_GAIN}-{MOMENTUM_MAX_1H_GAIN}% gainers\n"
            f"Position sizes: 0.02-0.20 SOL\n\n"
            "Hunting for pumps..."
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
                        if score >= 5:
                            position_size = MOMENTUM_POSITION_5_SCORE
                        elif score >= 4:
                            position_size = MOMENTUM_POSITION_4_SCORE
                        elif score >= 3:
                            position_size = MOMENTUM_POSITION_3_SCORE
                        else:
                            position_size = MOMENTUM_POSITION_2_SCORE
                        
                        if not is_prime_time:
                            position_size *= 0.5
                        
                        if should_send_telegram(f"momentum_buy_{token_address}"):
                            await send_telegram_alert(
                                f"🎯 MOMENTUM AUTO-BUY 🎯\n\n"
                                f"Token: {token_symbol} ({token_address[:8]}...)\n"
                                f"Score: {score}/5 ⭐\n"
                                f"Position: {position_size} SOL\n\n"
                                f"Signals:\n" + "\n".join(signals[:5]) + "\n\n"
                                f"Executing..."
                            )
                        
                        success = await buy_token(token_address, amount=position_size)
                        
                        if success:
                            momentum_bought.add(token_address)
                            await send_telegram_alert(
                                f"✅ MOMENTUM BUY SUCCESS\n"
                                f"Token: {token_symbol}\n"
                                f"Amount: {position_size} SOL\n"
                                f"Strategy: Momentum Play"
                            )
                            asyncio.create_task(wait_and_auto_sell(token_address))
                        
                    elif score >= MIN_SCORE_ALERT:
                        position_size = MOMENTUM_POSITION_3_SCORE if score >= 3 else MOMENTUM_POSITION_2_SCORE
                        
                        if should_send_telegram(f"momentum_alert_{token_address}"):
                            await send_telegram_alert(
                                f"🔔 MOMENTUM OPPORTUNITY 🔔\n\n"
                                f"Token: {token_symbol} ({token_address[:8]}...)\n"
                                f"Score: {score}/5 ⭐\n"
                                f"Suggested: {position_size} SOL\n\n"
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
        
        response = await HTTPManager.request(url, timeout=10)
        if response:
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
                elif score >= 2:
                    recommendation = MOMENTUM_POSITION_2_SCORE
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
    """Start the sniper bot with configurable features"""
    
    global ACTIVE_LISTENERS, LISTENER_TASKS, last_listener_status_msg
    
    for listener_id, task in LISTENER_TASKS.items():
        if not task.done():
            logging.info(f"[CLEANUP] Cancelling existing task for {listener_id}")
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    
    ACTIVE_LISTENERS.clear()
    LISTENER_TASKS.clear()
    last_listener_status_msg.clear()
    
    mode_text = "Fresh Token Sniper (Age Check ENABLED)"
    TASKS.append(asyncio.create_task(start_dexscreener_monitor()))
    
    # Get default buy amount from config
    from utils import CONFIG
    default_buy_amount = CONFIG.BUY_AMOUNT_SOL
    
    if should_send_telegram(f"sniper_start"):
        await send_telegram_alert(
            f"💰 SNIPER LAUNCHING 💰\n\n"
            f"Mode: {mode_text}\n"
            f"Age Check: {'ENABLED' if STRICT_AGE_CHECK else 'DISABLED'}\n"
            f"Max Token Age: {MAX_TOKEN_AGE_SECONDS}s\n"
            f"Min LP: {RUG_LP_THRESHOLD} SOL\n"
            f"Min AI Score: {MIN_AI_SCORE}\n"
            f"Min Volume: ${MIN_VOLUME_USD:,.0f}\n"
            f"Momentum: {'ON' if MOMENTUM_SCANNER_ENABLED else 'OFF'}\n"
            f"Trending: {'OFF' if SKIP_TRENDING_SCANNER else 'ON'}\n"
            f"Buy Amount: {default_buy_amount} SOL\n\n"
            f"Ready to snipe FRESH tokens only! 🎯"
        )

    if FORCE_TEST_MINT:
        if should_send_telegram(f"force_test_{FORCE_TEST_MINT}"):
            await send_telegram_alert(f"🚨 Forced Test Buy: {FORCE_TEST_MINT}")
        try:
            success = await buy_token(FORCE_TEST_MINT, amount=default_buy_amount)
            if success:
                await wait_and_auto_sell(FORCE_TEST_MINT)
        except Exception as e:
            logging.error(f"Force buy error: {e}")

    TASKS.append(asyncio.create_task(daily_stats_reset_loop()))
    
    # Only start enabled listeners
    listeners = ["Raydium", "PumpFun", "Moonshot"]
    if not SKIP_JUPITER_MEMPOOL:
        listeners.append("Jupiter")
    
    for listener in listeners:
        task = asyncio.create_task(mempool_listener(listener))
        TASKS.append(task)
    
    # CRITICAL FIX: Add the PumpFun transaction scanner task
    TASKS.append(asyncio.create_task(pumpfun_tx_scanner_task()))
    
    # Only start enabled scanners
    if not SKIP_TRENDING_SCANNER:
        TASKS.append(asyncio.create_task(trending_scanner()))
    
    if MOMENTUM_SCANNER_ENABLED:
        TASKS.append(asyncio.create_task(momentum_scanner()))
    
    if ENABLE_PUMPFUN_MIGRATION:
        TASKS.append(asyncio.create_task(pumpfun_migration_monitor()))
        TASKS.append(asyncio.create_task(raydium_graduation_scanner()))
    
    if should_send_telegram("sniper_ready"):
        await send_telegram_alert(f"🎯 SNIPER ACTIVE - {mode_text}!")

async def start_sniper_with_forced_token(mint: str):
    """Force buy a specific token with momentum scoring"""
    try:
        if should_send_telegram(f"force_buy_{mint}"):
            await send_telegram_alert(f"🚨 FORCE BUY: {mint}")
        
        if not is_bot_running():
            if should_send_telegram(f"force_buy_paused_{mint}"):
                await send_telegram_alert(f"⛔ Bot is paused. Cannot force buy {mint}")
            return

        if mint in BROKEN_TOKENS or mint in BLACKLIST or mint in already_bought:
            if should_send_telegram(f"force_buy_blocked_{mint}"):
                await send_telegram_alert(f"❌ {mint} is blacklisted, broken, or already bought")
            return

        # Check if it's a verified PumpFun token
        is_pumpfun = await is_pumpfun_token(mint)
        
        # Get default buy amount from config
        from utils import CONFIG
        default_buy_amount = CONFIG.BUY_AMOUNT_SOL
        
        momentum_data = await check_momentum_score(mint)
        if momentum_data["score"] > 0:
            if should_send_telegram(f"momentum_check_{mint}"):
                await send_telegram_alert(
                    f"📊 MOMENTUM SCORE CHECK\n\n"
                    f"Token: {mint[:8]}...\n"
                    f"Score: {momentum_data['score']}/5 ⭐\n"
                    f"Signals:\n" + "\n".join(momentum_data['signals'][:5]) + "\n\n"
                    f"Recommended position: {momentum_data['recommendation']} SOL"
                )
            
            if momentum_data['score'] >= MIN_SCORE_AUTO_BUY:
                buy_amount = momentum_data['recommendation']
            else:
                buy_amount = PUMPFUN_EARLY_BUY if is_pumpfun else default_buy_amount
        else:
            buy_amount = PUMPFUN_EARLY_BUY if is_pumpfun else default_buy_amount
        
        logging.info(f"[FORCEBUY] Attempting forced buy for {mint} with {buy_amount} SOL")
        
        result = await buy_token(mint, amount=buy_amount, is_pumpfun=is_pumpfun)
        
        if result:
            already_bought.add(mint)
            if BLACKLIST_AFTER_BUY:
                BLACKLIST.add(mint)
                
            token_type = "Verified PumpFun" if is_pumpfun else "Standard"
            if momentum_data.get("score", 0) >= MIN_SCORE_AUTO_BUY:
                token_type = f"Momentum Play (Score: {momentum_data['score']}/5)"
                
            if should_send_telegram(f"force_buy_success_{mint}"):
                await send_telegram_alert(
                    f"✅ Force buy successful\n"
                    f"Token: {mint}\n"
                    f"Type: {token_type}\n"
                    f"Amount: {buy_amount} SOL"
                )
            await wait_and_auto_sell(mint)
        else:
            if should_send_telegram(f"force_buy_failed_{mint}"):
                await send_telegram_alert(f"❌ Force buy failed for {mint}")
            
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        if should_send_telegram(f"force_buy_error_{mint}"):
            await send_telegram_alert(f"❌ Force buy error: {e}")
        logging.exception(f"[FORCEBUY] Exception: {e}\n{tb}")

async def stop_all_tasks():
    """Stop all running tasks with cleanup"""
    global ACTIVE_LISTENERS, LISTENER_TASKS, last_listener_status_msg
    
    for listener_id, task in LISTENER_TASKS.items():
        if not task.done():
            logging.info(f"[STOP] Cancelling listener {listener_id}")
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    
    for task in TASKS:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    
    TASKS.clear()
    ACTIVE_LISTENERS.clear()
    LISTENER_TASKS.clear()
    last_listener_status_msg.clear()
    
    if should_send_telegram("tasks_stopped"):
        await send_telegram_alert("🛑 All sniper tasks stopped and cleaned up.")
