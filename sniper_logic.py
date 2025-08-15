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

from utils import (
    is_valid_mint, buy_token, log_skipped_token, send_telegram_alert,
    get_trending_mints, wait_and_auto_sell, get_liquidity_and_ownership,
    is_bot_running, keypair, BUY_AMOUNT_SOL, BROKEN_TOKENS,
    mark_broken_token, daily_stats_reset_loop,
    update_last_activity, increment_stat, record_skip,
    listener_status, last_seen_token
)
from solders.pubkey import Pubkey

load_dotenv()

FORCE_TEST_MINT = os.getenv("FORCE_TEST_MINT")
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
HELIUS_API = os.getenv("HELIUS_API")
RUG_LP_THRESHOLD = float(os.getenv("RUG_LP_THRESHOLD", 2.0))
RISKY_LP_THRESHOLD = 0.5
TREND_SCAN_INTERVAL = int(os.getenv("TREND_SCAN_INTERVAL", 60))
RPC_URL = os.getenv("RPC_URL")
SLIPPAGE_BPS = 100
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")

# Position sizing
SAFE_BUY_AMOUNT = float(os.getenv("BUY_AMOUNT_SOL", 0.03))
RISKY_BUY_AMOUNT = 0.01
ULTRA_RISKY_BUY_AMOUNT = 0.005

# PumpFun Migration Settings
PUMPFUN_MIGRATION_BUY = float(os.getenv("PUMPFUN_MIGRATION_BUY", 0.05))  # Higher amount for migrations
PUMPFUN_GRADUATION_MC = 69420  # ~$69k market cap trigger
ENABLE_PUMPFUN_MIGRATION = os.getenv("ENABLE_PUMPFUN_MIGRATION", "true").lower() == "true"

seen_tokens = set()
BLACKLIST = set()
TASKS = []

# Track PumpFun tokens and their migration status
pumpfun_tokens = {}  # mint -> {"discovered": timestamp, "migrated": bool, "pool_id": str}
migration_watch_list = set()  # Tokens close to graduation

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

async def check_pumpfun_graduation(mint: str) -> bool:
    """Check if a PumpFun token is ready to graduate"""
    try:
        # Check market cap via API
        url = f"https://frontend-api.pump.fun/coins/{mint}"
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(url)
            if response.status_code == 200:
                data = response.json()
                market_cap = data.get("usd_market_cap", 0)
                
                # Check if approaching graduation threshold
                if market_cap > PUMPFUN_GRADUATION_MC * 0.9:  # 90% of graduation
                    logging.info(f"[PumpFun] {mint[:8]}... approaching graduation: ${market_cap:.0f}")
                    return True
    except Exception as e:
        logging.debug(f"[PumpFun] Graduation check error: {e}")
    
    return False

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
            
            # Check tokens on watch list
            for mint in list(migration_watch_list):
                if mint in pumpfun_tokens and not pumpfun_tokens[mint].get("migrated", False):
                    # Check if it has migrated to Raydium
                    lp_data = await get_liquidity_and_ownership(mint)
                    
                    if lp_data and lp_data.get("liquidity", 0) > 0:
                        # MIGRATION DETECTED!
                        pumpfun_tokens[mint]["migrated"] = True
                        migration_watch_list.discard(mint)
                        
                        await send_telegram_alert(
                            f"🚨 PUMPFUN MIGRATION DETECTED 🚨\n\n"
                            f"Token: `{mint}`\n"
                            f"Status: Graduated to Raydium!\n"
                            f"Liquidity: {lp_data.get('liquidity', 0):.2f} SOL\n"
                            f"Action: SNIPING NOW!"
                        )
                        
                        # Execute migration snipe with higher amount
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
            
            # Periodically check PumpFun API for tokens near graduation
            if int(time.time()) % 60 == 0:  # Every minute
                await scan_pumpfun_graduations()
            
            await asyncio.sleep(5)  # Check every 5 seconds
            
        except Exception as e:
            logging.error(f"[Migration Monitor] Error: {e}")
            await asyncio.sleep(10)

async def scan_pumpfun_graduations():
    """Scan PumpFun for tokens about to graduate"""
    try:
        # Get trending PumpFun tokens
        url = "https://frontend-api.pump.fun/coins?offset=0&limit=50&sort=usd_market_cap&order=desc"
        
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url)
            if response.status_code == 200:
                coins = response.json()
                
                for coin in coins[:20]:  # Check top 20 by market cap
                    mint = coin.get("mint")
                    market_cap = coin.get("usd_market_cap", 0)
                    
                    if not mint:
                        continue
                    
                    # Track if approaching graduation
                    if market_cap > PUMPFUN_GRADUATION_MC * 0.8:  # 80% of graduation threshold
                        if mint not in pumpfun_tokens:
                            pumpfun_tokens[mint] = {
                                "discovered": time.time(),
                                "migrated": False,
                                "market_cap": market_cap
                            }
                        
                        if mint not in migration_watch_list:
                            migration_watch_list.add(mint)
                            logging.info(f"[PumpFun] Added {mint[:8]}... to migration watch (MC: ${market_cap:.0f})")
                            
                            if market_cap > PUMPFUN_GRADUATION_MC * 0.95:  # 95% - very close!
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
    """Enhanced mempool listener with PumpFun tracking - FIXED POOL DETECTION"""
    if not HELIUS_API:
        logging.warning(f"[{name}] HELIUS_API not set, skipping mempool listener")
        await send_telegram_alert(f"⚠️ {name} listener disabled (no Helius API key)")
        return
    
    url = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API}"
    retry_attempts = 0
    max_retries = 10
    retry_delay = 10
    heartbeat_interval = 30
    max_inactive = 300
    
    # Set program ID based on listener name
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
                        
                        # FIXED: Better pool creation detection
                        is_pool_creation = False
                        for log in logs:
                            log_lower = log.lower()
                            
                            if name == "Raydium":
                                # FIXED: Correct Raydium pool creation keywords
                                if any(x in log_lower for x in [
                                    "initialize2",           # Main pool initialization
                                    "init_pc_amount",        # Pool creation with quote amount
                                    "init_coin_amount",      # Pool creation with base amount
                                    "initializepool",        # Alternative initialization
                                    "create pool",           # Direct pool creation
                                    "add liquidity"          # Initial liquidity add
                                ]):
                                    is_pool_creation = True
                                    
                                    # Special check: Is this a PumpFun migration?
                                    for key in account_keys:
                                        if key in pumpfun_tokens and not pumpfun_tokens[key].get("migrated", False):
                                            # This is a PumpFun token creating a Raydium pool!
                                            pumpfun_tokens[key]["migrated"] = True
                                            migration_watch_list.discard(key)
                                            
                                            await send_telegram_alert(
                                                f"🎊 PUMPFUN GRADUATION DETECTED 🎊\n\n"
                                                f"Token: `{key}`\n"
                                                f"Event: Creating Raydium Pool NOW!\n"
                                                f"Action: PRIORITY SNIPE!"
                                            )
                                            
                                            # Use higher amount for migration
                                            original_amount = os.getenv("BUY_AMOUNT_SOL")
                                            os.environ["BUY_AMOUNT_SOL"] = str(PUMPFUN_MIGRATION_BUY)
                                            
                                            try:
                                                success = await buy_token(key)
                                                if success:
                                                    await send_telegram_alert(
                                                        f"✅ GRADUATION SNIPE SUCCESS!\n"
                                                        f"Token: {key[:16]}...\n"
                                                        f"Amount: {PUMPFUN_MIGRATION_BUY} SOL\n"
                                                        f"Type: PumpFun Graduation"
                                                    )
                                                    asyncio.create_task(wait_and_auto_sell(key))
                                            finally:
                                                if original_amount:
                                                    os.environ["BUY_AMOUNT_SOL"] = original_amount
                                            break
                                    break
                            
                            elif name == "Jupiter":
                                # FIXED: Correct Jupiter pool creation keywords
                                if any(x in log_lower for x in [
                                    "initialize_pool",       # Jupiter pool initialization
                                    "create_pool",          # Direct pool creation
                                    "createpool",           # Alternative format
                                    "pool_created",         # Pool creation event
                                    "new_pool"              # New pool event
                                ]):
                                    is_pool_creation = True
                                    break
                            
                            elif name == "PumpFun":
                                if "create" in log_lower:
                                    is_pool_creation = True
                                    break
                            
                            elif name == "Moonshot":
                                if "launch" in log_lower or "initialize" in log_lower:
                                    is_pool_creation = True
                                    break
                        
                        if not is_pool_creation:
                            continue
                        
                        pool_creations_found += 1
                        logging.info(f"[{name}] POOL CREATION DETECTED! Total found: {pool_creations_found}")
                        
                        # FIXED: Better token mint detection - DON'T SKIP SEEN TOKENS FOR RAYDIUM/JUPITER
                        potential_mints = []
                        
                        for i, key in enumerate(account_keys):
                            if key in SYSTEM_PROGRAMS:
                                continue
                            if key == "So11111111111111111111111111111111111111112":
                                continue
                            if len(key) != 44:
                                continue
                            
                            # CRITICAL FIX: Don't skip seen tokens for Raydium/Jupiter
                            # They might be PumpFun graduations we've seen before!
                            if name in ["Raydium", "Jupiter"]:
                                # For Raydium/Jupiter, always process even if seen
                                # This catches PumpFun graduations
                                try:
                                    Pubkey.from_string(key)
                                    potential_mints.append(key)
                                except:
                                    continue
                            else:
                                # For PumpFun/Moonshot, skip if already seen
                                if key in seen_tokens:
                                    continue
                                try:
                                    Pubkey.from_string(key)
                                    potential_mints.append(key)
                                except:
                                    continue
                        
                        # Process potential mints
                        for potential_mint in potential_mints:
                            # Add to seen_tokens AFTER we process it (not before)
                            if potential_mint not in seen_tokens:
                                seen_tokens.add(potential_mint)
                            
                            # Track PumpFun tokens
                            if name == "PumpFun" and potential_mint not in pumpfun_tokens:
                                pumpfun_tokens[potential_mint] = {
                                    "discovered": time.time(),
                                    "migrated": False
                                }
                                logging.info(f"[PumpFun] Tracking new token: {potential_mint[:8]}...")
                            
                            logging.info(f"[{name}] Found potential token: {potential_mint[:8]}...")
                            increment_stat("tokens_scanned", 1)
                            update_last_activity()
                            
                            # Only buy on tradeable platforms
                            if name in ["Raydium", "Jupiter"] and is_bot_running():
                                if potential_mint not in BROKEN_TOKENS and potential_mint not in BLACKLIST:
                                    
                                    # Smart balance LP check
                                    await asyncio.sleep(0.2)
                                    
                                    lp_amount = 0
                                    risk_level = "UNKNOWN"
                                    buy_amount = ULTRA_RISKY_BUY_AMOUNT
                                    
                                    try:
                                        lp_check_task = asyncio.create_task(get_liquidity_and_ownership(potential_mint))
                                        lp_data = await asyncio.wait_for(lp_check_task, timeout=0.5)
                                        
                                        if lp_data:
                                            lp_amount = lp_data.get("liquidity", 0)
                                            
                                            if lp_amount >= RUG_LP_THRESHOLD:
                                                risk_level = "SAFE"
                                                buy_amount = SAFE_BUY_AMOUNT
                                            elif lp_amount >= RISKY_LP_THRESHOLD:
                                                risk_level = "MEDIUM"
                                                buy_amount = RISKY_BUY_AMOUNT
                                            elif lp_amount > 0:
                                                risk_level = "HIGH"
                                                buy_amount = ULTRA_RISKY_BUY_AMOUNT
                                            else:
                                                risk_level = "EXTREME"
                                                buy_amount = ULTRA_RISKY_BUY_AMOUNT
                                    except asyncio.TimeoutError:
                                        logging.info(f"[{name}] LP check timeout - proceeding with minimal amount")
                                        risk_level = "TIMEOUT"
                                        buy_amount = ULTRA_RISKY_BUY_AMOUNT
                                    except Exception as e:
                                        logging.debug(f"[{name}] LP check error: {e}")
                                        risk_level = "ERROR"
                                        buy_amount = ULTRA_RISKY_BUY_AMOUNT
                                    
                                    logging.info(f"[{name}] Risk Assessment:")
                                    logging.info(f"  Liquidity: {lp_amount:.2f} SOL")
                                    logging.info(f"  Risk Level: {risk_level}")
                                    logging.info(f"  Buy Amount: {buy_amount} SOL")
                                    
                                    risk_emoji = {
                                        "SAFE": "✅",
                                        "MEDIUM": "⚠️",
                                        "HIGH": "🔥",
                                        "EXTREME": "☠️",
                                        "TIMEOUT": "⏱️",
                                        "ERROR": "❓",
                                        "UNKNOWN": "❓"
                                    }.get(risk_level, "❓")
                                    
                                    await send_telegram_alert(
                                        f"{risk_emoji} NEW TOKEN DETECTED {risk_emoji}\n\n"
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
                                            await send_telegram_alert(
                                                f"✅ SNIPED ({risk_level} RISK)!\n"
                                                f"Token: {potential_mint[:16]}...\n"
                                                f"Amount: {buy_amount} SOL\n"
                                                f"Monitoring for profits..."
                                            )
                                            asyncio.create_task(wait_and_auto_sell(potential_mint))
                                            break
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
                                        
                            else:
                                # For PumpFun/Moonshot, just track but don't buy
                                if name in ["PumpFun", "Moonshot"]:
                                    logging.info(f"[{name}] New launch tracked: {potential_mint[:8]}...")
                                
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

MIN_LP_USD = 5000
MIN_VOLUME_USD = 10000
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
                
                if not mint or mint in seen_trending or mint in BLACKLIST or mint in BROKEN_TOKENS:
                    continue
                
                # Check if it's a graduated PumpFun token
                is_pumpfun_grad = mint in pumpfun_tokens and pumpfun_tokens[mint].get("migrated", False)
                
                # Lower thresholds for PumpFun graduations
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
                
                # Always consider PumpFun graduations as quality
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
                        # Use higher amount for PumpFun graduates
                        original_amount = None
                        if is_pumpfun_grad:
                            original_amount = os.getenv("BUY_AMOUNT_SOL")
                            os.environ["BUY_AMOUNT_SOL"] = str(PUMPFUN_MIGRATION_BUY)
                        
                        success = await buy_token(mint)
                        if success:
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
        min_lp = float(os.getenv("RUG_LP_THRESHOLD", 2.0))
        
        # Lower threshold for PumpFun graduates
        if mint in pumpfun_tokens and pumpfun_tokens[mint].get("migrated", False):
            min_lp = min_lp / 2  # Half the normal threshold
        
        if not data or data.get("liquidity", 0) < min_lp:
            logging.info(f"[RUG CHECK] {mint[:8]}... has {data.get('liquidity', 0):.2f} SOL (min: {min_lp})")
            return False
        return True
    except Exception as e:
        logging.error(f"Rug check error for {mint}: {e}")
        return False

async def start_sniper():
    """Start the ELITE sniper bot with PumpFun migration detection"""
    mode_text = "Smart Balance + Migration Sniper" if ENABLE_PUMPFUN_MIGRATION else "Smart Balance"
    
    await send_telegram_alert(
        f"🚀 SNIPER LAUNCHING 🚀\n\n"
        f"Mode: {mode_text}\n"
        f"Safe LP: 2+ SOL = 0.03 SOL buy\n"
        f"Medium LP: 0.5-2 SOL = 0.01 SOL buy\n"
        f"Low LP: <0.5 SOL = 0.005 SOL buy\n"
        f"Migration Snipe: {PUMPFUN_MIGRATION_BUY} SOL\n"
        f"Targets: 2x/5x/10x\n\n"
        f"Ready to catch launches & migrations! 🎯"
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
    TASKS.extend([
        asyncio.create_task(mempool_listener("Raydium")),
        asyncio.create_task(mempool_listener("Jupiter")),
        asyncio.create_task(mempool_listener("PumpFun")),
        asyncio.create_task(mempool_listener("Moonshot")),
        asyncio.create_task(trending_scanner())
    ])
    
    # Add PumpFun migration monitor
    if ENABLE_PUMPFUN_MIGRATION:
        TASKS.append(asyncio.create_task(pumpfun_migration_monitor()))
        await send_telegram_alert("🎯 PumpFun Migration Monitor: ACTIVE")
    
    await send_telegram_alert(f"🎯 ALL SYSTEMS ACTIVE - {mode_text}!")

async def start_sniper_with_forced_token(mint: str):
    """Force buy a specific token"""
    try:
        await send_telegram_alert(f"🚨 FORCE BUY: {mint}")
        
        if not is_bot_running():
            await send_telegram_alert(f"⛔ Bot is paused. Cannot force buy {mint}")
            return

        if mint in BROKEN_TOKENS or mint in BLACKLIST:
            await send_telegram_alert(f"❌ {mint} is blacklisted or broken")
            return

        # Check if it's a PumpFun token
        is_pumpfun = mint in pumpfun_tokens
        buy_amount = PUMPFUN_MIGRATION_BUY if is_pumpfun else BUY_AMOUNT_SOL
        
        logging.info(f"[FORCEBUY] Attempting forced buy for {mint} with {buy_amount} SOL")

        original_amount = os.getenv("BUY_AMOUNT_SOL")
        os.environ["BUY_AMOUNT_SOL"] = str(buy_amount)
        
        try:
            result = await buy_token(mint)
            if result:
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
