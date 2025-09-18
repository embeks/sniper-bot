# utils.py - COMPLETE FIXED VERSION WITH ALL PUMPFUN FIXES
import json
import logging
import httpx
import asyncio
import time
import csv
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Callable
from dotenv import load_dotenv
import base64
from solders.transaction import VersionedTransaction
import certifi

# Solana imports
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from spl.token.instructions import get_associated_token_address, close_account, CloseAccountParams

# Import Raydium client
from raydium_aggregator import RaydiumAggregatorClient

# Import config
import config

# Setup
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Load config once
CONFIG = config.load()

# CRITICAL FIX: Import PumpFun buy function at module level to avoid circular import
try:
    from pumpfun_buy import execute_pumpfun_buy as PUMPFUN_BUY_FN
    logging.info("[Init] PumpFun buy function loaded successfully")
except ImportError as e:
    PUMPFUN_BUY_FN = None
    logging.warning(f"[Init] PumpFun buy function not available: {e}")

# ============================================
# CENTRALIZED HTTP MANAGER - FIXED WITH RETRY LOGIC
# ============================================
class HTTPManager:
    """Centralized HTTP client with proper defaults and retry logic"""
    
    @staticmethod
    async def request(url: str, method: str = "GET", **kwargs):
        """Make HTTP request with retries and timeouts"""
        timeout = kwargs.pop('timeout', 10)
        retries = kwargs.pop('retries', 3)
        
        for attempt in range(retries):
            try:
                logging.debug(f"[HTTP] Attempt {attempt + 1}/{retries} for {method} {url[:50]}...")
                
                async with httpx.AsyncClient(
                    timeout=timeout,
                    verify=certifi.where(),
                    follow_redirects=True
                ) as client:
                    if method == "GET":
                        response = await client.get(url, **kwargs)
                    elif method == "POST":
                        response = await client.post(url, **kwargs)
                    else:
                        raise ValueError(f"Unsupported method: {method}")
                    
                    if response.status_code == 200:
                        logging.debug(f"[HTTP] Success on attempt {attempt + 1}")
                        return response
                    elif response.status_code == 429 and attempt < retries - 1:
                        # Rate limited, exponential backoff with jitter
                        wait_time = (2 ** attempt) * (1 + 0.1 * (time.time() % 1))
                        logging.debug(f"[HTTP] Rate limited (429), waiting {wait_time}s")
                        await asyncio.sleep(wait_time)
                        continue
                    elif response.status_code >= 500 and attempt < retries - 1:
                        # Server error, retry
                        logging.debug(f"[HTTP] Server error ({response.status_code}), retrying...")
                        await asyncio.sleep(1)
                        continue
                    else:
                        logging.debug(f"[HTTP] Failed with status {response.status_code}")
                        
            except (httpx.TimeoutException, asyncio.TimeoutError):  # FIXED: Catch both timeout types
                logging.debug(f"[HTTP] Timeout on attempt {attempt + 1}")
                if attempt < retries - 1:
                    await asyncio.sleep(1)
                    continue
            except Exception as e:
                logging.debug(f"[HTTP] Request error (attempt {attempt + 1}): {e}")
                if attempt < retries - 1:
                    await asyncio.sleep(1)
                    continue
        
        logging.debug(f"[HTTP] All {retries} attempts failed for {url[:50]}...")
        return None

# Known AMM program IDs for validation
KNOWN_AMM_PROGRAMS = {
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM V4
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # Raydium Stable
    "9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP",  # Orca
    "srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX",   # OpenBook/Serum
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",   # Jupiter Aggregator V6
    "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB",   # Jupiter Aggregator V4
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",   # Whirlpool
}

# PumpFun Program ID
PUMPFUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# Parse sell percentages
AUTO_SELL_PERCENT_2X = 50
AUTO_SELL_PERCENT_5X = 25
AUTO_SELL_PERCENT_10X = 25  # This is your moonbag

# PumpFun configuration
PUMPFUN_MIN_LIQUIDITY = {
    "graduated": 5.0,
    "near_graduation": 2.0,
    "early": 0.5,
    "ignore": 0.3
}

# Initialize clients
rpc = Client(CONFIG.RPC_URL, commitment=Confirmed)
raydium = RaydiumAggregatorClient(CONFIG.RPC_URL)

# Load wallet
import ast
try:
    if CONFIG.SOLANA_PRIVATE_KEY and CONFIG.SOLANA_PRIVATE_KEY.startswith("["):
        private_key_array = ast.literal_eval(CONFIG.SOLANA_PRIVATE_KEY)
        if len(private_key_array) == 64:
            keypair = Keypair.from_bytes(bytes(private_key_array))
        else:
            keypair = Keypair.from_seed(bytes(private_key_array[:32]))
    else:
        keypair = Keypair.from_base58_string(CONFIG.SOLANA_PRIVATE_KEY)
except Exception as e:
    raise ValueError(f"Failed to load wallet from SOLANA_PRIVATE_KEY: {e}")

wallet_pubkey = str(keypair.pubkey())
logging.info(f"ACTUAL WALLET BEING USED: {wallet_pubkey}")

# Use TELEGRAM_CHAT_ID but also support TELEGRAM_USER_ID
TELEGRAM_CHAT_ID = CONFIG.TELEGRAM_CHAT_ID or CONFIG.TELEGRAM_USER_ID

# Global state
OPEN_POSITIONS = {}
BROKEN_TOKENS = set()
BOT_RUNNING = True
BLACKLIST_FILE = "blacklist.json"
TRADES_CSV_FILE = "trades.csv"
BLACKLIST = set(CONFIG.BLACKLISTED_TOKENS.split(",")) if CONFIG.BLACKLISTED_TOKENS else set()

# Stop-loss tracking
STOPS: Dict[str, Dict] = {}

# Stats tracking
daily_stats = {
    "tokens_scanned": 0,
    "snipes_attempted": 0,
    "snipes_succeeded": 0,
    "sells_executed": 0,
    "profit_sol": 0.0,
    "skip_reasons": {
        "low_lp": 0,
        "blacklist": 0,
        "malformed": 0,
        "buy_failed": 0,
        "no_route": 0,
        "lp_timeout": 0,
        "old_token": 0,
        "quality_check": 0,
        "invalid_pumpfun": 0,
        "zero_lp_raydium": 0
    }
}

# Status tracking
listener_status = {"Raydium": "OFFLINE", "Jupiter": "OFFLINE", "PumpFun": "OFFLINE", "Moonshot": "OFFLINE"}
last_activity = time.time()
last_seen_token = {"Raydium": time.time(), "Jupiter": time.time(), "PumpFun": time.time(), "Moonshot": time.time()}

# Alert tracking for cooldowns
last_alert_times = {}

# Track PumpFun and trending tokens
pumpfun_tokens = {}
trending_tokens = set()

# Known token decimals
KNOWN_TOKEN_DECIMALS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": 6,  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": 6,  # USDT
    "So11111111111111111111111111111111111111112": 9,   # WSOL
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs": 8,  # WETH
    "7dHbWXmci3dT8UFYWYZweBLXgycu7Y3iL6trKn1Y7ARj": 9,  # stSOL
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So": 9,   # mSOL
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263": 5,  # Bonk
    "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R": 9,  # RAY
}

# Cache for token decimals
TOKEN_DECIMALS_CACHE = {}

# ============================================
# CRITICAL FIX: SIMPLIFIED PUMPFUN VERIFICATION
# ============================================
async def is_pumpfun_token(mint: str, assume_fresh: bool = False) -> bool:
    """Verify if a token is actually a PumpFun token - FIXED to be less strict"""
    try:
        # If we already tracked it as PumpFun, trust that
        if mint in pumpfun_tokens:
            return True
        
        # For fresh tokens detected via mempool, assume they're PumpFun if bonding curve exists
        if assume_fresh:
            try:
                mint_pubkey = Pubkey.from_string(mint)
                bonding_curve_seed = b"bonding-curve"
                pumpfun_program = Pubkey.from_string(PUMPFUN_PROGRAM_ID)
                bonding_curve, _ = Pubkey.find_program_address(
                    [bonding_curve_seed, bytes(mint_pubkey)],
                    pumpfun_program
                )
                
                bc_info = rpc.get_account_info(bonding_curve)
                if bc_info and bc_info.value:
                    # Fresh PumpFun tokens ALWAYS have SOL in bonding curve at creation
                    logging.info(f"[Verify] Fresh token {mint[:8]}... has bonding curve - assuming tradeable")
                    pumpfun_tokens[mint] = {
                        "discovered": time.time(),
                        "verified": True,
                        "migrated": False
                    }
                    return True
            except Exception as e:
                logging.debug(f"[Verify] Bonding curve check error: {e}")
        
        # Standard verification for non-fresh tokens
        try:
            mint_pubkey = Pubkey.from_string(mint)
            bonding_curve_seed = b"bonding-curve"
            pumpfun_program = Pubkey.from_string(PUMPFUN_PROGRAM_ID)
            bonding_curve, _ = Pubkey.find_program_address(
                [bonding_curve_seed, bytes(mint_pubkey)],
                pumpfun_program
            )
            
            bc_info = rpc.get_account_info(bonding_curve)
            if bc_info and bc_info.value:
                lamports = bc_info.value.lamports
                min_lamports = 1000000  # 0.001 SOL minimum
                
                if lamports >= min_lamports:
                    logging.info(f"[Verify] {mint[:8]}... has active bonding curve with {lamports/1e9:.4f} SOL")
                    if mint not in pumpfun_tokens:
                        pumpfun_tokens[mint] = {
                            "discovered": time.time(),
                            "verified": True,
                            "migrated": False
                        }
                    return True
        except Exception as e:
            logging.debug(f"[Verify] Bonding curve check error: {e}")
        
        # API fallback check
        try:
            url = f"https://frontend-api.pump.fun/coins/{mint}"
            response = await HTTPManager.request(url, timeout=3)
            if response:
                data = response.json()
                if data.get("mint") == mint:
                    market_cap = data.get("usd_market_cap", 0)
                    if market_cap > 100:
                        logging.info(f"[Verify] {mint[:8]}... found on PumpFun API with ${market_cap:.0f} MC")
                        if mint not in pumpfun_tokens:
                            pumpfun_tokens[mint] = {
                                "discovered": time.time(),
                                "verified": True,
                                "migrated": False,
                                "market_cap": market_cap
                            }
                        return True
        except Exception as e:
            logging.debug(f"[Verify] PumpFun API check error: {e}")
        
        return False
        
    except Exception as e:
        logging.error(f"[Verify] Error checking if {mint} is PumpFun: {e}")
        return False

# ============================================
# NOTIFICATION HELPER
# ============================================
async def notify(kind: str, text: str):
    """Alert policy helper - routes alerts based on config switches with cooldown"""
    alerts = getattr(CONFIG, "ALERTS_NOTIFY", {}) or {}
    if kind not in alerts:
        return True
    
    if not alerts.get(kind, False):
        return True
    
    # Check cooldown
    cooldown = alerts.get("cooldown_secs", 60)
    now = time.time()
    last_time = last_alert_times.get(kind, 0)
    
    if now - last_time < cooldown:
        return True  # Skip due to cooldown
    
    last_alert_times[kind] = now
    return await send_telegram_alert(text)

# ============================================
# STOP-LOSS HELPER FUNCTIONS
# ============================================
def register_stop(mint: str, stop_data: Dict):
    """Register a stop-loss order for a token"""
    STOPS[mint] = stop_data
    logging.info(f"[STOP] Registered for {mint[:8]}... - entry: {stop_data['entry_price']:.6f}, stop: {stop_data['stop_price']:.6f}, tokens: {stop_data['size_tokens']}")

async def check_mint_authority(mint: str) -> tuple[bool, bool]:
    """Check if mint and freeze authority are renounced"""
    try:
        mint_pubkey = Pubkey.from_string(mint)
        mint_info = rpc.get_account_info(mint_pubkey)
        
        if mint_info and mint_info.value:
            data = mint_info.value.data
            if isinstance(data, list) and len(data) > 0:
                import base64
                if isinstance(data[0], str):
                    decoded = base64.b64decode(data[0])
                else:
                    decoded = bytes(data[0])
                
                if len(decoded) >= 50:
                    mint_auth_opt = int.from_bytes(decoded[0:4], "little")
                    freeze_auth_opt = int.from_bytes(decoded[46:50], "little")
                    
                    mint_renounced = (mint_auth_opt == 0)
                    freeze_renounced = (freeze_auth_opt == 0)
                    
                    return mint_renounced, freeze_renounced
    except Exception as e:
        logging.debug(f"Authority check error for {mint}: {e}")
    
    return False, False

async def check_token_tax(mint: str) -> int:
    """Detect transfer tax on token via DexScreener"""
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        response = await HTTPManager.request(url)
        if response:
            data = response.json()
            if "pairs" in data and len(data["pairs"]) > 0:
                for pair in data["pairs"]:
                    buy_tax = pair.get("buyTax", 0)
                    sell_tax = pair.get("sellTax", 0)
                    if buy_tax > 0 or sell_tax > 0:
                        max_tax = max(buy_tax, sell_tax)
                        return int(max_tax * 100)
    except:
        pass
    
    return 0

# ============================================
# AGE CHECKING FUNCTIONS
# ============================================
async def is_fresh_token(mint: str, max_age_seconds: int = 60) -> bool:
    """Check if token/pool was created within the specified time"""
    try:
        # First try DexScreener
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        response = await HTTPManager.request(url, timeout=5)
        
        if response:
            data = response.json()
            if "pairs" in data and isinstance(data["pairs"], list) and len(data["pairs"]) > 0:
                for pair in data["pairs"]:
                    created_at = pair.get("pairCreatedAt")
                    if created_at:
                        age_ms = time.time() * 1000 - created_at
                        age_seconds = age_ms / 1000
                        
                        if age_seconds <= max_age_seconds:
                            logging.info(f"[AGE CHECK] Token {mint[:8]}... is {age_seconds:.0f}s old - FRESH!")
                            return True
                        else:
                            logging.info(f"[AGE CHECK] Token {mint[:8]}... is {age_seconds:.0f}s old (max: {max_age_seconds}s)")
                            return False
        
        # If DexScreener has no data, check on-chain
        logging.info(f"[AGE CHECK] No DexScreener data for {mint[:8]}... - checking on-chain")
        
        try:
            mint_pubkey = Pubkey.from_string(mint)
            signatures = rpc.get_signatures_for_address(
                mint_pubkey,
                limit=20,
                commitment="confirmed"
            )
            
            if signatures and hasattr(signatures, 'value') and signatures.value:
                creation_sig = signatures.value[-1]
                block_time = creation_sig.block_time
                
                if block_time:
                    age_seconds = time.time() - block_time
                    if age_seconds <= max_age_seconds:
                        logging.info(f"[AGE CHECK] Token {mint[:8]}... is {age_seconds:.0f}s old on-chain - FRESH!")
                        return True
                    else:
                        logging.info(f"[AGE CHECK] Token {mint[:8]}... is {age_seconds:.0f}s old on-chain - TOO OLD")
                        return False
        except Exception as e:
            logging.debug(f"[AGE CHECK] On-chain check failed: {e}")
        
        # For brand new tokens detected via mempool, assume they're fresh
        logging.info(f"[AGE CHECK] Cannot verify age for {mint[:8]}... - assuming FRESH (mempool detection)")
        return True
                
    except Exception as e:
        logging.error(f"Age check error: {e}")
        return False

async def verify_token_age_on_chain(mint: str, max_age_seconds: int = 60) -> bool:
    """Verify token age by checking mint creation on chain"""
    try:
        mint_pubkey = Pubkey.from_string(mint)
        
        signatures = rpc.get_signatures_for_address(
            mint_pubkey,
            limit=20,
            commitment="confirmed"
        )
        
        if not signatures or not hasattr(signatures, 'value') or not signatures.value:
            logging.info(f"[CHAIN CHECK] No signatures found for {mint[:8]}... - assuming old")
            return False
            
        creation_sig = signatures.value[-1]
        block_time = creation_sig.block_time
        
        if block_time:
            age_seconds = time.time() - block_time
            if age_seconds <= max_age_seconds:
                logging.info(f"[CHAIN CHECK] Token {mint[:8]}... is {age_seconds:.0f}s old - FRESH!")
                return True
            else:
                logging.info(f"[CHAIN CHECK] Token {mint[:8]}... is {age_seconds:.0f}s old - TOO OLD")
                return False
        else:
            logging.info(f"[CHAIN CHECK] No block time for {mint[:8]}... - assuming old")
            return False
        
    except Exception as e:
        logging.error(f"Chain age check error: {e}")
        return False

async def is_pumpfun_launch(mint: str) -> bool:
    """Check if this is a genuine PumpFun launch"""
    try:
        if mint in pumpfun_tokens:
            token_data = pumpfun_tokens[mint]
            if time.time() - token_data["discovered"] < 300:
                return True
        return False
    except:
        return False

# ============================================
# SCALING FUNCTIONS
# ============================================
async def get_dynamic_position_size(mint: str, pool_liquidity_sol: float, is_migration: bool = False) -> float:
    """Aggressive position sizing for 48hr gains"""
    try:
        balance = rpc.get_balance(keypair.pubkey()).value / 1e9
        
        base_size = 0.1
        
        if is_migration:
            base_size = 0.2
        elif mint in pumpfun_tokens:
            pf_status = await check_pumpfun_token_status(mint)
            if pf_status and pf_status.get("progress", 0) > 80:
                base_size = 0.15
        
        return max(0.05, min(base_size * balance, 0.25))
        
    except Exception as e:
        logging.error(f"Dynamic sizing error: {e}")
        return 0.1

def get_minimum_liquidity_required(balance_sol: float = None) -> float:
    """Aggressive liquidity for 48hr push"""
    return 1.0

async def evaluate_pumpfun_opportunity(mint: str, lp_sol: float) -> tuple[bool, float]:
    """Aggressive PumpFun evaluation"""
    try:
        pf_status = await check_pumpfun_token_status(mint)
        if not pf_status:
            return False, 0
        
        progress = pf_status.get("progress", 0)
        
        if lp_sol < PUMPFUN_MIN_LIQUIDITY["ignore"]:
            return False, 0
        
        if pf_status.get("graduated"):
            if lp_sol >= PUMPFUN_MIN_LIQUIDITY["graduated"]:
                return True, 0.15
        elif progress >= 80:
            if lp_sol >= PUMPFUN_MIN_LIQUIDITY["near_graduation"]:
                return True, 0.1
        elif progress >= 40:
            if lp_sol >= PUMPFUN_MIN_LIQUIDITY["early"]:
                return True, 0.05
        
        return False, 0
    except:
        return False, 0

# ============================================
# CORE FUNCTIONS
# ============================================
def update_last_activity():
    global last_activity
    last_activity = time.time()

def increment_stat(stat_name: str, value: int = 1):
    if stat_name in daily_stats:
        daily_stats[stat_name] += value

def record_skip(reason: str):
    if reason in daily_stats["skip_reasons"]:
        daily_stats["skip_reasons"][reason] += 1

def is_bot_running():
    return BOT_RUNNING

def start_bot():
    global BOT_RUNNING
    BOT_RUNNING = True

def stop_bot():
    global BOT_RUNNING
    BOT_RUNNING = False

def mark_broken_token(mint: str, error_code: int):
    # Do NOT mark brand-new PumpFun mints as broken for transient issues (first 3 minutes)
    try:
        if mint in pumpfun_tokens:
            discovered = pumpfun_tokens[mint].get("discovered", 0)
            if time.time() - discovered < 180:
                logging.info(f"[SKIP] {mint}: transient PumpFun error (not marking broken)")
                return
    except Exception:
        pass

    BROKEN_TOKENS.add(mint)
    log_skipped_token(mint, f"Marked as broken (error {error_code})")

def is_valid_mint(mint: str) -> bool:
    try:
        Pubkey.from_string(mint)
        return True
    except:
        return False

# ============================================
# TELEGRAM FUNCTIONS
# ============================================
async def send_telegram_alert(message: str, retry_count: int = 3) -> bool:
    """Send alert to Telegram"""
    try:
        url = f"https://api.telegram.org/bot{CONFIG.TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        
        for attempt in range(retry_count):
            try:
                response = await HTTPManager.request(url, method="POST", json=payload, timeout=10)
                if response and response.status_code == 200:
                    return True
                elif response and response.status_code == 429:
                    try:
                        data = response.json()
                        retry_after = data.get("parameters", {}).get("retry_after", 5)
                        logging.warning(f"Telegram rate limit hit, waiting {retry_after}s")
                        await asyncio.sleep(retry_after)
                    except:
                        await asyncio.sleep(5)
                    
            except Exception as e:
                logging.debug(f"Telegram send attempt {attempt + 1} failed: {e}")
                if attempt < retry_count - 1:
                    await asyncio.sleep(1)
        
        return False
        
    except Exception as e:
        logging.debug(f"Telegram send error: {e}")
        return False

async def send_telegram_batch(lines: List[str]):
    """Batch multiple messages together"""
    if lines:
        message = "\n".join(lines[:20])
        await send_telegram_alert(message)

def log_trade(mint: str, action: str, sol_amount: float, token_amount: float):
    """Log trade to CSV file"""
    try:
        with open(TRADES_CSV_FILE, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                datetime.now().isoformat(),
                mint,
                action,
                sol_amount,
                token_amount
            ])
    except Exception as e:
        logging.error(f"Failed to log trade: {e}")

def log_skipped_token(mint: str, reason: str):
    """Log skipped tokens"""
    logging.info(f"[SKIP] {mint}: {reason}")

def get_wallet_summary() -> str:
    """Get wallet balance summary"""
    try:
        balance = rpc.get_balance(keypair.pubkey()).value / 1e9
        balance_usd = balance * CONFIG.SOL_PRICE_USD
        
        if CONFIG.USE_DYNAMIC_SIZING:
            test_position = 0.1
            sizing_info = f"\n📏 Position Size: ~{test_position:.3f} SOL"
        else:
            sizing_info = f"\n📏 Fixed Size: {CONFIG.BUY_AMOUNT_SOL} SOL"
        
        return f"""Balance: {balance:.4f} SOL (${balance_usd:.0f})
Address: {wallet_pubkey}{sizing_info}
Min LP: {get_minimum_liquidity_required(balance)} SOL"""
    except:
        return "Failed to fetch wallet info"

def get_bot_status_message() -> str:
    """Get detailed bot status"""
    elapsed = int(time.time() - last_activity)
    raydium_elapsed = int(time.time() - last_seen_token["Raydium"])
    jupiter_elapsed = int(time.time() - last_seen_token["Jupiter"])
    pumpfun_elapsed = int(time.time() - last_seen_token["PumpFun"])
    moonshot_elapsed = int(time.time() - last_seen_token["Moonshot"])
    
    return f"""
🤖 Bot: {'RUNNING' if BOT_RUNNING else 'PAUSED'}
📊 Daily Stats:
  • Scanned: {daily_stats['tokens_scanned']}
  • Attempted: {daily_stats['snipes_attempted']}
  • Succeeded: {daily_stats['snipes_succeeded']}
  • Sells: {daily_stats['sells_executed']}
  • P&L: {daily_stats['profit_sol']:.4f} SOL
  
⏱ Last Activity: {elapsed}s ago
🔌 Listeners:
  • Raydium: {listener_status['Raydium']} ({raydium_elapsed}s)
  • Jupiter: {listener_status['Jupiter']} ({jupiter_elapsed}s)
  • PumpFun: {listener_status['PumpFun']} ({pumpfun_elapsed}s)
  • Moonshot: {listener_status['Moonshot']} ({moonshot_elapsed}s)
  
📈 Open Positions: {len(OPEN_POSITIONS)}
🛑 Active Stops: {len(STOPS)}
🚫 Broken Tokens: {len(BROKEN_TOKENS)}
💰 Min LP Filter: {CONFIG.MIN_LP_SOL} SOL
🎯 Scaling: {'ON' if CONFIG.USE_DYNAMIC_SIZING else 'OFF'}
"""

async def get_liquidity_and_ownership(mint: str) -> Optional[Dict[str, Any]]:
    """Get accurate liquidity with INCREASED timeout"""
    try:
        LP_CHECK_TIMEOUT = 8  # Increased from 5 seconds
        
        async def _check_liquidity():
            sol_mint = "So11111111111111111111111111111111111111112"
            
            # Check Raydium first
            pool = raydium.find_pool_realtime(mint)
            
            if pool:
                # Get actual SOL balance from vault
                if pool["baseMint"] == sol_mint:
                    sol_vault_key = pool["baseVault"]
                elif pool["quoteMint"] == sol_mint:
                    sol_vault_key = pool["quoteVault"]
                else:
                    logging.warning(f"[LP Check] Pool found but no SOL pair for {mint[:8]}...")
                    return {"liquidity": 0}
                
                sol_vault = Pubkey.from_string(sol_vault_key)
                response = rpc.get_balance(sol_vault)
                
                if response and hasattr(response, 'value'):
                    sol_balance = response.value / 1e9
                    
                    if sol_balance > 0:
                        logging.info(f"[LP Check] {mint[:8]}... has {sol_balance:.2f} SOL liquidity (Raydium)")
                        return {"liquidity": sol_balance}
                    else:
                        # Warm-up retry when Raydium pool shows 0
                        logging.warning(f"[LP Check] {mint[:8]}... has ZERO liquidity in Raydium pool, starting warm-up")
                        
                        # Check if ultra-fresh (< 30s old)
                        is_ultra_fresh = False
                        try:
                            is_ultra_fresh = await is_fresh_token(mint, max_age_seconds=30)
                        except:
                            pass
                        
                        if is_ultra_fresh:
                            # Progressive delays: 0.15s, 0.25s, 0.5s
                            delays = [0.15, 0.25, 0.5]
                            for i, delay in enumerate(delays):
                                await asyncio.sleep(delay)
                                try:
                                    retry_resp = rpc.get_balance(sol_vault)
                                    if retry_resp and hasattr(retry_resp, 'value'):
                                        sol_balance = retry_resp.value / 1e9
                                        if sol_balance > 0:
                                            logging.info(f"[LP Check] {mint[:8]}... warm-up success: {sol_balance:.2f} SOL (attempt {i+1}/{len(delays)})")
                                            return {"liquidity": sol_balance}
                                        else:
                                            logging.debug(f"[LP Check] Warm-up attempt {i+1}: still 0 SOL")
                                except Exception as e:
                                    logging.debug(f"[LP Check] Warm-up retry error: {e}")
                            
                            logging.warning(f"[LP Check] {mint[:8]}... still ZERO after warm-up")
                        else:
                            logging.info(f"[LP Check] {mint[:8]}... not ultra-fresh, skipping warm-up")
                        
                        return {"liquidity": 0}
            
            # Fallback to Jupiter
            logging.info(f"[LP Check] No Raydium pool, checking Jupiter...")
            try:
                response = await HTTPManager.request(
                    CONFIG.JUPITER_QUOTE_BASE_URL,
                    params={
                        "inputMint": sol_mint,
                        "outputMint": mint,
                        "amount": str(int(0.001 * 1e9)),
                        "slippageBps": "500",
                        "onlyDirectRoutes": "false"
                    }
                )
                
                if response:
                    quote = response.json()
                    if quote.get("outAmount") and int(quote.get("outAmount", 0)) > 0:
                        price_impact = float(quote.get("priceImpactPct", 100))
                        
                        # Estimate liquidity from price impact
                        if price_impact < 1:
                            estimated_lp = 10.0
                        elif price_impact < 5:
                            estimated_lp = 3.0
                        elif price_impact < 10:
                            estimated_lp = 1.0
                        else:
                            estimated_lp = 0.5
                        
                        logging.info(f"[LP Check] {mint[:8]}... on Jupiter with estimated {estimated_lp:.2f} SOL liquidity")
                        return {"liquidity": estimated_lp}
                    else:
                        logging.info(f"[LP Check] No viable route on Jupiter for {mint[:8]}...")
                        return {"liquidity": 0}
            except Exception as e:
                logging.debug(f"[LP Check] Jupiter check failed: {e}")
            
            logging.info(f"[LP Check] No liquidity found for {mint[:8]}... on any DEX")
            return {"liquidity": 0}
        
        try:
            result = await asyncio.wait_for(_check_liquidity(), timeout=LP_CHECK_TIMEOUT)
            return result
        except asyncio.TimeoutError:
            logging.warning(f"[LP Check] Timeout after {LP_CHECK_TIMEOUT}s for {mint[:8]}...")
            record_skip("lp_timeout")
            return {"liquidity": 0.1}  # Assume minimal liquidity for fresh tokens
            
    except Exception as e:
        logging.error(f"[LP Check] Error for {mint}: {e}")
        return {"liquidity": 0}

async def get_trending_mints():
    """Placeholder for trending mints"""
    return []

async def daily_stats_reset_loop():
    """Reset daily stats at midnight"""
    while True:
        try:
            now = datetime.now()
            midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
            seconds_until_midnight = (midnight - now).total_seconds()
            await asyncio.sleep(seconds_until_midnight)
            
            daily_stats["tokens_scanned"] = 0
            daily_stats["snipes_attempted"] = 0
            daily_stats["snipes_succeeded"] = 0
            daily_stats["sells_executed"] = 0
            daily_stats["profit_sol"] = 0.0
            for key in daily_stats["skip_reasons"]:
                daily_stats["skip_reasons"][key] = 0
                
            logging.info("Daily stats reset")
        except Exception as e:
            logging.error(f"Stats reset error: {e}")
            await asyncio.sleep(3600)

async def get_jupiter_quote(input_mint: str, output_mint: str, amount: int, slippage_bps: int = None):
    """Get swap quote from Jupiter API v6"""
    if slippage_bps is None:
        slippage_bps = CONFIG.STOP_MAX_SLIPPAGE_BPS
        
    try:
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(slippage_bps),
            "onlyDirectRoutes": "false",
            "asLegacyTransaction": "false"
        }
        
        response = await HTTPManager.request(CONFIG.JUPITER_QUOTE_BASE_URL, params=params)
        if response:
            quote = response.json()
            
            return {
                "inAmount": quote.get("inAmount", "0"),
                "outAmount": quote.get("outAmount", "0"),
                "otherAmountThreshold": quote.get("otherAmountThreshold", "0"),
                "priceImpactPct": quote.get("priceImpactPct", "0"),
                "routePlan": quote.get("routePlan", [])
            }
        else:
            logging.warning(f"[Jupiter] Quote request failed")
            return None
    except Exception as e:
        logging.error(f"[Jupiter] Error getting quote: {e}")
        return None

async def get_jupiter_swap_transaction(quote: dict, user_pubkey: str, slippage_bps: int = None, priority_fee_lamports: int = None):
    """Get the swap transaction from Jupiter v6"""
    if slippage_bps is None:
        slippage_bps = CONFIG.STOP_MAX_SLIPPAGE_BPS
    
    if priority_fee_lamports is None:
        priority_fee_lamports = 500000  # Default 0.0005 SOL
        
    try:
        body = {
            "quoteResponse": quote,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": priority_fee_lamports,
            "slippageBps": slippage_bps
        }
        
        response = await HTTPManager.request(CONFIG.JUPITER_SWAP_URL, method="POST", json=body, timeout=15)
        if response:
            data = response.json()
            logging.info("[Jupiter] Swap transaction received")
            return data
        else:
            logging.warning(f"[Jupiter] Swap request failed")
            return None
    except Exception as e:
        logging.error(f"[Jupiter] Error getting swap transaction: {e}")
        return None

async def simulate_transaction(tx: VersionedTransaction) -> bool:
    """Simulate transaction"""
    try:
        result = rpc.simulate_transaction(tx, commitment=Confirmed)
        
        if not result or not result.value:
            logging.warning("[Simulation] No result returned")
            return False
        
        if result.value.err:
            logging.warning(f"[Simulation] Error: {result.value.err}")
            return False
        
        return True
        
    except Exception as e:
        logging.error(f"[Simulation] Error: {e}")
        return False

async def execute_jupiter_swap(mint: str, amount_lamports: int, slippage_bps: int = None, priority_fee_lamports: int = None) -> Dict[str, Any]:
    """Execute a swap using Jupiter v6"""
    try:
        if slippage_bps is None:
            slippage_bps = getattr(CONFIG, 'BUY_SLIPPAGE_BPS', 2000)
        
        if priority_fee_lamports is None:
            priority_fee_lamports = getattr(CONFIG, 'BUY_PRIORITY_FEE_LAMPORTS', 500000)
        
        input_mint = "So11111111111111111111111111111111111111112"
        output_mint = mint
        
        logging.info(f"[Jupiter] Getting quote for {amount_lamports/1e9:.4f} SOL -> {mint[:8]}...")
        quote = await get_jupiter_quote(input_mint, output_mint, amount_lamports, slippage_bps)
        if not quote:
            logging.warning("[Jupiter] No quote received")
            return {"ok": False, "reason": "NO_QUOTE"}
        
        out_amount = int(quote.get("outAmount", 0))
        other_amount_threshold = int(quote.get("otherAmountThreshold", 0))
        
        if out_amount == 0 or other_amount_threshold == 0:
            logging.warning(f"[Jupiter] Invalid quote - outAmount: {out_amount}, threshold: {other_amount_threshold}")
            record_skip("no_route")
            return {"ok": False, "reason": "NO_QUOTE"}
        
        logging.info(f"[Jupiter] Quote received, expecting {out_amount} tokens")
        
        logging.info("[Jupiter] Building swap transaction...")
        swap_data = await get_jupiter_swap_transaction(quote, wallet_pubkey, slippage_bps, priority_fee_lamports)
        if not swap_data:
            logging.warning("[Jupiter] Could not build transaction")
            return {"ok": False, "reason": "BUILD_FAILED"}
        
        logging.info("[Jupiter] Transaction built")
        
        try:
            tx_bytes = base64.b64decode(swap_data["swapTransaction"])
            tx = VersionedTransaction.from_bytes(tx_bytes)
            signed_tx = VersionedTransaction(tx.message, [keypair])
            logging.info("[Jupiter] Transaction signed")
        except Exception as e:
            logging.error(f"[Jupiter] Decode/sign error: {e}")
            return {"ok": False, "reason": "BUILD_FAILED"}
        
        if CONFIG.SIMULATE_BEFORE_SEND:
            logging.info("[Jupiter] Simulating transaction...")
            if not await simulate_transaction(signed_tx):
                logging.warning("[Jupiter] Simulation failed")
                return {"ok": False, "reason": "SIM_FAIL"}
            logging.info("[Jupiter] Simulation passed")
        
        logging.info("[Jupiter] Sending transaction...")
        
        try:
            result = rpc.send_transaction(
                signed_tx,
                opts=TxOpts(
                    skip_preflight=True,
                    preflight_commitment=Confirmed,
                    max_retries=3
                )
            )
            
            if result.value:
                sig = str(result.value)
                logging.info(f"[Jupiter] Transaction sent: {sig}")
                
                await asyncio.sleep(2)
                
                try:
                    from solders.signature import Signature
                    sig_obj = Signature.from_string(sig)
                    status = rpc.get_signature_statuses([sig_obj])
                    if status.value[0] is not None:
                        if status.value[0].confirmation_status:
                            logging.info(f"[Jupiter] Transaction confirmed: {status.value[0].confirmation_status}")
                            return {"ok": True, "sig": sig, "reason": "OK"}
                        elif status.value[0].err:
                            logging.error(f"[Jupiter] Transaction error: {status.value[0].err}")
                            await cleanup_wsol_on_failure()
                            return {"ok": False, "reason": "CONFIRM_TIMEOUT"}
                    else:
                        logging.info("[Jupiter] Transaction pending, returning signature")
                        return {"ok": True, "sig": sig, "reason": "OK"}
                except Exception as e:
                    logging.debug(f"[Jupiter] Status check error: {e}, returning signature anyway")
                    return {"ok": True, "sig": sig, "reason": "OK"}
            else:
                logging.error(f"[Jupiter] No signature returned")
                await cleanup_wsol_on_failure()
                return {"ok": False, "reason": "SEND_ERR"}
                
        except Exception as e:
            logging.error(f"[Jupiter] Send error: {e}")
            await cleanup_wsol_on_failure()
            return {"ok": False, "reason": "SEND_ERR"}
            
    except Exception as e:
        logging.error(f"[Jupiter] Swap execution error: {e}")
        return {"ok": False, "reason": "SEND_ERR"}

async def execute_jupiter_sell(mint: str, amount: int, slippage_bps: int = None) -> Dict[str, Any]:
    """Execute a sell using Jupiter v6"""
    if slippage_bps is None:
        slippage_bps = CONFIG.STOP_MAX_SLIPPAGE_BPS

    try:
        input_mint = mint
        output_mint = "So11111111111111111111111111111111111111112"

        logging.info(f"[Jupiter] Getting sell quote for {mint[:8]}... with {slippage_bps} bps slippage")
        quote = await get_jupiter_quote(input_mint, output_mint, amount, slippage_bps)
        if not quote:
            return {"ok": False, "reason": "NO_QUOTE", "sig": None}

        swap_data = await get_jupiter_swap_transaction(quote, wallet_pubkey, slippage_bps)
        if not swap_data:
            return {"ok": False, "reason": "BUILD_FAILED", "sig": None}

        try:
            tx_bytes = base64.b64decode(swap_data["swapTransaction"])
            tx = VersionedTransaction.from_bytes(tx_bytes)
            signed_tx = VersionedTransaction(tx.message, [keypair])
        except Exception as e:
            logging.error(f"[Jupiter] Sell tx decode/sign error: {e}")
            return {"ok": False, "reason": "BUILD_FAILED", "sig": None}

        if CONFIG.SIMULATE_BEFORE_SEND:
            logging.info("[Jupiter] Simulating sell transaction...")
            if not await simulate_transaction(signed_tx):
                logging.error(f"[Jupiter] Sell simulation failed for {mint[:8]}...")
                return {"ok": False, "reason": "SIM_FAIL", "sig": None}

        logging.info("[Jupiter] Sending sell transaction...")
        try:
            result = rpc.send_transaction(
                signed_tx,
                opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed, max_retries=3)
            )
            if not result.value:
                logging.error("[Jupiter] Failed to send sell transaction")
                return {"ok": False, "reason": "SEND_ERR", "sig": None}

            sig = str(result.value)
            logging.info(f"[Jupiter] Sell transaction sent: {sig}")

            await asyncio.sleep(2)
            try:
                from solders.signature import Signature
                sig_obj = Signature.from_string(sig)
                status = rpc.get_signature_statuses([sig_obj])
                if status.value[0] is not None:
                    if status.value[0].confirmation_status:
                        logging.info(f"[Jupiter] Sell confirmed: {status.value[0].confirmation_status}")
                    elif status.value[0].err:
                        logging.error(f"[Jupiter] Sell failed: {status.value[0].err}")
                        return {"ok": False, "reason": "CONFIRM_TIMEOUT", "sig": sig}
            except Exception as e:
                logging.debug(f"[Jupiter] Sell status check: {e}")

            return {"ok": True, "reason": "OK", "sig": sig}

        except Exception as e:
            logging.error(f"[Jupiter] Sell send error: {e}")
            return {"ok": False, "reason": "SEND_ERR", "sig": None}

    except Exception as e:
        logging.error(f"[Jupiter] Sell execution error: {e}")
        return {"ok": False, "reason": "SEND_ERR", "sig": None}

async def cleanup_wsol_on_failure():
    """Clean up stranded WSOL on swap failure"""
    try:
        from spl.token.constants import WRAPPED_SOL_MINT, TOKEN_PROGRAM_ID
        wsol_account = get_associated_token_address(keypair.pubkey(), WRAPPED_SOL_MINT)
        
        response = rpc.get_token_account_balance(wsol_account)
        if response and response.value and int(response.value.amount) > 0:
            close_ix = close_account(
                CloseAccountParams(
                    account=wsol_account,
                    dest=keypair.pubkey(),
                    owner=keypair.pubkey(),
                    program_id=TOKEN_PROGRAM_ID
                )
            )
            
            from solders.message import MessageV0
            recent_blockhash = rpc.get_latest_blockhash().value.blockhash
            msg = MessageV0.try_compile(
                payer=keypair.pubkey(),
                instructions=[close_ix],
                address_lookup_table_accounts=[],
                recent_blockhash=recent_blockhash,
            )
            tx = VersionedTransaction(msg, [keypair])
            
            result = rpc.send_transaction(tx)
            if result.value:
                logging.info(f"[WSOL Cleanup] Recovered stranded WSOL: {result.value}")
                
    except Exception as e:
        logging.debug(f"[WSOL Cleanup] Error: {e}")

# ============================================
# CRITICAL FIX: ENHANCED BUY_TOKEN WITH PUMPFUN SUPPORT
# ============================================
async def buy_token(mint: str, amount: float = None, **kwargs) -> bool:
    """Execute buy with enhanced PumpFun detection and direct bonding curve support"""
    overall_timeout = 15
    
    try:
        async def _buy_with_timeout():
            if mint in BROKEN_TOKENS:
                log_skipped_token(mint, "Broken token")
                record_skip("malformed")
                return False

            increment_stat("snipes_attempted", 1)
            update_last_activity()
            
            buy_amt = CONFIG.BUY_AMOUNT_SOL if amount is None else amount
            buy_amt = max(0.01, min(buy_amt, 0.5))
            
            logging.info(f"[Buy] Starting buy process for {mint[:8]}... with {buy_amt:.3f} SOL")
            
            # ============================================
            # CRITICAL: DETECT AND ROUTE PUMPFUN TOKENS
            # ============================================
            is_pumpfun = kwargs.get("is_pumpfun", False)
            is_migration = kwargs.get("is_migration", False)
            
            # ENHANCED: Check if it's actually PumpFun even if not marked
            if not is_pumpfun:
                # For fresh tokens, be more aggressive about assuming PumpFun
                try:
                    is_ultra_fresh = await is_fresh_token(mint, max_age_seconds=45)
                except:
                    is_ultra_fresh = False
                
                # Check with relaxed verification for fresh tokens
                is_pumpfun = await is_pumpfun_token(mint, assume_fresh=is_ultra_fresh)
                if is_pumpfun:
                    logging.info(f"[Buy] Detected as PumpFun token via verification")
            
            # Skip known AMM programs
            if mint in KNOWN_AMM_PROGRAMS:
                logging.warning(f"[Buy] Skipping known AMM program {mint[:8]}...")
                return False
            
            # ============================================
            # PUMPFUN DIRECT BUY PATH (NO FALLBACK)
            # ============================================
            if is_pumpfun:
                logging.info(f"[Buy] PumpFun token detected - using DIRECT bonding curve buy")
                
                # Check if function is available
                if PUMPFUN_BUY_FN is None:
                    logging.error(f"[Buy] PumpFun buy function not available!")
                    record_skip("buy_failed")
                    return False
                
                # Conservative size for PumpFun
                buy_amt = min(buy_amt, 0.02)
                
                try:
                    pf_res = await PUMPFUN_BUY_FN(
                        mint=mint,
                        sol_amount=buy_amt,
                        slippage_bps=getattr(CONFIG, "BUY_SLIPPAGE_BPS", 2000),
                        priority_fee_lamports=getattr(CONFIG, "BUY_PRIORITY_FEE_LAMPORTS", 500000),
                    )
                    
                    if not pf_res or not pf_res.get("ok"):
                        reason = (pf_res or {}).get("reason", "UNKNOWN")
                        logging.warning(f"[Buy] PumpFun direct buy failed: {reason}")
                        
                        # Don't blacklist for transient errors
                        transient_errors = {"ProgramAccountNotFound", "SIM_FAIL", "NO_QUOTE", 
                                          "CONFIRM_TIMEOUT", "BUILD_FAILED", "InsufficientFunds"}
                        if reason not in transient_errors:
                            mark_broken_token(mint, 1)
                        
                        record_skip("buy_failed")
                        return False
                    
                    # Success - process result
                    sig = pf_res.get("sig")
                    real_tokens = int(pf_res.get("tokens_received", buy_amt * 1e9 * 100))
                    
                    # Register position
                    await _register_successful_buy(
                        mint=mint,
                        buy_amt=buy_amt,
                        real_tokens=real_tokens,
                        sig=sig,
                        is_pumpfun=True,
                        is_migration=is_migration
                    )
                    
                    increment_stat("snipes_succeeded", 1)
                    log_trade(mint, "BUY", buy_amt, real_tokens)
                    return True
                    
                except Exception as e:
                    logging.error(f"[Buy] PumpFun buy exception: {e}")
                    record_skip("buy_failed")
                    return False
            
            # ============================================
            # JUPITER PATH FOR NON-PUMPFUN TOKENS
            # ============================================
            
            # Check if ultra-fresh
            try:
                ultra_fresh = await is_fresh_token(mint, max_age_seconds=45)
            except:
                ultra_fresh = False
            
            # Check liquidity
            logging.info(f"[Buy] Checking liquidity for {mint[:8]}...")
            lp_data = await get_liquidity_and_ownership(mint)
            
            if lp_data is None:
                logging.warning(f"[Buy] LP check timed out, assuming minimal liquidity")
                pool_liquidity = 0.1
            else:
                pool_liquidity = lp_data.get("liquidity", 0)
            
            # ============================================
            # CRITICAL FIX: HANDLE ZERO LIQUIDITY PROPERLY
            # ============================================
            if pool_liquidity == 0:
                # Double-check if this is actually a PumpFun token
                logging.info(f"[Buy] Zero liquidity detected - checking if PumpFun token...")
                is_verified_pumpfun = await is_pumpfun_token(mint, assume_fresh=ultra_fresh)
                
                if is_verified_pumpfun:
                    # Redirect to PumpFun path
                    logging.info(f"[Buy] Zero LP token is PumpFun - redirecting to direct buy")
                    # Recursive call with is_pumpfun=True flag
                    return await buy_token(mint, amount=buy_amt, is_pumpfun=True, is_migration=is_migration)
                else:
                    # Skip zero liquidity non-PumpFun tokens
                    logging.warning(f"[Buy] Skipping zero liquidity non-PumpFun token")
                    log_skipped_token(mint, "Zero liquidity")
                    record_skip("zero_lp_raydium")
                    return False
            
            # Adjust minimum LP for ultra-fresh tokens
            effective_min_lp = CONFIG.MIN_LP_SOL
            if ultra_fresh and not is_pumpfun:
                pool = raydium.find_pool_realtime(mint)
                if pool:
                    newborn_min_lp = getattr(CONFIG, 'NEWBORN_RAYDIUM_MIN_LP_SOL', 0.2)
                    effective_min_lp = max(CONFIG.MIN_LP_SOL, newborn_min_lp)
                    logging.info(f"[Buy] Ultra-fresh Raydium token - using raised LP floor: {effective_min_lp:.2f} SOL")
            
            # Check minimum liquidity
            if pool_liquidity < effective_min_lp:
                if ultra_fresh:
                    logging.warning(f"[Buy] Ultra-fresh token with low LP, proceeding with caution")
                else:
                    log_skipped_token(mint, f"Low liquidity: {pool_liquidity:.2f} SOL")
                    record_skip("low_lp")
                    return False
            
            # Check authorities if configured
            if CONFIG.REQUIRE_AUTH_RENOUNCED:
                mint_renounced, freeze_renounced = await check_mint_authority(mint)
                if not (mint_renounced and freeze_renounced):
                    if ultra_fresh:
                        logging.warning(f"[Buy] Authority not renounced for ultra-fresh token")
                    else:
                        log_skipped_token(mint, "Authority not renounced")
                        return False
            
            # Check tax
            tax_bps = await check_token_tax(mint)
            if tax_bps > CONFIG.MAX_TRADE_TAX_BPS:
                log_skipped_token(mint, f"High tax: {tax_bps/100:.1f}%")
                return False
            
            # Skip sell route check for ultra-fresh tokens
            if not ultra_fresh:
                estimated_tokens = int(buy_amt * 1e9 * 100)
                sell_quote = await get_jupiter_quote(mint, "So11111111111111111111111111111111111111112", estimated_tokens, 500)
                if not sell_quote or int(sell_quote.get("outAmount", 0)) == 0:
                    log_skipped_token(mint, "No sell route available")
                    record_skip("no_route")
                    return False
            
            # Dynamic sizing if enabled
            if CONFIG.USE_DYNAMIC_SIZING and buy_amt == CONFIG.BUY_AMOUNT_SOL:
                buy_amt = await get_dynamic_position_size(mint, pool_liquidity, is_migration)
            
            # Risk check
            try:
                from integrate_monster import risk_manager
                if risk_manager and not await risk_manager.check_risk_limits():
                    logging.warning(f"[Buy] Risk limits hit")
                    return False
            except:
                pass
            
            amount_lamports = int(buy_amt * 1e9)
            logging.info(f"[Buy] Executing Jupiter swap for {buy_amt:.3f} SOL")
            
            # Execute swap with retries for ultra-fresh tokens
            buy_slippage = CONFIG.BUY_SLIPPAGE_BPS
            buy_priority_fee = CONFIG.BUY_PRIORITY_FEE_LAMPORTS
            
            max_attempts = 3 if ultra_fresh else 1
            delay_1 = CONFIG.BUY_RETRY_DELAY_1_MS / 1000.0
            delay_2 = CONFIG.BUY_RETRY_DELAY_2_MS / 1000.0
            backoff_delays = [delay_1, delay_2]
            
            swap_result = {"ok": False, "reason": "NO_QUOTE"}
            
            for attempt in range(max_attempts):
                logging.info(f"[Buy] Jupiter attempt {attempt + 1}/{max_attempts}")
                
                swap_result = await execute_jupiter_swap(
                    mint, 
                    amount_lamports,
                    slippage_bps=buy_slippage,
                    priority_fee_lamports=buy_priority_fee
                )
                
                if swap_result["ok"]:
                    break  # Success!
                else:
                    reason = swap_result["reason"]
                    logging.warning(f"[Buy] Swap failed: {reason}")
                    
                    if reason in ["NO_QUOTE", "CONFIRM_TIMEOUT"] and ultra_fresh and attempt < max_attempts - 1:
                        wait_time = backoff_delays[min(attempt, len(backoff_delays)-1)]
                        logging.info(f"[Buy] Ultra-fresh retry after {wait_time}s...")
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        if reason in ["BUILD_FAILED", "SIM_FAIL", "SEND_ERR"] and not ultra_fresh:
                            mark_broken_token(mint, 1)
                        
                        log_skipped_token(mint, f"Jupiter swap failed: {reason}")
                        record_skip("buy_failed")
                        return False
            
            # Check if we succeeded
            if not swap_result["ok"]:
                reason = swap_result["reason"]
                if reason in ["BUILD_FAILED", "SIM_FAIL", "SEND_ERR"] and not ultra_fresh:
                    mark_broken_token(mint, 1)
                
                log_skipped_token(mint, f"All attempts failed: {reason}")
                record_skip("buy_failed")
                return False
            
            # Success - get signature and balance
            sig = swap_result["sig"]
            
            # Get real token balance
            from spl.token.constants import TOKEN_PROGRAM_ID
            owner = keypair.pubkey()
            token_account = get_associated_token_address(owner, Pubkey.from_string(mint))
            
            real_tokens = 0
            for retry in range(10):
                try:
                    response = rpc.get_token_account_balance(token_account)
                    if response and response.value:
                        real_tokens = int(response.value.amount)
                        if real_tokens > 0:
                            logging.info(f"[Buy] Got real token balance: {real_tokens}")
                            break
                except:
                    pass
                await asyncio.sleep(0.3)
            
            if real_tokens == 0:
                estimated_tokens = int(buy_amt * 1e9 * 100)
                real_tokens = estimated_tokens
                logging.warning(f"[Buy] Using estimate: {real_tokens}")
            
            # Register successful buy
            await _register_successful_buy(
                mint=mint,
                buy_amt=buy_amt,
                real_tokens=real_tokens,
                sig=sig,
                is_pumpfun=False,
                is_migration=is_migration,
                pool_liquidity=pool_liquidity
            )
            
            increment_stat("snipes_succeeded", 1)
            log_trade(mint, "BUY", buy_amt, real_tokens)
            return True
        
        # Execute with timeout
        try:
            return await asyncio.wait_for(_buy_with_timeout(), timeout=overall_timeout)
        except asyncio.TimeoutError:
            logging.error(f"[Buy] Overall timeout ({overall_timeout}s) for {mint[:8]}...")
            record_skip("buy_failed")
            return False
            
    except Exception as e:
        logging.error(f"[Buy] Unexpected error: {e}")
        return False

async def _register_successful_buy(mint: str, buy_amt: float, real_tokens: int, 
                                  sig: str = None, is_pumpfun: bool = False, 
                                  is_migration: bool = False, pool_liquidity: float = 0.1):
    """Helper to register successful buy and setup monitoring"""
    try:
        balance = rpc.get_balance(keypair.pubkey()).value / 1e9
        balance_usd = balance * CONFIG.SOL_PRICE_USD
        
        # Get entry price
        entry_price = await get_token_price_usd(mint)
        if not entry_price:
            entry_price = (buy_amt * CONFIG.SOL_PRICE_USD) / (real_tokens / (10 ** await get_token_decimals(mint)))
        
        # Register stop-loss
        register_stop(mint, {
            "entry_price": entry_price,
            "size_tokens": real_tokens,
            "stop_price": entry_price * (1 - CONFIG.STOP_LOSS_PCT),
            "slippage_bps": CONFIG.STOP_MAX_SLIPPAGE_BPS,
            "state": "ARMED",
            "last_alert": 0,
            "first_no_route": 0,
            "stuck_reason": None,
            "emergency_attempts": 0
        })
        
        # Determine buy method
        buy_method = "PumpFun Direct" if is_pumpfun else "Jupiter"
        token_type = "PumpFun" if is_pumpfun else "Regular"
        
        # Send notification
        await notify("buy",
            f"✅ Sniped {mint[:8]}... via {buy_method}\n"
            f"Type: {token_type}\n"
            f"Amount: {buy_amt:.3f} SOL\n"
            f"Tokens: {real_tokens}\n"
            f"LP: {pool_liquidity:.2f} SOL\n"
            f"Entry: ${entry_price:.6f}\n"
            f"Stop: ${entry_price * (1 - CONFIG.STOP_LOSS_PCT):.6f}\n"
            f"{'🚀 MIGRATION!' if is_migration else ''}\n"
            f"Balance: {balance:.2f} SOL (${balance_usd:.0f})\n"
            f"{f'TX: https://solscan.io/tx/{sig}' if sig else ''}"
        )
        
        # Store position
        OPEN_POSITIONS[mint] = {
            "expected_token_amount": real_tokens,
            "buy_amount_sol": buy_amt,
            "sold_stages": set(),
            "buy_sig": sig,
            "is_migration": is_migration,
            "entry_price": entry_price,
            "is_pumpfun": is_pumpfun
        }
        
        # Track PumpFun token if applicable
        if is_pumpfun and mint not in pumpfun_tokens:
            pumpfun_tokens[mint] = {
                "discovered": time.time(),
                "verified": True,
                "migrated": False
            }
        
    except Exception as e:
        logging.error(f"[Buy] Error registering successful buy: {e}")

async def sell_token(mint: str, amount_to_sell=None, percentage=100, slippage_bps=None):
    """Execute sell transaction - uses PumpFun for bonding curve tokens, Jupiter for migrated"""
    if slippage_bps is None:
        slippage_bps = CONFIG.STOP_MAX_SLIPPAGE_BPS
        
    try:
        from spl.token.constants import TOKEN_PROGRAM_ID
        
        owner = keypair.pubkey()
        token_account = get_associated_token_address(owner, Pubkey.from_string(mint))
        
        try:
            response = rpc.get_token_account_balance(token_account)
            if not response or not hasattr(response, 'value') or not response.value:
                logging.warning(f"No token balance found for {mint}")
                return False
            
            balance = int(response.value.amount)
        except Exception as e:
            logging.error(f"Failed to get token balance for {mint}: {e}")
            return False
        
        # Calculate amount to sell
        if amount_to_sell is not None:
            amount = amount_to_sell
        else:
            amount = int(balance * percentage / 100)
        
        if amount == 0:
            logging.warning(f"Zero balance to sell for {mint[:8]}...")
            return False

        logging.info(f"[Sell] Selling {percentage}% ({amount} tokens) of {mint[:8]}...")

        # Check if this is a PumpFun token still on bonding curve
        is_pumpfun_bonding = False
        if mint in pumpfun_tokens:
            # Check if it has migrated to Raydium
            lp_data = await get_liquidity_and_ownership(mint)
            if lp_data and lp_data.get("liquidity", 0) > 1.0:
                logging.info(f"[Sell] PumpFun token {mint[:8]}... has migrated, using Jupiter")
                is_pumpfun_bonding = False
            else:
                logging.info(f"[Sell] PumpFun token {mint[:8]}... still on bonding curve")
                is_pumpfun_bonding = True
        
        # Check position for PumpFun flag
        if mint in OPEN_POSITIONS:
            position = OPEN_POSITIONS[mint]
            if position.get("is_pumpfun", False):
                # Double check if still on bonding curve
                if not is_pumpfun_bonding:
                    lp_data = await get_liquidity_and_ownership(mint)
                    if not lp_data or lp_data.get("liquidity", 0) < 1.0:
                        is_pumpfun_bonding = True

        # Execute sell
        if is_pumpfun_bonding:
            # Try Jupiter first in case it migrated
            logging.info(f"[Sell] PumpFun token, probing Jupiter")
            sell_res = await execute_jupiter_sell(mint, amount, slippage_bps)
            if sell_res.get("ok"):
                sig = sell_res.get("sig")
                await notify("sell", f"✅ Sold {percentage}% of {mint[:8]}... via Jupiter\nTX: https://solscan.io/tx/{sig}")
                log_trade(mint, f"SELL {percentage}%", 0, amount)
                increment_stat("sells_executed", 1)
                return True
            else:
                logging.warning(f"[Sell] Jupiter sell failed: {sell_res.get('reason')}")

            # Wait for migration
            max_wait_sec, poll_every = 90, 5
            waited = 0
            logging.info(f"[Sell] Waiting up to {max_wait_sec}s for migration")
            while waited < max_wait_sec:
                try:
                    if await detect_pumpfun_migration(mint):
                        logging.info(f"[Sell] Migration detected, retrying Jupiter")
                        sell_res = await execute_jupiter_sell(mint, amount, slippage_bps)
                        if sell_res.get("ok"):
                            sig = sell_res.get("sig")
                            await notify("sell", f"✅ Sold {percentage}% of {mint[:8]}... via Jupiter after migration\nTX: https://solscan.io/tx/{sig}")
                            log_trade(mint, f"SELL {percentage}%", 0, amount)
                            increment_stat("sells_executed", 1)
                            return True
                except Exception as e:
                    logging.debug(f"[Sell] Migration check error: {e}")
                await asyncio.sleep(poll_every)
                waited += poll_every

            logging.error(f"[Sell] No sell route for {mint[:8]}... after waiting {max_wait_sec}s")
            return False
        else:
            # Non-bonding (regular or migrated) - use Jupiter
            logging.info(f"[Sell] Using Jupiter for sell")
            sell_res = await execute_jupiter_sell(mint, amount, slippage_bps)
            
            if sell_res.get("ok"):
                sig = sell_res.get("sig")
                await notify("sell",
                    f"✅ Sold {percentage}% of {mint[:8]}... via Jupiter\n"
                    f"TX: https://solscan.io/tx/{sig}"
                )
                log_trade(mint, f"SELL {percentage}%", 0, amount)
                increment_stat("sells_executed", 1)
                return True
            else:
                reason = sell_res.get("reason", "UNKNOWN")
                logging.error(f"[Sell] Jupiter sell failed: {reason}")
                return False
        
    except Exception as e:
        logging.error(f"Sell failed for {mint[:8]}...: {e}")
        log_skipped_token(mint, f"Sell failed: {e}")
        return False

async def get_token_decimals(mint: str) -> int:
    """Get token decimals from blockchain with caching"""
    if mint in TOKEN_DECIMALS_CACHE:
        return TOKEN_DECIMALS_CACHE[mint]
    
    if mint in KNOWN_TOKEN_DECIMALS:
        TOKEN_DECIMALS_CACHE[mint] = KNOWN_TOKEN_DECIMALS[mint]
        return KNOWN_TOKEN_DECIMALS[mint]
    
    try:
        mint_pubkey = Pubkey.from_string(mint)
        mint_info = rpc.get_account_info(mint_pubkey)
        if mint_info and mint_info.value:
            data = mint_info.value.data
            if isinstance(data, list) and len(data) > 0:
                import base64
                if isinstance(data[0], str):
                    decoded = base64.b64decode(data[0])
                else:
                    decoded = bytes(data[0])
                
                if len(decoded) > 44:
                    decimals = decoded[44]
                    
                    if 0 <= decimals <= 18:
                        TOKEN_DECIMALS_CACHE[mint] = decimals
                        logging.info(f"[Decimals] Token {mint[:8]}... has {decimals} decimals")
                        return decimals
                    else:
                        logging.warning(f"[Decimals] Invalid decimals {decimals} for {mint[:8]}...")
                        return 9
    except Exception as e:
        logging.warning(f"[Decimals] Could not get decimals for {mint[:8]}...: {e}")
    
    logging.warning(f"[Decimals] Using DEFAULT 9 decimals for {mint[:8]}...")
    return 9

async def get_token_price_usd(mint: str) -> Optional[float]:
    """Get current token price"""
    try:
        STABLECOIN_MINTS = {
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
            "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
        }
        
        if mint in STABLECOIN_MINTS:
            logging.info(f"[Price] {STABLECOIN_MINTS[mint]} stablecoin, returning $1.00")
            return 1.0
        
        actual_decimals = await get_token_decimals(mint)
        logging.info(f"[Price] Token {mint[:8]}... using {actual_decimals} decimals")
        
        # Try DexScreener
        try:
            dex_url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
            response = await HTTPManager.request(dex_url)
            if response:
                data = response.json()
                if "pairs" in data and len(data["pairs"]) > 0:
                    pairs = sorted(data["pairs"], key=lambda x: float(x.get("liquidity", {}).get("usd", 0)), reverse=True)
                    if pairs[0].get("priceUsd"):
                        price = float(pairs[0]["priceUsd"])
                        if price > 0:
                            logging.info(f"[Price] {mint[:8]}... = ${price:.8f} (DexScreener)")
                            return price
        except Exception as e:
            logging.debug(f"[Price] DexScreener error: {e}")
        
        # Try Birdeye
        if CONFIG.BIRDEYE_API_KEY:
            try:
                url = f"https://public-api.birdeye.so/defi/price?address={mint}"
                headers = {"X-API-KEY": CONFIG.BIRDEYE_API_KEY}
                response = await HTTPManager.request(url, headers=headers)
                
                if response:
                    data = response.json()
                    if "data" in data and "value" in data["data"]:
                        price = float(data["data"]["value"])
                        if price > 0:
                            logging.info(f"[Price] {mint[:8]}... = ${price:.8f} (Birdeye)")
                            return price
            except Exception as e:
                logging.debug(f"[Price] Birdeye error: {e}")
        
        # Try Jupiter Price API
        try:
            url = f"https://price.jup.ag/v4/price?ids={mint}"
            response = await HTTPManager.request(url, timeout=5)
            if response:
                data = response.json()
                if mint in data.get("data", {}):
                    price_data = data["data"][mint]
                    price = float(price_data.get("price", 0))
                    if price > 0:
                        logging.info(f"[Price] {mint[:8]}... = ${price:.8f} (Jupiter)")
                        return price
        except Exception as e:
            logging.debug(f"[Price] Jupiter Price API error: {e}")
        
        # Last resort: compute from quote
        try:
            quote = await get_jupiter_quote(
                "So11111111111111111111111111111111111111112",
                mint,
                int(1e9)  # 1 SOL
            )
            
            if quote and quote.get("outAmount"):
                tokens_received = float(quote["outAmount"]) / (10 ** actual_decimals)
                if tokens_received > 0:
                    price = CONFIG.SOL_PRICE_USD / tokens_received
                    logging.info(f"[Price] {mint[:8]}... = ${price:.8f} (computed)")
                    return price
        except Exception as e:
            logging.debug(f"[Price] Quote error: {e}")
        
        logging.warning(f"[Price] Could not get price for {mint[:8]}...")
        return None
        
    except Exception as e:
        logging.error(f"[Price] Unexpected error for {mint}: {e}")
        return None

# STOP-LOSS MONITOR - Including wait_and_auto_sell functions
async def wait_and_auto_sell(mint: str):
    """Monitor position with integrated stop-loss engine"""
    try:
        if mint not in OPEN_POSITIONS:
            logging.warning(f"No position found for {mint}")
            return
            
        position = OPEN_POSITIONS[mint]
        
        # Get stop data if exists
        stop_data = STOPS.get(mint)
        if not stop_data:
            # Legacy path - register stop now with real balance
            entry_price = position.get("entry_price")
            if not entry_price:
                entry_price = await get_token_price_usd(mint)
                if not entry_price:
                    logging.warning(f"Could not get entry price for {mint}, using timer-based fallback")
                    await wait_and_auto_sell_timer_based(mint)
                    return
            
            # Get REAL token balance
            from spl.token.constants import TOKEN_PROGRAM_ID
            owner = keypair.pubkey()
            token_account = get_associated_token_address(owner, Pubkey.from_string(mint))
            
            try:
                response = rpc.get_token_account_balance(token_account)
                if response and response.value:
                    size_tokens = int(response.value.amount)
                else:
                    size_tokens = position.get("expected_token_amount", 0)
            except:
                size_tokens = position.get("expected_token_amount", 0)
            
            register_stop(mint, {
                "entry_price": entry_price,
                "size_tokens": size_tokens,
                "stop_price": entry_price * (1 - CONFIG.STOP_LOSS_PCT),
                "slippage_bps": CONFIG.STOP_MAX_SLIPPAGE_BPS,
                "state": "ARMED",
                "last_alert": 0,
                "first_no_route": 0,
                "stuck_reason": None,
                "emergency_attempts": 0
            })
            stop_data = STOPS[mint]
        
        # Determine token type and strategy
        is_pumpfun = mint in pumpfun_tokens
        is_trending = mint in trending_tokens
        is_migration = position.get("is_migration", False)
        
        # Select appropriate targets
        if is_migration:
            targets = [5.0, 15.0, 30.0]
            sell_percents = [30, 30, 40]
            strategy_name = "MIGRATION"
            min_hold_time = 60
        elif is_pumpfun and CONFIG.PUMPFUN_USE_MOON_STRATEGY:
            targets = [CONFIG.PUMPFUN_TAKE_PROFIT_1, CONFIG.PUMPFUN_TAKE_PROFIT_2, CONFIG.PUMPFUN_TAKE_PROFIT_3]
            sell_percents = [CONFIG.PUMPFUN_SELL_PERCENT_1, CONFIG.PUMPFUN_SELL_PERCENT_2, CONFIG.PUMPFUN_MOON_BAG]
            strategy_name = "MOON SHOT"
            min_hold_time = CONFIG.NO_SELL_FIRST_MINUTES * 60
        elif is_trending and CONFIG.TRENDING_USE_CUSTOM:
            targets = [CONFIG.TRENDING_TAKE_PROFIT_1, CONFIG.TRENDING_TAKE_PROFIT_2, CONFIG.TRENDING_TAKE_PROFIT_3]
            sell_percents = [30, 35, 35]
            strategy_name = "TRENDING"
            min_hold_time = 60
        else:
            targets = [CONFIG.TAKE_PROFIT_1, CONFIG.TAKE_PROFIT_2, CONFIG.TAKE_PROFIT_3]
            sell_percents = [CONFIG.SELL_PERCENT_1, CONFIG.SELL_PERCENT_2, CONFIG.SELL_PERCENT_3]
            strategy_name = "STANDARD"
            min_hold_time = 0
        
        entry_price = stop_data["entry_price"]
        position["entry_price"] = entry_price
        position["highest_price"] = entry_price
        
        logging.info(
            f"[{mint[:8]}] Monitoring with {strategy_name} strategy\n"
            f"Entry: ${entry_price:.6f}, Stop: ${stop_data['stop_price']:.6f}"
        )
        
        # Monitor loop
        start_time = time.time()
        last_price_check = 0
        last_status_log = 0
        max_sell_attempts = 3
        sell_attempts = {"profit1": 0, "profit2": 0, "profit3": 0, "stop_loss": 0}
        
        while time.time() - start_time < CONFIG.MAX_HOLD_TIME_SEC:
            try:
                # Check stop-loss more frequently
                if time.time() - last_price_check < CONFIG.STOP_CHECK_INTERVAL_SEC:
                    await asyncio.sleep(0.5)
                    continue
                    
                last_price_check = time.time()
                
                # Get current token balance
                owner = keypair.pubkey()
                token_account = get_associated_token_address(owner, Pubkey.from_string(mint))
                
                current_balance = 0
                max_balance_retries = 10
                for retry in range(max_balance_retries):
                    try:
                        response = rpc.get_token_account_balance(token_account)
                        if response and response.value:
                            current_balance = int(response.value.amount)
                            if current_balance > 0:
                                position["expected_token_amount"] = current_balance
                                if mint in STOPS:
                                    STOPS[mint]["size_tokens"] = current_balance
                                break
                    except:
                        pass
                    await asyncio.sleep(0.3)
                
                if current_balance == 0:
                    logging.info(f"[{mint[:8]}] Position fully closed")
                    break
                
                # Get current price
                current_price = await get_token_price_usd(mint)
                if not current_price:
                    # Try Jupiter quote as fallback
                    quote = await get_jupiter_quote(mint, "So11111111111111111111111111111111111111112", min(current_balance, int(current_balance * 0.1)))
                    if quote and quote.get("outAmount"):
                        sol_out = int(quote["outAmount"]) / 1e9
                        if sol_out > 0 and current_balance > 0:
                            current_price = (sol_out * CONFIG.SOL_PRICE_USD * 10) / (current_balance / (10 ** await get_token_decimals(mint)))
                    
                    if not current_price:
                        await asyncio.sleep(CONFIG.STOP_CHECK_INTERVAL_SEC)
                        continue
                
                profit_multiplier = current_price / entry_price
                profit_percent = (profit_multiplier - 1) * 100
                time_held = time.time() - start_time
                
                # Update highest price
                if current_price > position["highest_price"]:
                    position["highest_price"] = current_price
                    logging.info(f"[{mint[:8]}] New high: ${current_price:.6f} ({profit_multiplier:.2f}x)")
                
                # Check stop-loss FIRST
                if current_price <= stop_data["stop_price"] and stop_data["state"] == "ARMED":
                    stop_data["state"] = "TRIGGERED"
                    logging.info(f"🛑 STOP TRIGGERED for {mint[:8]}... @ ${current_price:.6f}")
                    await notify("stop_triggered", f"🛑 STOP TRIGGERED {mint[:8]}... @ ${current_price:.6f} (stop: ${stop_data['stop_price']:.6f})")
                
                if stop_data["state"] == "TRIGGERED" and sell_attempts["stop_loss"] < max_sell_attempts:
                    sell_attempts["stop_loss"] += 1
                    stop_data["state"] = "SUBMITTING"
                    
                    if sell_attempts["stop_loss"] <= 3:
                        if await sell_token(mint, percentage=100, slippage_bps=CONFIG.STOP_MAX_SLIPPAGE_BPS):
                            stop_data["state"] = "FILLED"
                            await notify("stop_filled",
                                f"✅ STOP FILLED {mint[:8]}...\n"
                                f"Price: ${current_price:.6f}\n"
                                f"Loss: {profit_percent:.1f}%"
                            )
                            break
                        else:
                            stop_data["state"] = "TRIGGERED"
                    
                    elif sell_attempts["stop_loss"] == 4:
                        stop_data["emergency_attempts"] = stop_data.get("emergency_attempts", 0) + 1
                        if stop_data["emergency_attempts"] == 1:
                            logging.warning(f"Trying emergency slippage {CONFIG.STOP_EMERGENCY_SLIPPAGE_BPS} bps")
                            
                            if await sell_token(mint, percentage=100, slippage_bps=CONFIG.STOP_EMERGENCY_SLIPPAGE_BPS):
                                stop_data["state"] = "FILLED"
                                await notify("stop_filled", f"✅ STOP FILLED {mint[:8]}... (emergency slippage)")
                                break
                            else:
                                stop_data["state"] = "TRIGGERED"
                                stop_data["stuck_reason"] = "SELL_FAILED"
                
                # Check minimum hold time
                if stop_data["state"] != "TRIGGERED" and is_pumpfun and time_held < min_hold_time:
                    logging.debug(f"[{mint[:8]}] Holding for {min_hold_time/60:.0f} mins minimum (PumpFun)")
                    await asyncio.sleep(CONFIG.STOP_CHECK_INTERVAL_SEC)
                    continue
                
                # Check trailing stop
                if stop_data["state"] != "TRIGGERED" and profit_multiplier >= CONFIG.TRAILING_STOP_ACTIVATION:
                    drop_from_high = (position["highest_price"] - current_price) / position["highest_price"] * 100
                    if drop_from_high >= CONFIG.TRAILING_STOP_PERCENT and len(position["sold_stages"]) > 0:
                        logging.info(f"[{mint[:8]}] Trailing stop triggered! Down {drop_from_high:.1f}% from peak")
                        if await sell_token(mint, percentage=100):
                            await notify("sell",
                                f"⛔ Trailing stop for {mint[:8]}!\n"
                                f"Dropped {drop_from_high:.1f}% from ${position['highest_price']:.6f}\n"
                                f"Sold at ${current_price:.6f} ({profit_multiplier:.1f}x)"
                            )
                            break
                
                # Check profit targets
                if stop_data["state"] != "TRIGGERED":
                    if profit_multiplier >= targets[0] and "profit1" not in position["sold_stages"] and sell_attempts["profit1"] < max_sell_attempts:
                        sell_attempts["profit1"] += 1
                        if await sell_token(mint, percentage=sell_percents[0]):
                            position["sold_stages"].add("profit1")
                            await notify("sell",
                                f"💰 Hit {targets[0]}x for {mint[:8]}!\n"
                                f"Price: ${current_price:.6f}\n"
                                f"Sold {sell_percents[0]}%"
                            )
                    
                    if profit_multiplier >= targets[1] and "profit2" not in position["sold_stages"] and sell_attempts["profit2"] < max_sell_attempts:
                        sell_attempts["profit2"] += 1
                        if await sell_token(mint, percentage=sell_percents[1]):
                            position["sold_stages"].add("profit2")
                            await notify("sell",
                                f"🚀 Hit {targets[1]}x for {mint[:8]}!\n"
                                f"Price: ${current_price:.6f}\n"
                                f"Sold {sell_percents[1]}%"
                            )
                    
                    if profit_multiplier >= targets[2] and "profit3" not in position["sold_stages"] and sell_attempts["profit3"] < max_sell_attempts:
                        sell_attempts["profit3"] += 1
                        if await sell_token(mint, percentage=sell_percents[2]):
                            position["sold_stages"].add("profit3")
                            await notify("sell",
                                f"🌙 Hit {targets[2]}x for {mint[:8]}!\n"
                                f"Price: ${current_price:.6f}\n"
                                f"Sold {sell_percents[2]}% - MOONBAG!"
                            )
                
                # Log status periodically
                if time.time() - last_status_log >= 60:
                    logging.info(
                        f"[{mint[:8]}] Price: ${current_price:.6f} ({profit_multiplier:.2f}x) | "
                        f"State: {stop_data['state']} | Sold: {position['sold_stages']}"
                    )
                    last_status_log = time.time()
                
                # Only exit if we sold everything
                if len(position["sold_stages"]) >= 3 and sell_percents[2] == 100:
                    logging.info(f"[{mint[:8]}] All profit targets hit, position fully closed")
                    break
                    
            except Exception as e:
                logging.error(f"Error monitoring {mint}: {e}")
                await asyncio.sleep(CONFIG.STOP_CHECK_INTERVAL_SEC)
        
        # Time limit reached
        if time.time() - start_time >= CONFIG.MAX_HOLD_TIME_SEC:
            if len(position["sold_stages"]) >= 3:
                logging.info(f"[{mint[:8]}] Max hold time reached, keeping moonbag")
            else:
                logging.info(f"[{mint[:8]}] Max hold time reached, force selling")
                if await sell_token(mint, percentage=100):
                    current_price = await get_token_price_usd(mint) or entry_price
                    profit_percent = ((current_price / entry_price) - 1) * 100
                    await notify("sell",
                        f"⏰ Max hold time for {mint[:8]}\n"
                        f"Force sold after {CONFIG.MAX_HOLD_TIME_SEC/60:.0f} min\n"
                        f"P&L: {profit_percent:+.1f}%"
                    )
        
        # Clean up
        if mint in STOPS:
            del STOPS[mint]
        if mint in OPEN_POSITIONS:
            owner = keypair.pubkey()
            token_account = get_associated_token_address(owner, Pubkey.from_string(mint))
            try:
                response = rpc.get_token_account_balance(token_account)
                if not response or not response.value or int(response.value.amount) == 0:
                    del OPEN_POSITIONS[mint]
            except:
                if len(position.get("sold_stages", set())) >= 3 and sell_percents[2] == 100:
                    del OPEN_POSITIONS[mint]
            
    except Exception as e:
        logging.error(f"Auto-sell error for {mint}: {e}")
        if mint in STOPS:
            del STOPS[mint]

async def wait_and_auto_sell_timer_based(mint: str):
    """Fallback timer-based selling if price feed fails"""
    try:
        if mint not in OPEN_POSITIONS:
            return
            
        position = OPEN_POSITIONS[mint]
        
        start_time = time.time()
        max_duration = 600
        max_sell_attempts = 3
        sell_attempts = {"2x": 0, "5x": 0, "10x": 0}
        
        while time.time() - start_time < max_duration:
            try:
                elapsed = time.time() - start_time
                
                if elapsed > 30 and "2x" not in position["sold_stages"] and sell_attempts["2x"] < max_sell_attempts:
                    sell_attempts["2x"] += 1
                    if await sell_token(mint, percentage=AUTO_SELL_PERCENT_2X):
                        position["sold_stages"].add("2x")
                        await notify("sell", f"📈 Sold {AUTO_SELL_PERCENT_2X}% at 30s for {mint[:8]}...")
                    elif sell_attempts["2x"] >= max_sell_attempts:
                        position["sold_stages"].add("2x")
                
                if elapsed > 120 and "5x" not in position["sold_stages"] and sell_attempts["5x"] < max_sell_attempts:
                    sell_attempts["5x"] += 1
                    if await sell_token(mint, percentage=AUTO_SELL_PERCENT_5X):
                        position["sold_stages"].add("5x")
                        await notify("sell", f"🚀 Sold {AUTO_SELL_PERCENT_5X}% at 2min for {mint[:8]}...")
                    elif sell_attempts["5x"] >= max_sell_attempts:
                        position["sold_stages"].add("5x")
                
                if elapsed > 300 and "10x" not in position["sold_stages"] and sell_attempts["10x"] < max_sell_attempts:
                    sell_attempts["10x"] += 1
                    if await sell_token(mint, percentage=AUTO_SELL_PERCENT_10X):
                        position["sold_stages"].add("10x")
                        await notify("sell", f"🌙 Sold {AUTO_SELL_PERCENT_10X}% at 5min for {mint[:8]}...")
                    elif sell_attempts["10x"] >= max_sell_attempts:
                        position["sold_stages"].add("10x")
                
                if len(position["sold_stages"]) >= 3 and AUTO_SELL_PERCENT_10X < 100:
                    logging.info(f"Timer targets hit, keeping moonbag for {mint[:8]}...")
                    return
                    
                await asyncio.sleep(10)
                
            except Exception as e:
                logging.error(f"Timer-based monitoring error for {mint}: {e}")
                await asyncio.sleep(10)
        
        if mint in OPEN_POSITIONS and AUTO_SELL_PERCENT_10X == 100:
            del OPEN_POSITIONS[mint]
            
    except Exception as e:
        logging.error(f"Timer-based auto-sell error for {mint}: {e}")
        if mint in OPEN_POSITIONS and AUTO_SELL_PERCENT_10X == 100:
            del OPEN_POSITIONS[mint]

async def check_pumpfun_token_status(mint: str) -> Optional[Dict[str, Any]]:
    """Check PumpFun token status and market cap"""
    try:
        url = f"https://frontend-api.pump.fun/coins/{mint}"
        response = await HTTPManager.request(url, timeout=5)
        if response:
            data = response.json()
            market_cap = data.get("usd_market_cap", 0)
            graduated = market_cap >= 69420
            pool_address = data.get("raydium_pool")
            
            return {
                "market_cap": market_cap,
                "graduated": graduated,
                "pool_address": pool_address,
                "progress": (market_cap / 69420) * 100 if market_cap < 69420 else 100
            }
    except Exception as e:
        logging.debug(f"PumpFun status check error: {e}")
    
    return None

async def detect_pumpfun_migration(mint: str) -> bool:
    """Detect if a PumpFun token has migrated to Raydium/Jupiter"""
    try:
        pf_status = await check_pumpfun_token_status(mint)
        if not pf_status or not pf_status.get("graduated"):
            return False
        
        pool = raydium.find_pool_realtime(mint)
        
        if pool:
            logging.info(f"[Migration] PumpFun token {mint[:8]}... has migrated to Raydium!")
            return True
            
        try:
            url = f"https://price.jup.ag/v4/price?ids={mint}"
            response = await HTTPManager.request(url, timeout=5)
            if response:
                data = response.json()
                if mint in data.get("data", {}):
                    logging.info(f"[Migration] PumpFun token {mint[:8]}... found on Jupiter!")
                    return True
        except:
            pass
            
    except Exception as e:
        logging.error(f"Migration detection error: {e}")
    
    return False

# Export backward compatibility values
BUY_AMOUNT_SOL = CONFIG.BUY_AMOUNT_SOL
USE_DYNAMIC_SIZING = CONFIG.USE_DYNAMIC_SIZING

# Export for use in sniper_logic
__all__ = [
    'is_valid_mint',
    'buy_token',
    'sell_token',
    'log_skipped_token',
    'send_telegram_alert',
    'send_telegram_batch',
    'get_trending_mints',
    'wait_and_auto_sell',
    'get_liquidity_and_ownership',
    'is_bot_running',
    'start_bot',
    'stop_bot',
    'keypair',
    'BUY_AMOUNT_SOL',
    'BROKEN_TOKENS',
    'mark_broken_token',
    'daily_stats_reset_loop',
    'update_last_activity',
    'increment_stat',
    'record_skip',
    'listener_status',
    'last_seen_token',
    'get_wallet_summary',
    'get_bot_status_message',
    'check_pumpfun_token_status',
    'detect_pumpfun_migration',
    'pumpfun_tokens',
    'trending_tokens',
    'get_token_price_usd',
    'get_token_decimals',
    'cleanup_wsol_on_failure',
    'OPEN_POSITIONS',
    'daily_stats',
    'BLACKLIST',
    'raydium',
    'rpc',
    'wait_and_auto_sell_timer_based',
    'get_dynamic_position_size',
    'get_minimum_liquidity_required',
    'USE_DYNAMIC_SIZING',
    'evaluate_pumpfun_opportunity',
    'is_fresh_token',
    'verify_token_age_on_chain',
    'is_pumpfun_launch',
    'is_pumpfun_token',
    'CONFIG',
    'register_stop',
    'STOPS',
    'notify',
    'HTTPManager',
    'PUMPFUN_BUY_FN',
    '_register_successful_buy'
]
