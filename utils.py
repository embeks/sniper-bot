# utils.py - COMPLETE PRODUCTION READY VERSION WITH SCALING AND DECIMAL FIX
import os
import json
import logging
import httpx
import asyncio
import time
import csv
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from dotenv import load_dotenv
import base64
from solders.transaction import VersionedTransaction
import certifi  # For TLS verification

# Solana imports
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from spl.token.instructions import get_associated_token_address, close_account, CloseAccountParams

# Import Raydium client
from raydium_aggregator import RaydiumAggregatorClient

# Setup
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Environment variables
RPC_URL = os.getenv("RPC_URL")
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_USER_ID = os.getenv("TELEGRAM_USER_ID")
BUY_AMOUNT_SOL = float(os.getenv("BUY_AMOUNT_SOL", 0.03))
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")
JUPITER_BASE_URL = os.getenv("JUPITER_BASE_URL", "https://quote-api.jup.ag")
SELL_MULTIPLIERS = os.getenv("SELL_MULTIPLIERS", "2,5,10").split(",")
SELL_TIMEOUT_SEC = int(os.getenv("SELL_TIMEOUT_SEC", 300))
RUG_LP_THRESHOLD = float(os.getenv("RUG_LP_THRESHOLD", 3.0))
BLACKLISTED_TOKENS = os.getenv("BLACKLISTED_TOKENS", "").split(",") if os.getenv("BLACKLISTED_TOKENS") else []
HELIUS_API = os.getenv("HELIUS_API")

# Parse sell percentages
AUTO_SELL_PERCENT_2X = 50
AUTO_SELL_PERCENT_5X = 25
AUTO_SELL_PERCENT_10X = 25

# Profit-based trading configuration
TAKE_PROFIT_1 = float(os.getenv("TAKE_PROFIT_1", 2.0))
TAKE_PROFIT_2 = float(os.getenv("TAKE_PROFIT_2", 5.0))
TAKE_PROFIT_3 = float(os.getenv("TAKE_PROFIT_3", 10.0))
SELL_PERCENT_1 = float(os.getenv("SELL_PERCENT_1", 50))
SELL_PERCENT_2 = float(os.getenv("SELL_PERCENT_2", 25))
SELL_PERCENT_3 = float(os.getenv("SELL_PERCENT_3", 25))
STOP_LOSS_PERCENT = float(os.getenv("STOP_LOSS_PERCENT", 50))
TRAILING_STOP_PERCENT = float(os.getenv("TRAILING_STOP_PERCENT", 20))
MAX_HOLD_TIME_SEC = int(os.getenv("MAX_HOLD_TIME_SEC", 3600))
PRICE_CHECK_INTERVAL_SEC = int(os.getenv("PRICE_CHECK_INTERVAL_SEC", 10))

# PumpFun configuration
PUMPFUN_USE_MOON_STRATEGY = os.getenv("PUMPFUN_USE_MOON_STRATEGY", "true").lower() == "true"
PUMPFUN_TAKE_PROFIT_1 = float(os.getenv("PUMPFUN_TAKE_PROFIT_1", 10.0))
PUMPFUN_TAKE_PROFIT_2 = float(os.getenv("PUMPFUN_TAKE_PROFIT_2", 25.0))
PUMPFUN_TAKE_PROFIT_3 = float(os.getenv("PUMPFUN_TAKE_PROFIT_3", 50.0))
PUMPFUN_SELL_PERCENT_1 = float(os.getenv("PUMPFUN_SELL_PERCENT_1", 20))
PUMPFUN_SELL_PERCENT_2 = float(os.getenv("PUMPFUN_SELL_PERCENT_2", 30))
PUMPFUN_MOON_BAG = float(os.getenv("PUMPFUN_MOON_BAG", 50))
NO_SELL_FIRST_MINUTES = int(os.getenv("NO_SELL_FIRST_MINUTES", 30))
TRAILING_STOP_ACTIVATION = float(os.getenv("TRAILING_STOP_ACTIVATION", 5.0))

# Trending tokens configuration
TRENDING_USE_CUSTOM = os.getenv("TRENDING_USE_CUSTOM", "false").lower() == "true"
TRENDING_TAKE_PROFIT_1 = float(os.getenv("TRENDING_TAKE_PROFIT_1", 3.0))
TRENDING_TAKE_PROFIT_2 = float(os.getenv("TRENDING_TAKE_PROFIT_2", 8.0))
TRENDING_TAKE_PROFIT_3 = float(os.getenv("TRENDING_TAKE_PROFIT_3", 15.0))

# Configuration flags
OVERRIDE_DECIMALS_TO_9 = os.getenv("OVERRIDE_DECIMALS_TO_9", "false").lower() == "true"
IGNORE_JUPITER_PRICE_FIELD = os.getenv("IGNORE_JUPITER_PRICE_FIELD", "false").lower() == "true"
LP_CHECK_TIMEOUT = int(os.getenv("LP_CHECK_TIMEOUT", 3))

# Scaling configuration
USE_DYNAMIC_SIZING = os.getenv("USE_DYNAMIC_SIZING", "true").lower() == "true"
SCALE_WITH_BALANCE = os.getenv("SCALE_WITH_BALANCE", "true").lower() == "true"
MIGRATION_BOOST_MULTIPLIER = float(os.getenv("MIGRATION_BOOST_MULTIPLIER", 2.0))
TRENDING_BOOST_MULTIPLIER = float(os.getenv("TRENDING_BOOST_MULTIPLIER", 1.5))

# Initialize clients
rpc = Client(RPC_URL, commitment=Confirmed)
raydium = RaydiumAggregatorClient(RPC_URL)

# Load wallet
import ast
try:
    if SOLANA_PRIVATE_KEY and SOLANA_PRIVATE_KEY.startswith("["):
        private_key_array = ast.literal_eval(SOLANA_PRIVATE_KEY)
        if len(private_key_array) == 64:
            keypair = Keypair.from_bytes(bytes(private_key_array))
        else:
            keypair = Keypair.from_seed(bytes(private_key_array[:32]))
    else:
        keypair = Keypair.from_base58_string(SOLANA_PRIVATE_KEY)
except Exception as e:
    raise ValueError(f"Failed to load wallet from SOLANA_PRIVATE_KEY: {e}")

wallet_pubkey = str(keypair.pubkey())

# Use TELEGRAM_CHAT_ID but also support TELEGRAM_USER_ID
if not TELEGRAM_CHAT_ID and TELEGRAM_USER_ID:
    TELEGRAM_CHAT_ID = TELEGRAM_USER_ID

# Global state
OPEN_POSITIONS = {}
BROKEN_TOKENS = set()
BOT_RUNNING = True
BLACKLIST_FILE = "blacklist.json"
TRADES_CSV_FILE = "trades.csv"
BLACKLIST = set(BLACKLISTED_TOKENS) if BLACKLISTED_TOKENS else set()

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
        "lp_timeout": 0
    }
}

# Status tracking
listener_status = {"Raydium": "OFFLINE", "Jupiter": "OFFLINE", "PumpFun": "OFFLINE", "Moonshot": "OFFLINE"}
last_activity = time.time()
last_seen_token = {"Raydium": time.time(), "Jupiter": time.time(), "PumpFun": time.time(), "Moonshot": time.time()}

# Telegram batching
telegram_batch = []
telegram_batch_time = 0
telegram_batch_interval = 1.0
telegram_last_sent = 0
telegram_min_interval = 0.5

# Track PumpFun and trending tokens
pumpfun_tokens = {}
trending_tokens = set()

# Known token decimals - EXPANDED LIST WITH COMMON TOKENS
KNOWN_TOKEN_DECIMALS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": 6,  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": 6,  # USDT
    "So11111111111111111111111111111111111111112": 9,   # WSOL
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs": 8,  # WETH
    "7dHbWXmci3dT8UFYWYZweBLXgycu7Y3iL6trKn1Y7ARj": 9,  # stSOL
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So": 9,   # mSOL
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263": 5,  # Bonk (note: 5 decimals!)
    "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R": 9,  # RAY
}

# Cache for token decimals to avoid repeated RPC calls
TOKEN_DECIMALS_CACHE = {}

# ============================================
# SCALING FUNCTIONS
# ============================================

async def get_dynamic_position_size(mint: str, pool_liquidity_sol: float, is_migration: bool = False) -> float:
    """Calculate position size based on current balance and token type"""
    try:
        # Get current balance
        balance = rpc.get_balance(keypair.pubkey()).value / 1e9
        balance_usd = balance * 150  # Assume SOL = $150
        
        # Base position sizing by bankroll
        if balance_usd < 500:
            base_percent = 0.01  # 1% when small ($300 starting)
        elif balance_usd < 1500:
            base_percent = 0.015  # 1.5% when growing
        elif balance_usd < 5000:
            base_percent = 0.02  # 2% when established
        elif balance_usd < 10000:
            base_percent = 0.025  # 2.5% when large
        else:
            base_percent = 0.03  # 3% when massive (10k+ target)
        
        position = balance * base_percent
        
        # Boost for special situations
        if is_migration:
            position *= MIGRATION_BOOST_MULTIPLIER  # Double for PumpFun migrations
            logging.info(f"[Position] Migration detected, boosting {MIGRATION_BOOST_MULTIPLIER}x to {position:.3f} SOL")
        
        if mint in trending_tokens:
            position *= TRENDING_BOOST_MULTIPLIER  # 50% boost for trending
            logging.info(f"[Position] Trending token, boosting {TRENDING_BOOST_MULTIPLIER}x to {position:.3f} SOL")
        
        # Apply limits
        position = max(0.01, position)  # Minimum 0.01 SOL
        position = min(position, pool_liquidity_sol * 0.01)  # Max 1% of pool
        position = min(position, balance * 0.05)  # Max 5% of total balance
        position = min(position, 1.0)  # Hard cap at 1 SOL per trade
        
        logging.info(f"[Position] Balance: {balance:.2f} SOL (${balance_usd:.0f}) -> Size: {position:.3f} SOL")
        return position
        
    except Exception as e:
        logging.error(f"Dynamic sizing error: {e}")
        return float(os.getenv("BUY_AMOUNT_SOL", 0.03))

def get_minimum_liquidity_required(balance_sol: float = None) -> float:
    """Scale liquidity requirements with balance"""
    try:
        if balance_sol is None:
            balance_sol = rpc.get_balance(keypair.pubkey()).value / 1e9
        
        balance_usd = balance_sol * 150
        
        if balance_usd < 500:
            return 3.0  # 3 SOL minimum when starting
        elif balance_usd < 1500:
            return 5.0  # 5 SOL when growing
        elif balance_usd < 5000:
            return 10.0  # 10 SOL when established
        elif balance_usd < 10000:
            return 20.0  # 20 SOL when large
        else:
            return 30.0  # 30 SOL when targeting 10k+
    except:
        return float(os.getenv("RUG_LP_THRESHOLD", 3.0))

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
    BROKEN_TOKENS.add(mint)
    log_skipped_token(mint, f"Marked as broken (error {error_code})")

def is_valid_mint(mint: str) -> bool:
    try:
        Pubkey.from_string(mint)
        return True
    except:
        return False

async def send_telegram_alert(message: str, retry_count: int = 3) -> bool:
    """Send alert to Telegram with rate limiting and proper TLS"""
    global telegram_last_sent
    
    try:
        # Rate limiting
        now = time.time()
        time_since_last = now - telegram_last_sent
        if time_since_last < telegram_min_interval:
            await asyncio.sleep(telegram_min_interval - time_since_last)
        
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        
        for attempt in range(retry_count):
            try:
                async with httpx.AsyncClient(timeout=10, verify=certifi.where()) as client:
                    response = await client.post(url, json=payload)
                    
                    if response.status_code == 200:
                        telegram_last_sent = time.time()
                        return True
                    elif response.status_code == 429:
                        try:
                            data = response.json()
                            retry_after = data.get("parameters", {}).get("retry_after", 5)
                            logging.warning(f"Telegram rate limit hit, waiting {retry_after}s")
                            await asyncio.sleep(retry_after)
                        except:
                            await asyncio.sleep(5)
                    
            except httpx.TimeoutError:
                if attempt < retry_count - 1:
                    await asyncio.sleep(1)
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
    global telegram_batch, telegram_batch_time
    
    current_time = time.time()
    
    telegram_batch.extend(lines)
    
    if telegram_batch_time == 0:
        telegram_batch_time = current_time
    
    if (current_time - telegram_batch_time > telegram_batch_interval or 
        len(telegram_batch) > 10):
        
        if telegram_batch:
            message = "\n".join(telegram_batch[:20])
            await send_telegram_alert(message)
            telegram_batch = []
            telegram_batch_time = 0

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
        balance_usd = balance * 150
        
        # Get position sizing info
        if USE_DYNAMIC_SIZING:
            test_position = balance * 0.015 if balance_usd < 1500 else balance * 0.02
            sizing_info = f"\n📏 Position Size: ~{test_position:.3f} SOL"
        else:
            sizing_info = f"\n📏 Fixed Size: {BUY_AMOUNT_SOL} SOL"
        
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
🚫 Broken Tokens: {len(BROKEN_TOKENS)}
💰 Min LP Filter: {get_minimum_liquidity_required()} SOL
🎯 Scaling: {'ON' if USE_DYNAMIC_SIZING else 'OFF'}
"""

async def get_liquidity_and_ownership(mint: str) -> Optional[Dict[str, Any]]:
    """Get accurate liquidity with timeout and proper validation"""
    try:
        async def _check_liquidity():
            sol_mint = "So11111111111111111111111111111111111111112"
            
            # Try Raydium first
            pool = raydium.find_pool_realtime(mint)
            
            if pool:
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
                        logging.warning(f"[LP Check] {mint[:8]}... has ZERO liquidity in Raydium pool")
                        return {"liquidity": 0}
            
            # Try Jupiter as fallback
            logging.info(f"[LP Check] No Raydium pool, checking Jupiter...")
            try:
                url = f"{JUPITER_BASE_URL}/v6/quote"
                params = {
                    "inputMint": sol_mint,
                    "outputMint": mint,
                    "amount": str(int(0.001 * 1e9)),
                    "slippageBps": "100",
                    "onlyDirectRoutes": "false"
                }
                
                async with httpx.AsyncClient(timeout=3, verify=certifi.where()) as client:
                    response = await client.get(url, params=params)
                    if response.status_code == 200:
                        quote = response.json()
                        
                        if quote.get("outAmount") and int(quote.get("outAmount", 0)) > 0:
                            price_impact = float(quote.get("priceImpactPct", 100))
                            
                            if price_impact < 1:
                                estimated_lp = 10.0
                            elif price_impact < 5:
                                estimated_lp = 3.0
                            elif price_impact < 10:
                                estimated_lp = 1.0
                            else:
                                estimated_lp = 0.1
                            
                            logging.info(f"[LP Check] {mint[:8]}... on Jupiter with estimated {estimated_lp:.2f} SOL liquidity")
                            return {"liquidity": estimated_lp}
                        else:
                            logging.info(f"[LP Check] No viable route on Jupiter for {mint[:8]}...")
                            return {"liquidity": 0}
            except Exception as e:
                logging.debug(f"[LP Check] Jupiter check failed: {e}")
            
            logging.info(f"[LP Check] No liquidity found for {mint[:8]}... on any DEX")
            return {"liquidity": 0}
        
        # Execute with timeout
        try:
            result = await asyncio.wait_for(_check_liquidity(), timeout=LP_CHECK_TIMEOUT)
            return result
        except asyncio.TimeoutError:
            logging.warning(f"[LP Check] Timeout after {LP_CHECK_TIMEOUT}s for {mint[:8]}...")
            record_skip("lp_timeout")
            return None
            
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
            
            # Reset stats
            daily_stats["tokens_scanned"] = 0
            daily_stats["snipes_attempted"] = 0
            daily_stats["snipes_succeeded"] = 0
            daily_stats["sells_executed"] = 0
            daily_stats["profit_sol"] = 0.0
            for key in daily_stats["skip_reasons"]:
                daily_stats["skip_reasons"][key] = 0
                
            await send_telegram_alert("📊 Daily stats reset")
        except Exception as e:
            logging.error(f"Stats reset error: {e}")
            await asyncio.sleep(3600)

async def get_jupiter_quote(input_mint: str, output_mint: str, amount: int, slippage_bps: int = 100):
    """Get swap quote from Jupiter API"""
    try:
        url = f"{JUPITER_BASE_URL}/v6/quote"
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(slippage_bps),
            "onlyDirectRoutes": "false",
            "asLegacyTransaction": "false"
        }
        
        async with httpx.AsyncClient(timeout=10, verify=certifi.where()) as client:
            response = await client.get(url, params=params)
            if response.status_code == 200:
                quote = response.json()
                
                in_amount = int(quote.get("inAmount", 0))
                out_amount = int(quote.get("outAmount", 0))
                other_amount_threshold = int(quote.get("otherAmountThreshold", 0))
                
                logging.info(f"[Jupiter] Quote: {in_amount/1e9:.4f} SOL -> {out_amount} tokens (min: {other_amount_threshold})")
                return quote
            else:
                logging.warning(f"[Jupiter] Quote request failed: {response.status_code}")
                return None
    except Exception as e:
        logging.error(f"[Jupiter] Error getting quote: {e}")
        return None

async def get_jupiter_swap_transaction(quote: dict, user_pubkey: str):
    """Get the swap transaction from Jupiter"""
    try:
        url = f"{JUPITER_BASE_URL}/v6/swap"
        
        body = {
            "quoteResponse": quote,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": 500000,
            "slippageBps": 300
        }
        
        async with httpx.AsyncClient(timeout=15, verify=certifi.where()) as client:
            response = await client.post(url, json=body)
            if response.status_code == 200:
                data = response.json()
                logging.info("[Jupiter] Swap transaction received")
                return data
            else:
                logging.warning(f"[Jupiter] Swap request failed: {response.status_code}")
                return None
    except Exception as e:
        logging.error(f"[Jupiter] Error getting swap transaction: {e}")
        return None

async def execute_jupiter_swap(mint: str, amount_lamports: int) -> Optional[str]:
    """Execute a swap using Jupiter"""
    try:
        input_mint = "So11111111111111111111111111111111111111112"
        output_mint = mint
        
        logging.info(f"[Jupiter] Getting quote for {amount_lamports/1e9:.4f} SOL -> {mint[:8]}...")
        quote = await get_jupiter_quote(input_mint, output_mint, amount_lamports)
        if not quote:
            logging.warning("[Jupiter] Failed to get quote")
            return None
        
        out_amount = int(quote.get("outAmount", 0))
        other_amount_threshold = int(quote.get("otherAmountThreshold", 0))
        
        if out_amount == 0 or other_amount_threshold == 0:
            logging.warning(f"[Jupiter] Invalid quote - outAmount: {out_amount}, threshold: {other_amount_threshold}")
            record_skip("no_route")
            return None
        
        logging.info("[Jupiter] Building swap transaction...")
        swap_data = await get_jupiter_swap_transaction(quote, wallet_pubkey)
        if not swap_data:
            logging.warning("[Jupiter] Failed to get swap transaction")
            return None
        
        tx_bytes = base64.b64decode(swap_data["swapTransaction"])
        tx = VersionedTransaction.from_bytes(tx_bytes)
        signed_tx = VersionedTransaction(tx.message, [keypair])
        
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
                            logging.info(f"[Jupiter] Transaction status: {status.value[0].confirmation_status}")
                            return sig
                        elif status.value[0].err:
                            logging.error(f"[Jupiter] Transaction failed: {status.value[0].err}")
                            await cleanup_wsol_on_failure()
                            return None
                except Exception as e:
                    logging.debug(f"[Jupiter] Status check: {e}")
                
                return sig
            else:
                logging.error(f"[Jupiter] Failed to send transaction")
                await cleanup_wsol_on_failure()
                return None
                
        except Exception as e:
            logging.error(f"[Jupiter] Send error: {e}")
            await cleanup_wsol_on_failure()
            return None
            
    except Exception as e:
        logging.error(f"[Jupiter] Swap execution error: {e}")
        return None

async def execute_jupiter_sell(mint: str, amount: int) -> Optional[str]:
    """Execute a sell using Jupiter"""
    try:
        input_mint = mint
        output_mint = "So11111111111111111111111111111111111111112"
        
        logging.info(f"[Jupiter] Getting sell quote for {mint[:8]}...")
        quote = await get_jupiter_quote(input_mint, output_mint, amount)
        if not quote:
            return None
        
        swap_data = await get_jupiter_swap_transaction(quote, wallet_pubkey)
        if not swap_data:
            return None
        
        tx_bytes = base64.b64decode(swap_data["swapTransaction"])
        tx = VersionedTransaction.from_bytes(tx_bytes)
        signed_tx = VersionedTransaction(tx.message, [keypair])
        
        logging.info("[Jupiter] Sending sell transaction...")
        
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
                            return None
                except Exception as e:
                    logging.debug(f"[Jupiter] Status check: {e}")
                
                return sig
            else:
                logging.error(f"[Jupiter] Failed to send sell transaction")
                return None
                
        except Exception as e:
            logging.error(f"[Jupiter] Sell send error: {e}")
            return None
            
    except Exception as e:
        logging.error(f"[Jupiter] Sell execution error: {e}")
        return None

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

async def buy_token(mint: str, force_amount: Optional[float] = None, is_migration: bool = False):
    """Execute buy with dynamic sizing and enhanced validation"""
    try:
        if mint in BROKEN_TOKENS:
            await send_telegram_alert(f"❌ Skipped {mint[:8]}... — broken token")
            log_skipped_token(mint, "Broken token")
            record_skip("malformed")
            return False

        increment_stat("snipes_attempted", 1)
        update_last_activity()
        
        # Check liquidity with timeout
        logging.info(f"[Buy] Checking liquidity for {mint[:8]}...")
        lp_data = await get_liquidity_and_ownership(mint)
        
        if lp_data is None:
            logging.warning(f"[Buy] LP check timed out for {mint[:8]}..., requeuing")
            return False
        
        pool_liquidity = lp_data.get("liquidity", 0)
        
        # Get minimum LP requirement based on current balance
        min_lp = get_minimum_liquidity_required() if SCALE_WITH_BALANCE else RUG_LP_THRESHOLD
        
        if pool_liquidity < min_lp:
            await send_telegram_alert(
                f"⚠️ Skipping low LP token\n"
                f"Token: {mint[:8]}...\n"
                f"LP: {pool_liquidity:.2f} SOL\n"
                f"Min required: {min_lp} SOL"
            )
            log_skipped_token(mint, f"Low liquidity: {pool_liquidity:.2f} SOL")
            record_skip("low_lp")
            return False

        # Get dynamic position size
        if force_amount:
            amount_sol = force_amount
            logging.info(f"[Buy] Using forced amount: {amount_sol} SOL")
        elif USE_DYNAMIC_SIZING:
            amount_sol = await get_dynamic_position_size(mint, pool_liquidity, is_migration)
        else:
            amount_sol = BUY_AMOUNT_SOL
        
        # Final safety check with risk manager
        try:
            from integrate_monster import risk_manager
            if risk_manager and not await risk_manager.check_risk_limits():
                logging.warning(f"[Buy] Risk limits hit, skipping buy for {mint[:8]}...")
                await send_telegram_alert(f"⚠️ Risk limits hit, skipping {mint[:8]}...")
                return False
        except ImportError:
            logging.debug("[Buy] Risk manager not available, continuing without check")
        except Exception as e:
            logging.debug(f"[Buy] Risk check error: {e}, continuing")
        
        amount_lamports = int(amount_sol * 1e9)
        logging.info(f"[Buy] Using dynamic position: {amount_sol:.3f} SOL for {mint[:8]}...")

        # Try Jupiter first
        logging.info(f"[Buy] Attempting Jupiter swap for {mint[:8]}...")
        jupiter_sig = await execute_jupiter_swap(mint, amount_lamports)
        
        if jupiter_sig:
            # Get current balance for status
            balance = rpc.get_balance(keypair.pubkey()).value / 1e9
            balance_usd = balance * 150
            
            await send_telegram_alert(
                f"✅ Sniped {mint[:8]}... via Jupiter\n"
                f"Amount: {amount_sol:.3f} SOL\n"
                f"LP: {pool_liquidity:.2f} SOL\n"
                f"{'🚀 MIGRATION!' if is_migration else ''}\n"
                f"Balance: {balance:.2f} SOL (${balance_usd:.0f})\n"
                f"TX: https://solscan.io/tx/{jupiter_sig}"
            )
            
            OPEN_POSITIONS[mint] = {
                "expected_token_amount": 0,
                "buy_amount_sol": amount_sol,
                "sold_stages": set(),
                "buy_sig": jupiter_sig,
                "is_migration": is_migration
            }
            
            increment_stat("snipes_succeeded", 1)
            log_trade(mint, "BUY", amount_sol, 0)
            return True
        
        # Fallback to Raydium
        logging.info(f"[Buy] Jupiter failed, trying Raydium for {mint[:8]}...")
        
        input_mint = "So11111111111111111111111111111111111111112"
        output_mint = mint
        
        pool = raydium.find_pool(input_mint, output_mint)
        if not pool:
            pool = raydium.find_pool(output_mint, input_mint)
            if not pool:
                await send_telegram_alert(f"⚠️ No pool found for {mint[:8]}...")
                log_skipped_token(mint, "No pool on Jupiter or Raydium")
                record_skip("malformed")
                return False

        tx = raydium.build_swap_transaction(keypair, input_mint, output_mint, amount_lamports)
        if not tx:
            await send_telegram_alert(f"❌ Failed to build Raydium swap for {mint[:8]}...")
            mark_broken_token(mint, 0)
            return False

        sig = raydium.send_transaction(tx, keypair)
        if not sig:
            await send_telegram_alert(f"📉 Raydium swap failed for {mint[:8]}...")
            mark_broken_token(mint, 0)
            return False

        # Get current balance for status
        balance = rpc.get_balance(keypair.pubkey()).value / 1e9
        balance_usd = balance * 150
        
        await send_telegram_alert(
            f"✅ Sniped {mint[:8]}... via Raydium\n"
            f"Amount: {amount_sol:.3f} SOL\n"
            f"LP: {pool_liquidity:.2f} SOL\n"
            f"{'🚀 MIGRATION!' if is_migration else ''}\n"
            f"Balance: {balance:.2f} SOL (${balance_usd:.0f})\n"
            f"TX: https://solscan.io/tx/{sig}"
        )
        
        OPEN_POSITIONS[mint] = {
            "expected_token_amount": 0,
            "buy_amount_sol": amount_sol,
            "sold_stages": set(),
            "buy_sig": sig,
            "is_migration": is_migration
        }
        
        increment_stat("snipes_succeeded", 1)
        log_trade(mint, "BUY", amount_sol, 0)
        return True

    except Exception as e:
        await send_telegram_alert(f"❌ Buy failed for {mint[:8]}...: {str(e)[:100]}")
        log_skipped_token(mint, f"Buy failed: {e}")
        return False

async def sell_token(mint: str, percent: float = 100.0):
    """Execute sell transaction for a token"""
    try:
        from spl.token.constants import TOKEN_PROGRAM_ID
        
        owner = keypair.pubkey()
        token_account = get_associated_token_address(owner, Pubkey.from_string(mint))
        
        try:
            response = rpc.get_token_account_balance(token_account)
            if not response or not hasattr(response, 'value') or not response.value:
                logging.warning(f"No token balance found for {mint}")
                await send_telegram_alert(f"⚠️ No token balance found for {mint[:8]}...")
                return False
            
            balance = int(response.value.amount)
        except Exception as e:
            logging.error(f"Failed to get token balance for {mint}: {e}")
            await send_telegram_alert(f"⚠️ Failed to get balance for {mint[:8]}...")
            return False
        
        amount = int(balance * percent / 100)
        
        if amount == 0:
            await send_telegram_alert(f"⚠️ Zero balance to sell for {mint[:8]}...")
            return False

        # Try Jupiter first
        logging.info(f"[Sell] Attempting Jupiter sell for {mint[:8]}...")
        jupiter_sig = await execute_jupiter_sell(mint, amount)
        
        if jupiter_sig:
            await send_telegram_alert(
                f"✅ Sold {percent}% of {mint[:8]}... via Jupiter\n"
                f"TX: https://solscan.io/tx/{jupiter_sig}"
            )
            log_trade(mint, f"SELL {percent}%", 0, amount)
            increment_stat("sells_executed", 1)
            return True
        
        # Fallback to Raydium
        logging.info(f"[Sell] Jupiter failed, trying Raydium for {mint[:8]}...")
        
        input_mint = mint
        output_mint = "So11111111111111111111111111111111111111112"
        
        pool = raydium.find_pool(input_mint, output_mint)
        if not pool:
            pool = raydium.find_pool(output_mint, input_mint)
            if not pool:
                await send_telegram_alert(f"⚠️ No pool for {mint[:8]}...")
                log_skipped_token(mint, "No pool for sell")
                return False

        tx = raydium.build_swap_transaction(keypair, input_mint, output_mint, amount)
        if not tx:
            await send_telegram_alert(f"❌ Failed to build sell TX for {mint[:8]}...")
            return False

        sig = raydium.send_transaction(tx, keypair)
        if not sig:
            await send_telegram_alert(f"❌ Failed to send sell tx for {mint[:8]}...")
            return False

        await send_telegram_alert(
            f"✅ Sold {percent}% of {mint[:8]}... via Raydium\n"
            f"TX: https://solscan.io/tx/{sig}"
        )
        log_trade(mint, f"SELL {percent}%", 0, amount)
        increment_stat("sells_executed", 1)
        return True
        
    except Exception as e:
        await send_telegram_alert(f"❌ Sell failed for {mint[:8]}...: {str(e)[:100]}")
        log_skipped_token(mint, f"Sell failed: {e}")
        return False

async def get_token_decimals(mint: str) -> int:
    """Get token decimals from blockchain with caching"""
    # Check cache first
    if mint in TOKEN_DECIMALS_CACHE:
        return TOKEN_DECIMALS_CACHE[mint]
    
    # Check known list
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
                    TOKEN_DECIMALS_CACHE[mint] = decimals
                    logging.info(f"[Decimals] Token {mint[:8]}... has {decimals} decimals (from chain)")
                    return decimals
    except Exception as e:
        logging.warning(f"[Decimals] Could not get decimals for {mint[:8]}...: {e}")
    
    # Default - but log a warning since this is where issues happen
    logging.warning(f"[Decimals] Using DEFAULT 9 decimals for {mint[:8]}... - THIS MAY CAUSE PRICE ERRORS!")
    return 9

async def get_token_price_usd(mint: str) -> Optional[float]:
    """Get current token price with proper decimal handling"""
    try:
        STABLECOIN_MINTS = {
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
            "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
        }
        
        if mint in STABLECOIN_MINTS:
            logging.info(f"[Price] {STABLECOIN_MINTS[mint]} stablecoin, returning $1.00")
            return 1.0
        
        # CRITICAL: Get actual decimals FIRST before any price calculations
        actual_decimals = await get_token_decimals(mint)
        logging.info(f"[Price] Token {mint[:8]}... using {actual_decimals} decimals for calculations")
        
        # Try DexScreener first for new tokens
        try:
            dex_url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
            
            async with httpx.AsyncClient(timeout=10, verify=certifi.where()) as client:
                response = await client.get(dex_url)
                if response.status_code == 200:
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
        
        # Try Birdeye if configured
        if BIRDEYE_API_KEY:
            try:
                url = f"https://public-api.birdeye.so/defi/price?address={mint}"
                
                async with httpx.AsyncClient(timeout=10, verify=certifi.where()) as client:
                    headers = {"X-API-KEY": BIRDEYE_API_KEY}
                    response = await client.get(url, headers=headers)
                    
                    if response.status_code == 200:
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
            async with httpx.AsyncClient(timeout=5, verify=certifi.where()) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    if mint in data.get("data", {}):
                        price_data = data["data"][mint]
                        price = float(price_data.get("price", 0))
                        if price > 0:
                            logging.info(f"[Price] {mint[:8]}... = ${price:.8f} (Jupiter Price API)")
                            return price
        except Exception as e:
            logging.debug(f"[Price] Jupiter Price API error: {e}")
        
        # Last resort: Calculate from Jupiter quote with proper decimal handling
        try:
            quote_url = f"{JUPITER_BASE_URL}/v6/quote"
            params = {
                "inputMint": "So11111111111111111111111111111111111111112",
                "outputMint": mint,
                "amount": str(int(1 * 1e9)),  # 1 SOL
                "slippageBps": "100"
            }
            
            async with httpx.AsyncClient(timeout=5, verify=certifi.where()) as client:
                response = await client.get(quote_url, params=params)
                if response.status_code == 200:
                    quote = response.json()
                    
                    # Honor Jupiter's provided values if available
                    if not IGNORE_JUPITER_PRICE_FIELD and "price" in quote:
                        price = float(quote["price"])
                        logging.info(f"[Price] {mint[:8]}... = ${price:.8f} (Jupiter price field)")
                        return price
                    
                    if "outAmount" in quote and float(quote["outAmount"]) > 0:
                        sol_price = 150.0  # Current SOL price
                        
                        # CRITICAL FIX: Use actual decimals, not hardcoded 9
                        if OVERRIDE_DECIMALS_TO_9:
                            # Only use 9 if explicitly overridden
                            tokens_received = float(quote["outAmount"]) / 1e9
                            logging.warning(f"[Price] OVERRIDE active, using 9 decimals instead of {actual_decimals}")
                        else:
                            # Use the actual token decimals
                            tokens_received = float(quote["outAmount"]) / (10 ** actual_decimals)
                        
                        sol_spent = 1.0
                        price = (sol_spent * sol_price) / tokens_received
                        
                        # Sanity check for known stablecoins
                        if mint in STABLECOIN_MINTS and (price > 2.0 or price < 0.5):
                            logging.error(f"[Price] Calculated ${price:.4f} for {STABLECOIN_MINTS[mint]} - decimal mismatch likely!")
                            logging.error(f"[Price] Token amount: {tokens_received}, decimals used: {actual_decimals}")
                            return 1.0
                        
                        # Sanity check for extreme prices that indicate decimal issues
                        if price > 1000000 or price < 0.0000001:
                            logging.warning(f"[Price] Extreme price ${price:.8f} detected for {mint[:8]}... - possible decimal issue")
                            logging.warning(f"[Price] Decimals: {actual_decimals}, Tokens: {tokens_received}, Raw amount: {quote['outAmount']}")
                        
                        logging.info(f"[Price] {mint[:8]}... = ${price:.8f} (calculated with {actual_decimals} decimals)")
                        return price
        except Exception as e:
            logging.debug(f"[Price] Jupiter quote error: {e}")
        
        logging.warning(f"[Price] Could not get price for {mint[:8]}...")
        return None
        
    except Exception as e:
        logging.error(f"[Price] Unexpected error for {mint}: {e}")
        return None

async def wait_and_auto_sell(mint: str):
    """Monitor position and auto-sell with different strategies based on token type"""
    try:
        if mint not in OPEN_POSITIONS:
            logging.warning(f"No position found for {mint}")
            return
            
        position = OPEN_POSITIONS[mint]
        buy_amount_sol = position["buy_amount_sol"]
        
        # Determine token type and strategy
        is_pumpfun = mint in pumpfun_tokens
        is_trending = mint in trending_tokens
        is_migration = position.get("is_migration", False)
        
        # Select appropriate targets
        if is_migration:
            # Aggressive targets for migrations
            targets = [5.0, 15.0, 30.0]
            sell_percents = [30, 30, 40]
            strategy_name = "MIGRATION"
            min_hold_time = 60
        elif is_pumpfun and PUMPFUN_USE_MOON_STRATEGY:
            targets = [PUMPFUN_TAKE_PROFIT_1, PUMPFUN_TAKE_PROFIT_2, PUMPFUN_TAKE_PROFIT_3]
            sell_percents = [PUMPFUN_SELL_PERCENT_1, PUMPFUN_SELL_PERCENT_2, PUMPFUN_MOON_BAG]
            strategy_name = "MOON SHOT"
            min_hold_time = NO_SELL_FIRST_MINUTES * 60
        elif is_trending and TRENDING_USE_CUSTOM:
            targets = [TRENDING_TAKE_PROFIT_1, TRENDING_TAKE_PROFIT_2, TRENDING_TAKE_PROFIT_3]
            sell_percents = [30, 35, 35]
            strategy_name = "TRENDING"
            min_hold_time = 60
        else:
            targets = [TAKE_PROFIT_1, TAKE_PROFIT_2, TAKE_PROFIT_3]
            sell_percents = [SELL_PERCENT_1, SELL_PERCENT_2, SELL_PERCENT_3]
            strategy_name = "STANDARD"
            min_hold_time = 0
        
        await asyncio.sleep(10)
        
        # Get entry price
        entry_price = await get_token_price_usd(mint)
        if not entry_price:
            logging.warning(f"Could not get entry price for {mint}, using timer-based fallback")
            await wait_and_auto_sell_timer_based(mint)
            return
            
        position["entry_price"] = entry_price
        position["highest_price"] = entry_price
        position["token_amount"] = position.get("expected_token_amount", 0)
        
        await send_telegram_alert(
            f"📊 Monitoring {mint[:8]}... [{strategy_name}]\n"
            f"Entry: ${entry_price:.6f}\n"
            f"Targets: {targets[0]}x/${entry_price*targets[0]:.6f}, "
            f"{targets[1]}x/${entry_price*targets[1]:.6f}, "
            f"{targets[2]}x/${entry_price*targets[2]:.6f}\n"
            f"Stop Loss: -${entry_price*STOP_LOSS_PERCENT/100:.6f}"
        )
        
        # Monitor loop
        start_time = time.time()
        last_price_check = 0
        max_sell_attempts = 3
        sell_attempts = {"profit1": 0, "profit2": 0, "profit3": 0, "stop_loss": 0}
        
        while time.time() - start_time < MAX_HOLD_TIME_SEC:
            try:
                if time.time() - last_price_check < PRICE_CHECK_INTERVAL_SEC:
                    await asyncio.sleep(1)
                    continue
                    
                last_price_check = time.time()
                current_price = await get_token_price_usd(mint)
                
                if not current_price:
                    logging.debug(f"Could not get price for {mint}, skipping this check")
                    await asyncio.sleep(PRICE_CHECK_INTERVAL_SEC)
                    continue
                
                profit_multiplier = current_price / entry_price
                profit_percent = (profit_multiplier - 1) * 100
                time_held = time.time() - start_time
                
                # Update highest price
                if current_price > position["highest_price"]:
                    position["highest_price"] = current_price
                    logging.info(f"[{mint[:8]}] New high: ${current_price:.6f} ({profit_multiplier:.2f}x)")
                
                # Check minimum hold time
                if is_pumpfun and time_held < min_hold_time:
                    logging.debug(f"[{mint[:8]}] Holding for {min_hold_time/60:.0f} mins minimum (PumpFun)")
                    await asyncio.sleep(PRICE_CHECK_INTERVAL_SEC)
                    continue
                
                # Check trailing stop
                if profit_multiplier >= TRAILING_STOP_ACTIVATION:
                    drop_from_high = (position["highest_price"] - current_price) / position["highest_price"] * 100
                    if drop_from_high >= TRAILING_STOP_PERCENT and len(position["sold_stages"]) > 0:
                        logging.info(f"[{mint[:8]}] Trailing stop triggered! Down {drop_from_high:.1f}% from peak")
                        if await sell_token(mint, 100):
                            await send_telegram_alert(
                                f"⛔ Trailing stop triggered for {mint[:8]}!\n"
                                f"Price dropped {drop_from_high:.1f}% from peak ${position['highest_price']:.6f}\n"
                                f"Sold remaining position at ${current_price:.6f} ({profit_multiplier:.1f}x)"
                            )
                            break
                
                # Check stop loss
                if profit_percent <= -STOP_LOSS_PERCENT and sell_attempts["stop_loss"] < max_sell_attempts:
                    sell_attempts["stop_loss"] += 1
                    logging.info(f"[{mint[:8]}] Stop loss triggered at {profit_percent:.1f}%")
                    if await sell_token(mint, 100):
                        await send_telegram_alert(
                            f"🛑 Stop loss triggered for {mint[:8]}!\n"
                            f"Loss: {profit_percent:.1f}% (${current_price:.6f})\n"
                            f"Sold all to minimize losses"
                        )
                        break
                
                # Check profit targets
                if profit_multiplier >= targets[0] and "profit1" not in position["sold_stages"] and sell_attempts["profit1"] < max_sell_attempts:
                    sell_attempts["profit1"] += 1
                    if await sell_token(mint, sell_percents[0]):
                        position["sold_stages"].add("profit1")
                        await send_telegram_alert(
                            f"💰 Hit {targets[0]}x profit for {mint[:8]}!\n"
                            f"Price: ${current_price:.6f} ({profit_multiplier:.2f}x)\n"
                            f"Sold {sell_percents[0]}% of position\n"
                            f"Strategy: {strategy_name}"
                        )
                
                if profit_multiplier >= targets[1] and "profit2" not in position["sold_stages"] and sell_attempts["profit2"] < max_sell_attempts:
                    sell_attempts["profit2"] += 1
                    if await sell_token(mint, sell_percents[1]):
                        position["sold_stages"].add("profit2")
                        await send_telegram_alert(
                            f"🚀 Hit {targets[1]}x profit for {mint[:8]}!\n"
                            f"Price: ${current_price:.6f} ({profit_multiplier:.2f}x)\n"
                            f"Sold {sell_percents[1]}% of position\n"
                            f"Strategy: {strategy_name}"
                        )
                
                if profit_multiplier >= targets[2] and "profit3" not in position["sold_stages"] and sell_attempts["profit3"] < max_sell_attempts:
                    sell_attempts["profit3"] += 1
                    if await sell_token(mint, sell_percents[2]):
                        position["sold_stages"].add("profit3")
                        await send_telegram_alert(
                            f"🌙 Hit {targets[2]}x profit for {mint[:8]}!\n"
                            f"Price: ${current_price:.6f} ({profit_multiplier:.2f}x)\n"
                            f"Sold final {sell_percents[2]}% of position\n"
                            f"Total profit: {(profit_multiplier-1)*100:.1f}%!\n"
                            f"Strategy: {strategy_name} SUCCESS! 🎯"
                        )
                        break
                
                # Log status every minute
                if int((time.time() - start_time) % 60) == 0:
                    logging.info(
                        f"[{mint[:8]}] [{strategy_name}] Price: ${current_price:.6f} ({profit_multiplier:.2f}x) | "
                        f"High: ${position['highest_price']:.6f} | "
                        f"Sold stages: {position['sold_stages']}"
                    )
                
                if len(position["sold_stages"]) >= 3:
                    logging.info(f"[{mint[:8]}] All profit targets hit, position closed")
                    break
                    
            except Exception as e:
                logging.error(f"Error monitoring {mint}: {e}")
                await asyncio.sleep(PRICE_CHECK_INTERVAL_SEC)
        
        # Time limit reached
        if time.time() - start_time >= MAX_HOLD_TIME_SEC:
            logging.info(f"[{mint[:8]}] Max hold time reached, force selling")
            if await sell_token(mint, 100):
                current_price = await get_token_price_usd(mint) or entry_price
                profit_percent = ((current_price / entry_price) - 1) * 100
                await send_telegram_alert(
                    f"⏰ Max hold time reached for {mint[:8]}\n"
                    f"Force sold after {MAX_HOLD_TIME_SEC/60:.0f} minutes\n"
                    f"Final P&L: {profit_percent:+.1f}%\n"
                    f"Strategy used: {strategy_name}"
                )
        
        # Clean up position
        if mint in OPEN_POSITIONS:
            del OPEN_POSITIONS[mint]
            
    except Exception as e:
        logging.error(f"Auto-sell error for {mint}: {e}")
        await send_telegram_alert(f"⚠️ Auto-sell error for {mint}: {e}")
        if mint in OPEN_POSITIONS:
            del OPEN_POSITIONS[mint]
