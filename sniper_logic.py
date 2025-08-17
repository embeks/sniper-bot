import asyncio
import json
import os
import websockets
import logging
import time
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

FORCE_TEST_MINT = os.getenv("FORCE_TEST_MINT")
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
HELIUS_API = os.getenv("HELIUS_API")
RUG_LP_THRESHOLD = float(os.getenv("RUG_LP_THRESHOLD", 15.0))  # Raised to 15
RISKY_LP_THRESHOLD = 5.0  # Raised from 0.5
TREND_SCAN_INTERVAL = int(os.getenv("TREND_SCAN_INTERVAL", 60))
RPC_URL = os.getenv("RPC_URL")
SLIPPAGE_BPS = 100
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")

# Enhanced position sizing
SAFE_BUY_AMOUNT = float(os.getenv("SAFE_BUY_AMOUNT", 0.02))
RISKY_BUY_AMOUNT = float(os.getenv("RISKY_BUY_AMOUNT", 0.01))
ULTRA_RISKY_BUY_AMOUNT = float(os.getenv("ULTRA_RISKY_BUY_AMOUNT", 0.005))

# Quality filters
MIN_AI_SCORE = float(os.getenv("MIN_AI_SCORE", 0.40))
MIN_HOLDER_COUNT = int(os.getenv("MIN_HOLDER_COUNT", 50))
MAX_TOP_HOLDER_PERCENT = float(os.getenv("MAX_TOP_HOLDER_PERCENT", 30))
MIN_BUYS_COUNT = int(os.getenv("MIN_BUYS_COUNT", 20))
MIN_BUY_SELL_RATIO = float(os.getenv("MIN_BUY_SELL_RATIO", 1.5))

# Anti-duplicate settings
DUPLICATE_CHECK_WINDOW = int(os.getenv("DUPLICATE_CHECK_WINDOW", 300))
MAX_BUYS_PER_TOKEN = int(os.getenv("MAX_BUYS_PER_TOKEN", 1))
BLACKLIST_AFTER_BUY = os.getenv("BLACKLIST_AFTER_BUY", "true").lower() == "true"

# Disable Jupiter mempool if configured
SKIP_JUPITER_MEMPOOL = os.getenv("SKIP_JUPITER_MEMPOOL", "true").lower() == "true"

# PumpFun Migration Settings
PUMPFUN_MIGRATION_BUY = float(os.getenv("PUMPFUN_MIGRATION_BUY", 0.1))
PUMPFUN_GRADUATION_MC = 69420
ENABLE_PUMPFUN_MIGRATION = os.getenv("ENABLE_PUMPFUN_MIGRATION", "true").lower() == "true"

seen_tokens = set()
BLACKLIST = set()
TASKS = []

# Enhanced tracking
pumpfun_tokens = {}
migration_watch_list = set()
already_bought = set()
recent_buy_attempts = {}  # token -> timestamp
pool_verification_cache = {}  # token -> is_verified

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

async def fetch_transaction_accounts(signature: str, rpc_url: str = None) -> list:
    """Fetch full transaction details to get account keys"""
    try:
        if not rpc_url:
            rpc_url = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
            if HELIUS_API:
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
                            "encoding": "json",
                            "maxSupportedTransactionVersion": 0
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
                                    elif isinstance(key, dict) and "pubkey" in key:
                                        account_keys.append(key["pubkey"])
                            
                            if "addressTableLookups" in msg:
                                for lookup in msg["addressTableLookups"]:
                                    if "accountKey" in lookup:
                                        account_keys.append(lookup["accountKey"])
                    
                    logging.info(f"[TX FETCH] Got {len(account_keys)} accounts for {signature[:8]}...")
                    return account_keys
        
        return []
        
    except Exception as e:
        logging.error(f"[TX FETCH] Error fetching transaction {signature[:8]}...: {e}")
        return []

async def is_quality_token(mint: str, lp_amount: float) -> tuple[bool, str]:
    """
    Enhanced quality check for tokens
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
        
        # Check minimum liquidity
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
                        
                        # Check volume
                        volume_h24 = float(pair.get("volume", {}).get("h24", 0))
                        if volume_h24 < float(os.getenv("MIN_VOLUME_USD", 50000)):
                            return False, f"Low volume: ${volume_h24:.0f}"
                        
                        # Check buy/sell ratio
                        txns = pair.get("txns", {})
                        buys_h1 = txns.get("h1", {}).get("buys", 0)
                        sells_h1 = txns.get("h1", {}).get("sells", 0)
                        
                        if sells_h1 > 0 and buys_h1 / sells_h1 < MIN_BUY_SELL_RATIO:
                            return False, f"Bad buy/sell ratio: {buys_h1}/{sells_h1}"
                        
                        # Check price change (avoid dumps)
                        price_change_h1 = float(pair.get("priceChange", {}).get("h1", 0))
                        if price_change_h1 < -30:
                            return False, f"Dumping: {price_change_h1:.1f}% in 1h"
                        
                        # Passed all checks
                        return True, "Quality token"
        except:
            pass
        
        # If we can't get DexScreener data but LP is good, allow with caution
        if lp_amount >= RUG_LP_THRESHOLD * 1.5:  # Higher threshold without data
            return True, "High liquidity, no data"
        
        return False, "Failed quality checks"
        
    except Exception as e:
        logging.error(f"Quality check error: {e}")
        return False, "Quality check error"

async def verify_pool_exists(mint: str) -> bool:
    """
    Verify that a real trading pool exists for this token
    """
    try:
        # Check cache first
        if mint in pool_verification_cache:
            return pool_verification_cache[mint]
        
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
    """Enhanced mempool listener with FIXED detection logic"""
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
            await send_telegram_alert(f"📱 {name} listener ACTIVE")
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
                        
                        if name == "Raydium":
                            # STRICT Raydium pool creation detection
                            raydium_indicators = 0
                            has_init_pool = False
                            has_create_pool = False
                            has_liquidity = False
                            
                            for log in logs:
                                log_lower = log.lower()
                                
                                # Look for specific Raydium pool initialization
                                if "initialize" in log_lower and ("pool" in log_lower or "amm" in log_lower):
                                    has_init_pool = True
                                    raydium_indicators += 3
                                
                                # Look for pool creation
                                if "create" in log_lower and ("pool" in log_lower or "amm" in log_lower):
                                    has_create_pool = True
                                    raydium_indicators += 3
                                
                                # Look for liquidity addition (essential for new pools)
                                if "add_liquidity" in log_lower or "deposit" in log_lower:
                                    has_liquidity = True
                                    raydium_indicators += 2
                                
                                # Raydium-specific instruction names
                                if any(x in log_lower for x in ["init_pc_amount", "init_coin_amount", "opentime"]):
                                    raydium_indicators += 2
                            
                            # Need multiple strong indicators for real pool creation
                            if raydium_indicators >= 5 and (has_init_pool or has_create_pool):
                                # Additional validation: check instruction count
                                # Real pool creations have many instructions (usually 10+)
                                if len(logs) >= 10:
                                    is_pool_creation = True
                                    logging.info(f"[RAYDIUM] VERIFIED pool creation - Score: {raydium_indicators}, Logs: {len(logs)}")
                        
                        elif name == "PumpFun":
                            # PumpFun ONLY for NEW token creation, not trades
                            pumpfun_create_indicators = 0
                            
                            for log in logs:
                                log_lower = log.lower()
                                
                                # PumpFun specific creation patterns
                                if "create" in log_lower and ("token" in log_lower or "coin" in log_lower):
                                    pumpfun_create_indicators += 3
                                
                                if "initialize" in log_lower and "mint" in log_lower:
                                    pumpfun_create_indicators += 2
                                
                                # PumpFun uses "launch" for new tokens
                                if "launch" in log_lower:
                                    pumpfun_create_indicators += 3
                                
                                # Bonding curve initialization is key indicator
                                if "bonding" in log_lower and ("init" in log_lower or "create" in log_lower):
                                    pumpfun_create_indicators += 4
                            
                            # PumpFun token creation needs strong indicators
                            # AND should NOT be a simple swap (which has fewer logs)
                            if pumpfun_create_indicators >= 3 and len(logs) >= 5:
                                # Filter out swaps - they have different patterns
                                is_swap = any("swap" in log.lower() or "trade" in log.lower() for log in logs)
                                if not is_swap:
                                    is_pool_creation = True
                                    logging.info(f"[PUMPFUN] NEW TOKEN CREATION - Score: {pumpfun_create_indicators}")
                        
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
                        logging.info(f"[{name}] REAL POOL/TOKEN CREATION DETECTED! Total found: {pool_creations_found}")
                        
                        # Fetch full transaction if needed
                        if len(account_keys) == 0:
                            logging.info(f"[{name}] Fetching full transaction...")
                            account_keys = await fetch_transaction_accounts(signature)
                            
                            if len(account_keys) == 0:
                                logging.warning(f"[{name}] Could not fetch account keys")
                                continue
                        
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
                            
                            # Only buy from tradeable platforms
                            if name in ["Raydium"] and is_bot_running():
                                if potential_mint not in BROKEN_TOKENS and potential_mint not in BLACKLIST:
                                    
                                    # Add delay to let pool settle
                                    await asyncio.sleep(float(os.getenv("MEMPOOL_DELAY_MS", 200)) / 1000)
                                    
                                    # Verify pool exists
                                    if not await verify_pool_exists(potential_mint):
                                        logging.info(f"[{name}] No verified pool for {potential_mint[:8]}...")
                                        continue
                                    
                                    # Get liquidity with timeout
                                    lp_amount = 0
                                    try:
                                        lp_check_task = asyncio.create_task(get_liquidity_and_ownership(potential_mint))
                                        lp_data = await asyncio.wait_for(lp_check_task, timeout=1.0)
                                        
                                        if lp_data:
                                            lp_amount = lp_data.get("liquidity", 0)
                                    except asyncio.TimeoutError:
                                        logging.info(f"[{name}] LP check timeout")
                                        continue
                                    except Exception as e:
                                        logging.debug(f"[{name}] LP check error: {e}")
                                        continue
                                    
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

MIN_LP_USD = float(os.getenv("MIN_LP_USD", 25000))
MIN_VOLUME_USD = float(os.getenv("MIN_VOLUME_USD", 50000))
seen_trending = set()

async def get_trending_pairs_dexscreener():
    """Fetch trending pairs from DexScreener"""
    url = "https://api.dexscreener.com/latest/dex/pairs/solana"
    
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(
                timeout=30, 
                follow_redirects=True,
                verify=False
            ) as client:
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
            async with httpx.AsyncClient(
                timeout=30,
                verify=False
            ) as client:
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
        min_lp = float(os.getenv("RUG_LP_THRESHOLD", 15.0))
        
        if mint in pumpfun_tokens and pumpfun_tokens[mint].get("migrated", False):
            min_lp = min_lp / 2
        
        if not data or data.get("liquidity", 0) < min_lp:
            logging.info(f"[RUG CHECK] {mint[:8]}... has {data.get('liquidity', 0):.2f} SOL (min: {min_lp})")
            return False
        return True
    except Exception as e:
        logging.error(f"Rug check error for {mint}: {e}")
        return False

async def start_sniper():
    """Start the ELITE sniper bot"""
    mode_text = "ELITE Money Printer Mode"
    TASKS.append(asyncio.create_task(start_dexscreener_monitor()))
    
    await send_telegram_alert(
        f"💰 MONEY PRINTER LAUNCHING 💰\n\n"
        f"Mode: {mode_text}\n"
        f"Min LP: {RUG_LP_THRESHOLD} SOL\n"
        f"Min AI Score: {MIN_AI_SCORE}\n"
        f"Min Volume: ${MIN_VOLUME_USD:,.0f}\n"
        f"Migration Snipe: {PUMPFUN_MIGRATION_BUY} SOL\n\n"
        f"Quality filters: ACTIVE ✅\n"
        f"Duplicate prevention: ACTIVE ✅\n"
        f"Pool verification: ACTIVE ✅\n\n"
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
    
    if ENABLE_PUMPFUN_MIGRATION:
        TASKS.append(asyncio.create_task(pumpfun_migration_monitor()))
        TASKS.append(asyncio.create_task(raydium_graduation_scanner()))
        await send_telegram_alert("🎯 PumpFun Migration Monitor: ACTIVE")
        await send_telegram_alert("🎓 Graduation Scanner: ACTIVE")
    
    await send_telegram_alert(f"🎯 MONEY PRINTER ACTIVE - {mode_text}!")

async def start_sniper_with_forced_token(mint: str):
    """Force buy a specific token"""
    try:
        await send_telegram_alert(f"🚨 FORCE BUY: {mint}")
        
        if not is_bot_running():
            await send_telegram_alert(f"⛔ Bot is paused. Cannot force buy {mint}")
            return

        if mint in BROKEN_TOKENS or mint in BLACKLIST or mint in already_bought:
            await send_telegram_alert(f"❌ {mint} is blacklisted, broken, or already bought")
            return

        is_pumpfun = mint in pumpfun_tokens
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
