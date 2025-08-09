import os
import json
import logging
import httpx
import asyncio
import time
import csv
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from dotenv import load_dotenv
import base64
from solders.transaction import VersionedTransaction

# Solana imports
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from spl.token.instructions import get_associated_token_address

# Import Raydium client
from raydium_aggregator import RaydiumAggregatorClient

# Setup
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Environment variables - MATCHING YOUR .env FILE
RPC_URL = os.getenv("RPC_URL")
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")  # Changed from WALLET_PK
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # Changed from TELEGRAM_USER_ID
TELEGRAM_USER_ID = os.getenv("TELEGRAM_USER_ID")  # Keep both for compatibility
BUY_AMOUNT_SOL = float(os.getenv("BUY_AMOUNT_SOL", 0.03))  # Changed default to 0.03
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")
JUPITER_BASE_URL = os.getenv("JUPITER_BASE_URL", "https://quote-api.jup.ag")
SELL_MULTIPLIERS = os.getenv("SELL_MULTIPLIERS", "2,5,10").split(",")
SELL_TIMEOUT_SEC = int(os.getenv("SELL_TIMEOUT_SEC", 300))
RUG_LP_THRESHOLD = float(os.getenv("RUG_LP_THRESHOLD", 0.5))
BLACKLISTED_TOKENS = os.getenv("BLACKLISTED_TOKENS", "").split(",") if os.getenv("BLACKLISTED_TOKENS") else []

# Parse sell percentages from multipliers (using defaults)
AUTO_SELL_PERCENT_2X = 50
AUTO_SELL_PERCENT_5X = 25
AUTO_SELL_PERCENT_10X = 25

# NEW: Profit-based trading configuration
TAKE_PROFIT_1 = float(os.getenv("TAKE_PROFIT_1", 2.0))  # 2x
TAKE_PROFIT_2 = float(os.getenv("TAKE_PROFIT_2", 5.0))  # 5x
TAKE_PROFIT_3 = float(os.getenv("TAKE_PROFIT_3", 10.0))  # 10x
SELL_PERCENT_1 = float(os.getenv("SELL_PERCENT_1", 50))
SELL_PERCENT_2 = float(os.getenv("SELL_PERCENT_2", 25))
SELL_PERCENT_3 = float(os.getenv("SELL_PERCENT_3", 25))
STOP_LOSS_PERCENT = float(os.getenv("STOP_LOSS_PERCENT", 50))  # Sell if down 50%
TRAILING_STOP_PERCENT = float(os.getenv("TRAILING_STOP_PERCENT", 20))  # Sell if drops 20% from peak
MAX_HOLD_TIME_SEC = int(os.getenv("MAX_HOLD_TIME_SEC", 3600))  # 1 hour max hold
PRICE_CHECK_INTERVAL_SEC = int(os.getenv("PRICE_CHECK_INTERVAL_SEC", 10))  # Check every 10s

# Initialize clients
rpc = Client(RPC_URL, commitment=Confirmed)
raydium = RaydiumAggregatorClient(RPC_URL)

# Load wallet - Handle array format [1,2,3,...] from your .env
import ast
try:
    # If it's an array string like [1,2,3,...]
    if SOLANA_PRIVATE_KEY and SOLANA_PRIVATE_KEY.startswith("["):
        private_key_array = ast.literal_eval(SOLANA_PRIVATE_KEY)
        keypair = Keypair.from_seed(bytes(private_key_array[:32]))
    else:
        # If it's a base58 string
        keypair = Keypair.from_base58_string(SOLANA_PRIVATE_KEY)
except Exception as e:
    raise ValueError(f"Failed to load wallet from SOLANA_PRIVATE_KEY: {e}")

wallet_pubkey = str(keypair.pubkey())

# Use TELEGRAM_CHAT_ID but also support TELEGRAM_USER_ID for backwards compatibility
if not TELEGRAM_CHAT_ID and TELEGRAM_USER_ID:
    TELEGRAM_CHAT_ID = TELEGRAM_USER_ID

# Global state
OPEN_POSITIONS = {}
BROKEN_TOKENS = set()
BOT_RUNNING = True
BLACKLIST_FILE = "blacklist.json"
TRADES_CSV_FILE = "trades.csv"

# Add blacklisted tokens from env
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
        "buy_failed": 0
    }
}

# Status tracking
listener_status = {"Raydium": "OFFLINE", "Jupiter": "OFFLINE"}
last_activity = time.time()
last_seen_token = {"Raydium": time.time(), "Jupiter": time.time()}

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

async def send_telegram_alert(message: str):
    """Send alert to Telegram"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,  # Changed from TELEGRAM_USER_ID
            "text": message[:4096],
            "parse_mode": "HTML"
        }
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(url, json=payload)
    except Exception as e:
        logging.error(f"Telegram send failed: {e}")

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
        return f"Balance: {balance:.4f} SOL\nAddress: {wallet_pubkey}"
    except:
        return "Failed to fetch wallet info"

def get_bot_status_message() -> str:
    """Get detailed bot status"""
    elapsed = int(time.time() - last_activity)
    raydium_elapsed = int(time.time() - last_seen_token["Raydium"])
    jupiter_elapsed = int(time.time() - last_seen_token["Jupiter"])
    
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
  
📈 Open Positions: {len(OPEN_POSITIONS)}
🚫 Broken Tokens: {len(BROKEN_TOKENS)}
"""

async def get_liquidity_and_ownership(mint: str) -> Optional[Dict[str, Any]]:
    """Get liquidity info for a token"""
    try:
        pool = raydium.find_pool("So11111111111111111111111111111111111111112", mint)
        if pool:
            # Get vault balances
            sol_vault = Pubkey.from_string(pool["baseVault"] if pool["baseMint"] == "So11111111111111111111111111111111111111112" else pool["quoteVault"])
            sol_balance = rpc.get_balance(sol_vault).value / 1e9
            return {"liquidity": sol_balance * 2}  # Rough estimate
    except Exception as e:
        logging.error(f"Failed to get liquidity: {e}")
    return None

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

# Jupiter Integration Functions
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
        
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url, params=params)
            if response.status_code == 200:
                quote = response.json()
                logging.info(f"[Jupiter] Got quote: {amount/1e9:.4f} SOL -> {float(quote.get('outAmount', 0))/1e9:.4f} tokens")
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
            "wrapAndUnwrapSol": True,  # Handles WSOL automatically!
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": 500000,  # Higher priority - 0.0005 SOL
            "slippageBps": 300  # 3% slippage for volatile memecoins
        }
        
        async with httpx.AsyncClient(timeout=15) as client:
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
        input_mint = "So11111111111111111111111111111111111111112"  # SOL
        output_mint = mint
        
        # Step 1: Get quote
        logging.info(f"[Jupiter] Getting quote for {amount_lamports/1e9:.4f} SOL -> {mint[:8]}...")
        quote = await get_jupiter_quote(input_mint, output_mint, amount_lamports)
        if not quote:
            logging.warning("[Jupiter] Failed to get quote")
            return None
        
        # Check if output amount is reasonable
        out_amount = int(quote.get("outAmount", 0))
        if out_amount == 0:
            logging.warning("[Jupiter] Quote returned zero output amount")
            return None
        
        # Step 2: Get swap transaction
        logging.info("[Jupiter] Building swap transaction...")
        swap_data = await get_jupiter_swap_transaction(quote, wallet_pubkey)
        if not swap_data:
            logging.warning("[Jupiter] Failed to get swap transaction")
            return None
        
        # Step 3: Deserialize and sign transaction - SIMPLEST METHOD
        tx_bytes = base64.b64decode(swap_data["swapTransaction"])
        tx = VersionedTransaction.from_bytes(tx_bytes)
        
        # The transaction needs to be signed with our keypair
        # Pass the keypair directly to the transaction constructor
        signed_tx = VersionedTransaction(tx.message, [keypair])
        
        # Step 4: Send transaction
        logging.info("[Jupiter] Sending transaction...")
        
        try:
            result = rpc.send_transaction(
                signed_tx,
                opts=TxOpts(
                    skip_preflight=True,  # Skip to avoid false failures
                    preflight_commitment=Confirmed,
                    max_retries=3
                )
            )
            
            if result.value:
                sig = str(result.value)
                logging.info(f"[Jupiter] Transaction sent: {sig}")
                
                # Quick confirmation check
                time.sleep(2)  # Wait 2 seconds
                
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
                            return None
                except Exception as e:
                    logging.debug(f"[Jupiter] Status check not critical: {e}")
                
                return sig  # Return the signature anyway
            else:
                logging.error(f"[Jupiter] Failed to send transaction: {result}")
                return None
                
        except Exception as e:
            logging.error(f"[Jupiter] Send error: {e}")
            # Log more details
            import traceback
            logging.error(traceback.format_exc())
            return None
            
    except Exception as e:
        logging.error(f"[Jupiter] Swap execution error: {e}")
        import traceback
        logging.error(traceback.format_exc())
        return None

async def execute_jupiter_sell(mint: str, amount: int) -> Optional[str]:
    """Execute a sell using Jupiter"""
    try:
        input_mint = mint
        output_mint = "So11111111111111111111111111111111111111112"  # SOL
        
        # Get quote
        logging.info(f"[Jupiter] Getting sell quote for {mint[:8]}...")
        quote = await get_jupiter_quote(input_mint, output_mint, amount)
        if not quote:
            return None
        
        # Get swap transaction
        swap_data = await get_jupiter_swap_transaction(quote, wallet_pubkey)
        if not swap_data:
            return None
        
        # Sign and send - SAME FIX AS BUY
        tx_bytes = base64.b64decode(swap_data["swapTransaction"])
        tx = VersionedTransaction.from_bytes(tx_bytes)
        
        # The transaction needs to be signed with our keypair
        signed_tx = VersionedTransaction(tx.message, [keypair])
        
        # Send transaction
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
                
                # Quick confirmation check
                time.sleep(2)
                
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
            import traceback
            logging.error(traceback.format_exc())
            return None
            
    except Exception as e:
        logging.error(f"[Jupiter] Sell execution error: {e}")
        return None

async def buy_token(mint: str):
    """Execute buy transaction for a token - NOW WITH JUPITER!"""
    amount = int(BUY_AMOUNT_SOL * 1e9)  # Convert SOL to lamports

    try:
        if mint in BROKEN_TOKENS:
            await send_telegram_alert(f"❌ Skipped {mint} — broken token")
            log_skipped_token(mint, "Broken token")
            record_skip("malformed")
            return False

        increment_stat("snipes_attempted", 1)
        update_last_activity()

        # ========== TRY JUPITER FIRST (works for 95% of tokens) ==========
        logging.info(f"[Buy] Attempting Jupiter swap for {mint[:8]}...")
        jupiter_sig = await execute_jupiter_swap(mint, amount)
        
        if jupiter_sig:
            await send_telegram_alert(
                f"✅ Sniped {mint} via Jupiter — bought with {BUY_AMOUNT_SOL} SOL\n"
                f"TX: https://solscan.io/tx/{jupiter_sig}"
            )
            
            OPEN_POSITIONS[mint] = {
                "expected_token_amount": 0,
                "buy_amount_sol": BUY_AMOUNT_SOL,
                "sold_stages": set(),
                "buy_sig": jupiter_sig
            }
            
            increment_stat("snipes_succeeded", 1)
            log_trade(mint, "BUY", BUY_AMOUNT_SOL, 0)
            return True
        
        # ========== FALLBACK TO RAYDIUM (for new tokens not on Jupiter yet) ==========
        logging.info(f"[Buy] Jupiter failed, trying Raydium for {mint[:8]}...")
        
        input_mint = "So11111111111111111111111111111111111111112"  # SOL
        output_mint = mint
        
        # Check if pool exists
        pool = raydium.find_pool(input_mint, output_mint)
        if not pool:
            # Try reverse direction
            pool = raydium.find_pool(output_mint, input_mint)
            if not pool:
                await send_telegram_alert(f"⚠️ No pool found on Jupiter or Raydium for {mint}. Skipping.")
                log_skipped_token(mint, "No pool on Jupiter or Raydium")
                record_skip("malformed")
                return False

        # Build swap transaction
        tx = raydium.build_swap_transaction(keypair, input_mint, output_mint, amount)
        if not tx:
            await send_telegram_alert(f"❌ Failed to build Raydium swap TX for {mint}")
            mark_broken_token(mint, 0)
            return False

        # Send transaction
        sig = raydium.send_transaction(tx, keypair)
        if not sig:
            await send_telegram_alert(f"📉 Trade failed — TX send error for {mint}")
            mark_broken_token(mint, 0)
            return False

        await send_telegram_alert(
            f"✅ Sniped {mint} via Raydium — bought with {BUY_AMOUNT_SOL} SOL\n"
            f"TX: https://solscan.io/tx/{sig}"
        )
        
        OPEN_POSITIONS[mint] = {
            "expected_token_amount": 0,
            "buy_amount_sol": BUY_AMOUNT_SOL,
            "sold_stages": set(),
            "buy_sig": sig
        }
        
        increment_stat("snipes_succeeded", 1)
        log_trade(mint, "BUY", BUY_AMOUNT_SOL, 0)
        return True

    except Exception as e:
        await send_telegram_alert(f"❌ Buy failed for {mint}: {e}")
        log_skipped_token(mint, f"Buy failed: {e}")
        return False

async def sell_token(mint: str, percent: float = 100.0):
    """Execute sell transaction for a token - NOW WITH JUPITER!"""
    try:
        # Get token balance
        owner = keypair.pubkey()
        token_account = get_associated_token_address(owner, Pubkey.from_string(mint))
        
        # Get actual balance - FIXED ERROR HANDLING
        try:
            response = rpc.get_token_account_balance(token_account)
            if not response or not hasattr(response, 'value') or not response.value:
                logging.warning(f"No token balance found for {mint}")
                await send_telegram_alert(f"⚠️ No token balance found for {mint}")
                return False
            
            balance = int(response.value.amount)
        except Exception as e:
            logging.error(f"Failed to get token balance for {mint}: {e}")
            await send_telegram_alert(f"⚠️ Failed to get balance for {mint}: {e}")
            return False
        
        amount = int(balance * percent / 100)
        
        if amount == 0:
            await send_telegram_alert(f"⚠️ Zero balance to sell for {mint}")
            return False

        # ========== TRY JUPITER FIRST ==========
        logging.info(f"[Sell] Attempting Jupiter sell for {mint[:8]}...")
        jupiter_sig = await execute_jupiter_sell(mint, amount)
        
        if jupiter_sig:
            await send_telegram_alert(
                f"✅ Sold {percent}% of {mint} via Jupiter\n"
                f"TX: https://solscan.io/tx/{jupiter_sig}"
            )
            log_trade(mint, f"SELL {percent}%", 0, amount)
            increment_stat("sells_executed", 1)
            return True
        
        # ========== FALLBACK TO RAYDIUM ==========
        logging.info(f"[Sell] Jupiter failed, trying Raydium for {mint[:8]}...")
        
        input_mint = mint
        output_mint = "So11111111111111111111111111111111111111112"  # SOL
        
        # Find pool
        pool = raydium.find_pool(input_mint, output_mint)
        if not pool:
            pool = raydium.find_pool(output_mint, input_mint)
            if not pool:
                await send_telegram_alert(f"⚠️ No pool for {mint}. Cannot sell.")
                log_skipped_token(mint, "No pool for sell")
                return False

        # Build and send transaction
        tx = raydium.build_swap_transaction(keypair, input_mint, output_mint, amount)
        if not tx:
            await send_telegram_alert(f"❌ Failed to build sell TX for {mint}")
            return False

        sig = raydium.send_transaction(tx, keypair)
        if not sig:
            await send_telegram_alert(f"❌ Failed to send sell tx for {mint}")
            return False

        await send_telegram_alert(
            f"✅ Sold {percent}% of {mint} via Raydium\n"
            f"TX: https://solscan.io/tx/{sig}"
        )
        log_trade(mint, f"SELL {percent}%", 0, amount)
        increment_stat("sells_executed", 1)
        return True
        
    except Exception as e:
        await send_telegram_alert(f"❌ Sell failed for {mint}: {e}")
        log_skipped_token(mint, f"Sell failed: {e}")
        return False

async def get_token_price_usd(mint: str) -> Optional[float]:
    """Get current token price in USD - ELITE VERSION optimized for what actually works"""
    try:
        # Try DexScreener FIRST (most reliable for new tokens and no DNS issues)
        try:
            dex_url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
            
            async with httpx.AsyncClient(timeout=10, verify=False) as client:
                response = await client.get(dex_url)
                if response.status_code == 200:
                    data = response.json()
                    if "pairs" in data and len(data["pairs"]) > 0:
                        # Get the pair with highest liquidity
                        pairs = sorted(data["pairs"], key=lambda x: float(x.get("liquidity", {}).get("usd", 0)), reverse=True)
                        if pairs[0].get("priceUsd"):
                            price = float(pairs[0]["priceUsd"])
                            if price > 0:
                                logging.info(f"[Price] {mint[:8]}... = ${price:.8f}")
                                return price
        except Exception as e:
            logging.debug(f"[Price] DexScreener error: {e}")
        
        # Try Birdeye if configured (great for established tokens)
        if BIRDEYE_API_KEY:
            try:
                url = f"https://public-api.birdeye.so/defi/price?address={mint}"
                
                async with httpx.AsyncClient(timeout=10, verify=False) as client:
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
        
        # Only try Jupiter as LAST resort (since it has DNS issues on Render)
        # But use the quote API which might work better than price API
        try:
            # Get a quote for 0.001 SOL to derive price
            quote_url = f"{JUPITER_BASE_URL}/v6/quote"
            params = {
                "inputMint": "So11111111111111111111111111111111111111112",
                "outputMint": mint,
                "amount": str(int(0.001 * 1e9)),
                "slippageBps": "100"
            }
            
            async with httpx.AsyncClient(timeout=5, verify=False) as client:
                response = await client.get(quote_url, params=params)
                if response.status_code == 200:
                    quote = response.json()
                    if "outAmount" in quote and float(quote["outAmount"]) > 0:
                        # Calculate price assuming SOL = $150
                        sol_price = 150.0
                        tokens_received = float(quote["outAmount"]) / 1e9
                        sol_spent = 0.001
                        price = (sol_spent * sol_price) / tokens_received
                        logging.info(f"[Price] {mint[:8]}... = ${price:.8f} (calculated)")
                        return price
        except Exception as e:
            # Don't log Jupiter errors since we know DNS is broken
            pass

async def wait_and_auto_sell(mint: str):
    """Monitor position and auto-sell at REAL profit targets with risk management"""
    try:
        if mint not in OPEN_POSITIONS:
            logging.warning(f"No position found for {mint}")
            return
            
        position = OPEN_POSITIONS[mint]
        buy_amount_sol = position["buy_amount_sol"]
        
        # Wait for token to settle and get initial price
        await asyncio.sleep(10)
        
        # Get entry price (price we bought at)
        entry_price = await get_token_price_usd(mint)
        if not entry_price:
            logging.warning(f"Could not get entry price for {mint}, using timer-based fallback")
            # Fall back to timer-based selling if we can't get prices
            await wait_and_auto_sell_timer_based(mint)
            return
            
        # Initialize tracking variables
        position["entry_price"] = entry_price
        position["highest_price"] = entry_price
        position["token_amount"] = position.get("expected_token_amount", 0)
        
        await send_telegram_alert(
            f"📊 Monitoring {mint[:8]}...\n"
            f"Entry: ${entry_price:.6f}\n"
            f"Targets: {TAKE_PROFIT_1}x/${entry_price*TAKE_PROFIT_1:.6f}, "
            f"{TAKE_PROFIT_2}x/${entry_price*TAKE_PROFIT_2:.6f}, "
            f"{TAKE_PROFIT_3}x/${entry_price*TAKE_PROFIT_3:.6f}\n"
            f"Stop Loss: -${entry_price*STOP_LOSS_PERCENT/100:.6f}"
        )
        
        # Monitor loop
        start_time = time.time()
        last_price_check = 0
        max_sell_attempts = 3
        sell_attempts = {"profit1": 0, "profit2": 0, "profit3": 0, "stop_loss": 0}
        
        while time.time() - start_time < MAX_HOLD_TIME_SEC:
            try:
                # Check price at intervals
                if time.time() - last_price_check < PRICE_CHECK_INTERVAL_SEC:
                    await asyncio.sleep(1)
                    continue
                    
                last_price_check = time.time()
                current_price = await get_token_price_usd(mint)
                
                if not current_price:
                    logging.debug(f"Could not get price for {mint}, skipping this check")
                    await asyncio.sleep(PRICE_CHECK_INTERVAL_SEC)
                    continue
                
                # Calculate profit metrics
                profit_multiplier = current_price / entry_price
                profit_percent = (profit_multiplier - 1) * 100
                
                # Update highest price for trailing stop
                if current_price > position["highest_price"]:
                    position["highest_price"] = current_price
                    logging.info(f"[{mint[:8]}] New high: ${current_price:.6f} ({profit_multiplier:.2f}x)")
                
                # Check for trailing stop
                drop_from_high = (position["highest_price"] - current_price) / position["highest_price"] * 100
                if drop_from_high >= TRAILING_STOP_PERCENT and len(position["sold_stages"]) > 0:
                    logging.info(f"[{mint[:8]}] Trailing stop triggered! Down {drop_from_high:.1f}% from peak")
                    if await sell_token(mint, 100):  # Sell all remaining
                        await send_telegram_alert(
                            f"⛔ Trailing stop triggered for {mint[:8]}!\n"
                            f"Price dropped {drop_from_high:.1f}% from peak ${position['highest_price']:.6f}\n"
                            f"Sold remaining position at ${current_price:.6f}"
                        )
                        break
                
                # Check stop loss
                if profit_percent <= -STOP_LOSS_PERCENT and sell_attempts["stop_loss"] < max_sell_attempts:
                    sell_attempts["stop_loss"] += 1
                    logging.info(f"[{mint[:8]}] Stop loss triggered at {profit_percent:.1f}%")
                    if await sell_token(mint, 100):  # Sell everything
                        await send_telegram_alert(
                            f"🛑 Stop loss triggered for {mint[:8]}!\n"
                            f"Loss: {profit_percent:.1f}% (${current_price:.6f})\n"
                            f"Sold all to minimize losses"
                        )
                        break
                
                # Check profit targets
                # Target 1 (e.g., 2x)
                if profit_multiplier >= TAKE_PROFIT_1 and "profit1" not in position["sold_stages"] and sell_attempts["profit1"] < max_sell_attempts:
                    sell_attempts["profit1"] += 1
                    if await sell_token(mint, SELL_PERCENT_1):
                        position["sold_stages"].add("profit1")
                        await send_telegram_alert(
                            f"💰 Hit {TAKE_PROFIT_1}x profit for {mint[:8]}!\n"
                            f"Price: ${current_price:.6f} ({profit_multiplier:.2f}x)\n"
                            f"Sold {SELL_PERCENT_1}% of position"
                        )
                
                # Target 2 (e.g., 5x)
                if profit_multiplier >= TAKE_PROFIT_2 and "profit2" not in position["sold_stages"] and sell_attempts["profit2"] < max_sell_attempts:
                    sell_attempts["profit2"] += 1
                    if await sell_token(mint, SELL_PERCENT_2):
                        position["sold_stages"].add("profit2")
                        await send_telegram_alert(
                            f"🚀 Hit {TAKE_PROFIT_2}x profit for {mint[:8]}!\n"
                            f"Price: ${current_price:.6f} ({profit_multiplier:.2f}x)\n"
                            f"Sold {SELL_PERCENT_2}% of position"
                        )
                
                # Target 3 (e.g., 10x)
                if profit_multiplier >= TAKE_PROFIT_3 and "profit3" not in position["sold_stages"] and sell_attempts["profit3"] < max_sell_attempts:
                    sell_attempts["profit3"] += 1
                    if await sell_token(mint, SELL_PERCENT_3):
                        position["sold_stages"].add("profit3")
                        await send_telegram_alert(
                            f"🌙 Hit {TAKE_PROFIT_3}x profit for {mint[:8]}!\n"
                            f"Price: ${current_price:.6f} ({profit_multiplier:.2f}x)\n"
                            f"Sold final {SELL_PERCENT_3}% of position\n"
                            f"Total profit: {(profit_multiplier-1)*100:.1f}%!"
                        )
                        break  # All sold
                
                # Log current status every minute
                if int((time.time() - start_time) % 60) == 0:
                    logging.info(
                        f"[{mint[:8]}] Price: ${current_price:.6f} ({profit_multiplier:.2f}x) | "
                        f"High: ${position['highest_price']:.6f} | "
                        f"Sold stages: {position['sold_stages']}"
                    )
                
                # Check if all targets hit
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
                    f"Final P&L: {profit_percent:+.1f}%"
                )
        
        # Clean up position
        if mint in OPEN_POSITIONS:
            del OPEN_POSITIONS[mint]
            
    except Exception as e:
        logging.error(f"Auto-sell error for {mint}: {e}")
        await send_telegram_alert(f"⚠️ Auto-sell error for {mint}: {e}")
        # Clean up position even on error
        if mint in OPEN_POSITIONS:
            del OPEN_POSITIONS[mint]

async def wait_and_auto_sell_timer_based(mint: str):
    """FALLBACK: Original timer-based selling if price feed fails"""
    try:
        if mint not in OPEN_POSITIONS:
            return
            
        position = OPEN_POSITIONS[mint]
        
        # Original timer-based logic (as backup)
        start_time = time.time()
        max_duration = 600
        max_sell_attempts = 3
        sell_attempts = {"2x": 0, "5x": 0, "10x": 0}
        
        while time.time() - start_time < max_duration:
            try:
                elapsed = time.time() - start_time
                
                # Timer-based sells (original logic)
                if elapsed > 30 and "2x" not in position["sold_stages"] and sell_attempts["2x"] < max_sell_attempts:
                    sell_attempts["2x"] += 1
                    if await sell_token(mint, AUTO_SELL_PERCENT_2X):
                        position["sold_stages"].add("2x")
                        await send_telegram_alert(f"📈 Sold {AUTO_SELL_PERCENT_2X}% at 30s timer for {mint}")
                    elif sell_attempts["2x"] >= max_sell_attempts:
                        position["sold_stages"].add("2x")
                
                if elapsed > 120 and "5x" not in position["sold_stages"] and sell_attempts["5x"] < max_sell_attempts:
                    sell_attempts["5x"] += 1
                    if await sell_token(mint, AUTO_SELL_PERCENT_5X):
                        position["sold_stages"].add("5x")
                        await send_telegram_alert(f"🚀 Sold {AUTO_SELL_PERCENT_5X}% at 2min timer for {mint}")
                    elif sell_attempts["5x"] >= max_sell_attempts:
                        position["sold_stages"].add("5x")
                
                if elapsed > 300 and "10x" not in position["sold_stages"] and sell_attempts["10x"] < max_sell_attempts:
                    sell_attempts["10x"] += 1
                    if await sell_token(mint, AUTO_SELL_PERCENT_10X):
                        position["sold_stages"].add("10x")
                        await send_telegram_alert(f"🌙 Sold final {AUTO_SELL_PERCENT_10X}% at 5min timer for {mint}")
                        break
                    elif sell_attempts["10x"] >= max_sell_attempts:
                        position["sold_stages"].add("10x")
                        break
                
                if len(position["sold_stages"]) >= 3:
                    break
                    
                await asyncio.sleep(10)
                
            except Exception as e:
                logging.error(f"Timer-based monitoring error for {mint}: {e}")
                await asyncio.sleep(10)
        
        # Clean up position
        if mint in OPEN_POSITIONS:
            del OPEN_POSITIONS[mint]
            
    except Exception as e:
        logging.error(f"Timer-based auto-sell error for {mint}: {e}")
        # Clean up position even on error
        if mint in OPEN_POSITIONS:
            del OPEN_POSITIONS[mint]
