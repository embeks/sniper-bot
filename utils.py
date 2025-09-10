# utils.py - FIXED VERSION WITH PROPER BALANCE TRACKING AND PUMPFUN SUPPORT
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
print(f"ACTUAL WALLET BEING USED: {wallet_pubkey}")

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
        "quality_check": 0
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
# NOTIFICATION HELPER
# ============================================
async def notify(kind: str, text: str):
    """Alert policy helper - routes alerts based on config switches with cooldown"""
    if kind not in CONFIG.ALERTS_NOTIFY:
        return True
    
    if not CONFIG.ALERTS_NOTIFY.get(kind, False):
        return True
    
    # Check cooldown
    cooldown = CONFIG.ALERTS_NOTIFY.get("cooldown_secs", 60)
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
                
                # Check mint authority (bytes 0-32) and freeze authority (bytes 36-68)
                if len(decoded) > 68:
                    mint_auth = decoded[0:32]
                    freeze_auth = decoded[36:68]
                    
                    # Check if authorities are null (all zeros)
                    mint_renounced = all(b == 0 for b in mint_auth)
                    freeze_renounced = all(b == 0 for b in freeze_auth)
                    
                    return mint_renounced, freeze_renounced
    except Exception as e:
        logging.debug(f"Authority check error for {mint}: {e}")
    
    return False, False

async def check_token_tax(mint: str) -> int:
    """Detect transfer tax on token via DexScreener (returns basis points)"""
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        async with httpx.AsyncClient(timeout=5, verify=certifi.where()) as client:
            response = await client.get(url)
            if response.status_code == 200:
                data = response.json()
                if "pairs" in data and len(data["pairs"]) > 0:
                    # Check if any pair has buy/sell tax info
                    for pair in data["pairs"]:
                        buy_tax = pair.get("buyTax", 0)
                        sell_tax = pair.get("sellTax", 0)
                        if buy_tax > 0 or sell_tax > 0:
                            max_tax = max(buy_tax, sell_tax)
                            return int(max_tax * 100)  # Convert to basis points
    except:
        pass
    
    return 0  # Default to no tax if can't determine

# ============================================
# AGE CHECKING FUNCTIONS
# ============================================
async def is_fresh_token(mint: str, max_age_seconds: int = 60) -> bool:
    """Check if token/pool was created within the specified time"""
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        async with httpx.AsyncClient(timeout=5, verify=certifi.where()) as client:
            response = await client.get(url)
            if response.status_code == 200:
                data = response.json()
                if "pairs" in data and len(data["pairs"]) > 0:
                    for pair in data["pairs"]:
                        created_at = pair.get("pairCreatedAt")
                        if created_at:
                            age_ms = time.time() * 1000 - created_at
                            age_seconds = age_ms / 1000
                            
                            if age_seconds <= max_age_seconds:
                                return True
                            else:
                                logging.info(f"[AGE CHECK] Token {mint[:8]}... is {age_seconds:.0f}s old (max: {max_age_seconds}s)")
                                return False
                
                logging.info(f"[AGE CHECK] No data for {mint[:8]}... - assuming old token")
                return False
                
    except Exception as e:
        logging.error(f"Age check error: {e}")
        return False

async def verify_token_age_on_chain(mint: str, max_age_seconds: int = 60) -> bool:
    """Verify token age by checking mint creation on chain"""
    try:
        mint_pubkey = Pubkey.from_string(mint)
        
        signatures = rpc.get_signatures_for_address(
            mint_pubkey,
            limit=1,
            commitment="confirmed"
        )
        
        if signatures and signatures.value:
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
                async with httpx.AsyncClient(timeout=10, verify=certifi.where()) as client:
                    response = await client.post(url, json=payload)
                    
                    if response.status_code == 200:
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
    """Get accurate liquidity with timeout and proper validation"""
    try:
        async def _check_liquidity():
            sol_mint = "So11111111111111111111111111111111111111112"
            
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
            
            logging.info(f"[LP Check] No Raydium pool, checking Jupiter...")
            try:
                url = CONFIG.JUPITER_QUOTE_BASE_URL
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
        
        try:
            result = await asyncio.wait_for(_check_liquidity(), timeout=CONFIG.LP_CHECK_TIMEOUT)
            return result
        except asyncio.TimeoutError:
            logging.warning(f"[LP Check] Timeout after {CONFIG.LP_CHECK_TIMEOUT}s for {mint[:8]}...")
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
        url = CONFIG.JUPITER_QUOTE_BASE_URL
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
                
                # Return v6 quote format
                return {
                    "inAmount": quote.get("inAmount", "0"),
                    "outAmount": quote.get("outAmount", "0"),
                    "otherAmountThreshold": quote.get("otherAmountThreshold", "0"),
                    "priceImpactPct": quote.get("priceImpactPct", "0"),
                    "routePlan": quote.get("routePlan", [])
                }
            else:
                logging.warning(f"[Jupiter] Quote request failed: {response.status_code}")
                return None
    except Exception as e:
        logging.error(f"[Jupiter] Error getting quote: {e}")
        return None

async def get_jupiter_swap_transaction(quote: dict, user_pubkey: str, slippage_bps: int = None):
    """Get the swap transaction from Jupiter v6"""
    if slippage_bps is None:
        slippage_bps = CONFIG.STOP_MAX_SLIPPAGE_BPS
        
    try:
        url = CONFIG.JUPITER_SWAP_URL
        
        body = {
            "quoteResponse": quote,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": 500000,
            "slippageBps": slippage_bps
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

async def simulate_transaction(tx: VersionedTransaction) -> bool:
    """Simulate transaction - treat successful simulation as valid"""
    try:
        result = rpc.simulate_transaction(tx, commitment=Confirmed)
        
        if not result or not result.value:
            logging.warning("[Simulation] No result returned")
            return False
        
        if result.value.err:
            logging.warning(f"[Simulation] Error: {result.value.err}")
            return False
        
        # If no error, simulation is successful
        return True
        
    except Exception as e:
        logging.error(f"[Simulation] Error: {e}")
        return False

async def execute_jupiter_swap(mint: str, amount_lamports: int) -> Optional[str]:
    """Execute a swap using Jupiter v6"""
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
        
        # Optional simulation
        if CONFIG.SIMULATE_BEFORE_SEND:
            logging.info("[Jupiter] Simulating transaction...")
            if not await simulate_transaction(tx):
                logging.warning("[Jupiter] Simulation failed, aborting")
                return None
        
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

async def execute_jupiter_sell(mint: str, amount: int, slippage_bps: int = None) -> Optional[str]:
    """Execute a sell using Jupiter v6 with safety validation"""
    if slippage_bps is None:
        slippage_bps = CONFIG.STOP_MAX_SLIPPAGE_BPS
        
    try:
        input_mint = mint
        output_mint = "So11111111111111111111111111111111111111112"
        
        logging.info(f"[Jupiter] Getting sell quote for {mint[:8]}... with {slippage_bps} bps slippage")
        quote = await get_jupiter_quote(input_mint, output_mint, amount, slippage_bps)
        if not quote:
            return None
        
        swap_data = await get_jupiter_swap_transaction(quote, wallet_pubkey, slippage_bps)
        if not swap_data:
            return None
        
        tx_bytes = base64.b64decode(swap_data["swapTransaction"])
        tx = VersionedTransaction.from_bytes(tx_bytes)
        
        # Validate transaction if configured
        if CONFIG.SIMULATE_BEFORE_SEND:
            logging.info("[Jupiter] Simulating sell transaction for safety...")
            if not await simulate_transaction(tx):
                logging.error(f"[Jupiter] Sell simulation failed for {mint[:8]}...")
                return None
        
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

async def buy_token(mint: str, amount: float = None, **kwargs) -> bool:
    """Execute buy with pre-trade validation and stop-loss registration - Jupiter only"""
    try:
        if mint in BROKEN_TOKENS:
            log_skipped_token(mint, "Broken token")
            record_skip("malformed")
            return False

        increment_stat("snipes_attempted", 1)
        update_last_activity()
        
        # Use explicit amount or default from config
        if amount is None:
            amount = CONFIG.BUY_AMOUNT_SOL
        
        logging.info(f"[Buy] Using amount: {amount:.3f} SOL for {mint[:8]}...")
        
        # ============================================
        # CRITICAL FIX: DETECT PUMPFUN TOKENS EARLY
        # ============================================
        is_pumpfun = mint in pumpfun_tokens or kwargs.get("is_pumpfun", False)
        is_migration = kwargs.get("is_migration", False)
        
        # PRE-TRADE VALIDATION
        
        if is_pumpfun:
            # SPECIAL HANDLING FOR PUMPFUN TOKENS
            logging.info(f"[Buy] PumpFun token detected - using simplified checks")
            
            # For PumpFun, assume minimal liquidity and skip extensive checks
            pool_liquidity = 0.1  # Default for fresh PumpFun
            
            # Try to get actual liquidity but don't fail if we can't
            try:
                # Quick Jupiter quote check to see if it's tradeable
                test_quote = await get_jupiter_quote(
                    "So11111111111111111111111111111111111111112",
                    mint,
                    int(0.001 * 1e9),
                    500
                )
                if test_quote and int(test_quote.get("outAmount", 0)) > 0:
                    # Token is tradeable on Jupiter
                    price_impact = float(test_quote.get("priceImpactPct", 100))
                    if price_impact < 10:
                        pool_liquidity = 1.0  # Decent liquidity
                    elif price_impact < 50:
                        pool_liquidity = 0.5  # Low liquidity
                    else:
                        pool_liquidity = 0.1  # Very low liquidity
                    logging.info(f"[Buy] PumpFun token has estimated {pool_liquidity:.2f} SOL liquidity")
            except Exception as e:
                logging.debug(f"[Buy] Quick liquidity check failed for PumpFun: {e}")
                pool_liquidity = 0.1  # Assume minimal
            
            # Skip authority and tax checks for PumpFun (they handle this)
            
        else:
            # REGULAR TOKEN HANDLING (Raydium, etc.)
            logging.info(f"[Buy] Checking liquidity for regular token {mint[:8]}...")
            lp_data = await get_liquidity_and_ownership(mint)
            
            if lp_data is None:
                logging.warning(f"[Buy] LP check timed out for {mint[:8]}..., requeuing")
                return False
            
            pool_liquidity = lp_data.get("liquidity", 0)
            
            # Check minimum liquidity for non-PumpFun
            if pool_liquidity < CONFIG.MIN_LP_SOL:
                log_skipped_token(mint, f"Low liquidity: {pool_liquidity:.2f} SOL")
                record_skip("low_lp")
                return False
            
            # Check authority renouncement for non-PumpFun
            if CONFIG.REQUIRE_AUTH_RENOUNCED:
                mint_renounced, freeze_renounced = await check_mint_authority(mint)
                if not (mint_renounced and freeze_renounced):
                    log_skipped_token(mint, "Authority not renounced")
                    return False
            
            # Check for high tax
            tax_bps = await check_token_tax(mint)
            if tax_bps > CONFIG.MAX_TRADE_TAX_BPS:
                log_skipped_token(mint, f"High tax: {tax_bps/100:.1f}%")
                return False
        
        # Check sell route availability before buying (for all tokens)
        estimated_tokens = int(amount * 1e9 * 100)  # Rough estimate
        sell_quote = await get_jupiter_quote(mint, "So11111111111111111111111111111111111111112", estimated_tokens, 500)
        if not sell_quote or int(sell_quote.get("outAmount", 0)) == 0:
            log_skipped_token(mint, "No sell route available")
            record_skip("no_route")
            return False
        
        # Position sizing for PumpFun
        pumpfun_position = 0
        
        if is_pumpfun:
            can_buy, pumpfun_position = await evaluate_pumpfun_opportunity(mint, pool_liquidity)
            
            if not can_buy and pool_liquidity < 0.05:
                logging.info(f"[Buy] PumpFun token {mint[:8]}... with {pool_liquidity:.2f} SOL LP - TOO LOW")
                record_skip("low_lp")
                return False
        
        # Override amount for special cases if dynamic sizing is enabled
        if CONFIG.USE_DYNAMIC_SIZING and amount == CONFIG.BUY_AMOUNT_SOL:
            if is_pumpfun and pumpfun_position > 0:
                amount = pumpfun_position
            else:
                amount = await get_dynamic_position_size(mint, pool_liquidity, is_migration)
        
        # Final safety check with risk manager
        try:
            from integrate_monster import risk_manager
            if risk_manager and not await risk_manager.check_risk_limits():
                logging.warning(f"[Buy] Risk limits hit, skipping buy for {mint[:8]}...")
                return False
        except ImportError:
            logging.debug("[Buy] Risk manager not available, continuing without check")
        except Exception as e:
            logging.debug(f"[Buy] Risk check error: {e}, continuing")
        
        amount_lamports = int(amount * 1e9)
        logging.info(f"[Buy] Final position size: {amount:.3f} SOL for {mint[:8]}...")

        # Execute Jupiter swap
        logging.info(f"[Buy] Attempting Jupiter swap for {mint[:8]}...")
        jupiter_sig = await execute_jupiter_swap(mint, amount_lamports)
        
        if jupiter_sig:
            balance = rpc.get_balance(keypair.pubkey()).value / 1e9
            balance_usd = balance * CONFIG.SOL_PRICE_USD
            
            # Wait for ATA to be created and get REAL balance
            from spl.token.constants import TOKEN_PROGRAM_ID
            owner = keypair.pubkey()
            token_account = get_associated_token_address(owner, Pubkey.from_string(mint))
            
            real_tokens = 0
            for retry in range(10):  # Try for ~3 seconds
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
                # Fallback but log warning
                real_tokens = estimated_tokens
                logging.warning(f"[Buy] Could not get real balance, using estimate: {real_tokens}")
            
            # Get entry price for stop-loss
            entry_price = await get_token_price_usd(mint)
            if not entry_price:
                entry_price = (amount * CONFIG.SOL_PRICE_USD) / (real_tokens / (10 ** await get_token_decimals(mint)))
            
            # ARM THE STOP-LOSS with REAL token balance
            register_stop(mint, {
                "entry_price": entry_price,
                "size_tokens": real_tokens,  # Use actual balance!
                "stop_price": entry_price * (1 - CONFIG.STOP_LOSS_PCT),
                "slippage_bps": CONFIG.STOP_MAX_SLIPPAGE_BPS,
                "state": "ARMED",
                "last_alert": 0,
                "first_no_route": 0,
                "stuck_reason": None,
                "emergency_attempts": 0
            })
            
            token_type = "PumpFun" if is_pumpfun else "Regular"
            
            await notify("buy",
                f"✅ Sniped {mint[:8]}... via Jupiter\n"
                f"Type: {token_type}\n"
                f"Amount: {amount:.3f} SOL\n"
                f"Tokens: {real_tokens}\n"
                f"LP: {pool_liquidity:.2f} SOL\n"
                f"Entry: ${entry_price:.6f}\n"
                f"Stop: ${entry_price * (1 - CONFIG.STOP_LOSS_PCT):.6f}\n"
                f"{'🚀 MIGRATION!' if is_migration else ''}\n"
                f"Balance: {balance:.2f} SOL (${balance_usd:.0f})\n"
                f"TX: https://solscan.io/tx/{jupiter_sig}"
            )
            
            OPEN_POSITIONS[mint] = {
                "expected_token_amount": real_tokens,
                "buy_amount_sol": amount,
                "sold_stages": set(),
                "buy_sig": jupiter_sig,
                "is_migration": is_migration,
                "entry_price": entry_price
            }
            
            increment_stat("snipes_succeeded", 1)
            log_trade(mint, "BUY", amount, real_tokens)
            return True
        else:
            # Jupiter failed
            log_skipped_token(mint, "Jupiter swap failed")
            record_skip("buy_failed")
            return False

    except Exception as e:
        logging.error(f"Buy failed for {mint[:8]}...: {e}")
        log_skipped_token(mint, f"Buy failed: {e}")
        return False

async def sell_token(mint: str, amount_to_sell=None, percentage=100, slippage_bps=None):
    """Execute sell transaction - ALWAYS uses Jupiter with validation"""
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

        # ALWAYS use Jupiter (forced by config)
        logging.info(f"[Sell] Using Jupiter for sell of {mint[:8]}...")
        jupiter_sig = await execute_jupiter_sell(mint, amount, slippage_bps)
        
        if jupiter_sig:
            await notify("sell",
                f"✅ Sold {percentage}% of {mint[:8]}... via Jupiter\n"
                f"TX: https://solscan.io/tx/{jupiter_sig}"
            )
            log_trade(mint, f"SELL {percentage}%", 0, amount)
            increment_stat("sells_executed", 1)
            return True
        else:
            logging.error(f"[Sell] Jupiter sell failed for {mint[:8]}...")
            return False
        
    except Exception as e:
        logging.error(f"Sell failed for {mint[:8]}...: {e}")
        log_skipped_token(mint, f"Sell failed: {e}")
        return False

async def get_token_decimals(mint: str) -> int:
    """Get token decimals from blockchain with caching and validation"""
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
                        logging.info(f"[Decimals] Token {mint[:8]}... has {decimals} decimals (from chain)")
                        return decimals
                    else:
                        logging.warning(f"[Decimals] Invalid decimals {decimals} for {mint[:8]}... - using default 9")
                        return 9
    except Exception as e:
        logging.warning(f"[Decimals] Could not get decimals for {mint[:8]}...: {e}")
    
    logging.warning(f"[Decimals] Using DEFAULT 9 decimals for {mint[:8]}...")
    return 9

async def get_token_price_usd(mint: str) -> Optional[float]:
    """Get current token price - proper implementation without corruption"""
    try:
        STABLECOIN_MINTS = {
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
            "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
        }
        
        if mint in STABLECOIN_MINTS:
            logging.info(f"[Price] {STABLECOIN_MINTS[mint]} stablecoin, returning $1.00")
            return 1.0
        
        actual_decimals = await get_token_decimals(mint)
        logging.info(f"[Price] Token {mint[:8]}... using {actual_decimals} decimals for calculations")
        
        # 1. Try DexScreener first
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
        
        # 2. Try Birdeye if available
        if CONFIG.BIRDEYE_API_KEY:
            try:
                url = f"https://public-api.birdeye.so/defi/price?address={mint}"
                
                async with httpx.AsyncClient(timeout=10, verify=certifi.where()) as client:
                    headers = {"X-API-KEY": CONFIG.BIRDEYE_API_KEY}
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
        
        # 3. Try Jupiter Price API
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
        
        # 4. Last resort: compute from Jupiter quote
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
                    logging.info(f"[Price] {mint[:8]}... = ${price:.8f} (computed from quote)")
                    return price
        except Exception as e:
            logging.debug(f"[Price] Jupiter quote error: {e}")
        
        logging.warning(f"[Price] Could not get price for {mint[:8]}...")
        return None
        
    except Exception as e:
        logging.error(f"[Price] Unexpected error for {mint}: {e}")
        return None

# STOP-LOSS MONITOR - FIXED WITH PROPER BALANCE TRACKING
async def wait_and_auto_sell(mint: str):
    """Monitor position with integrated stop-loss engine - FIXED BALANCE TRACKING"""
    try:
        if mint not in OPEN_POSITIONS:
            logging.warning(f"No position found for {mint}")
            return
            
        position = OPEN_POSITIONS[mint]
        buy_amount_sol = position["buy_amount_sol"]
        
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
        max_sell_attempts = 3
        sell_attempts = {"profit1": 0, "profit2": 0, "profit3": 0, "stop_loss": 0}
        
        while time.time() - start_time < CONFIG.MAX_HOLD_TIME_SEC:
            try:
                # Check stop-loss more frequently
                if time.time() - last_price_check < CONFIG.STOP_CHECK_INTERVAL_SEC:
                    await asyncio.sleep(0.5)
                    continue
                    
                last_price_check = time.time()
                
                # FIXED: Get current token balance with proper retries
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
                                # Update position with real balance
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
                    
                    # Try standard slippage first (up to 3 attempts)
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
                            stop_data["state"] = "TRIGGERED"  # Reset to retry
                    
                    # Try emergency slippage once after 3 normal attempts
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
                
                # Check minimum hold time (skip profit targets if stop is triggered)
                if stop_data["state"] != "TRIGGERED" and is_pumpfun and time_held < min_hold_time:
                    logging.debug(f"[{mint[:8]}] Holding for {min_hold_time/60:.0f} mins minimum (PumpFun)")
                    await asyncio.sleep(CONFIG.STOP_CHECK_INTERVAL_SEC)
                    continue
                
                # Check trailing stop (only if stop not triggered)
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
                
                # Check profit targets (only if stop not triggered)
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
                if int((time.time() - start_time) % 60) == 0:
                    logging.info(
                        f"[{mint[:8]}] Price: ${current_price:.6f} ({profit_multiplier:.2f}x) | "
                        f"State: {stop_data['state']} | Sold: {position['sold_stages']}"
                    )
                
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
            # Only delete if fully closed
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
        async with httpx.AsyncClient(timeout=5, verify=certifi.where()) as client:
            response = await client.get(url)
            if response.status_code == 200:
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
            async with httpx.AsyncClient(timeout=5, verify=certifi.where()) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
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
    'CONFIG',
    'register_stop',
    'STOPS',
    'notify'
]
