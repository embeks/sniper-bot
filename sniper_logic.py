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
RUG_LP_THRESHOLD = float(os.getenv("RUG_LP_THRESHOLD", 2.0))  # Normal threshold
RISKY_LP_THRESHOLD = 0.5  # Below this = very risky
TREND_SCAN_INTERVAL = int(os.getenv("TREND_SCAN_INTERVAL", 60))
RPC_URL = os.getenv("RPC_URL")
SLIPPAGE_BPS = 100
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")

# SMART POSITION SIZING
SAFE_BUY_AMOUNT = float(os.getenv("BUY_AMOUNT_SOL", 0.03))  # Full amount for safe pools
RISKY_BUY_AMOUNT = 0.01  # Reduced for risky pools
ULTRA_RISKY_BUY_AMOUNT = 0.005  # Minimal for ultra risky

seen_tokens = set()
BLACKLIST = set()
TASKS = []

last_alert_sent = {"Raydium": 0, "Jupiter": 0, "PumpFun": 0, "Moonshot": 0}
alert_cooldown_sec = 1800

# SYSTEM PROGRAMS TO IGNORE
SYSTEM_PROGRAMS = [
    "11111111111111111111111111111111",
    "ComputeBudget111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # Raydium Authority
    "srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX",  # Serum
]

async def mempool_listener(name, program_id=None):
    """SMART BALANCE VERSION - Fast but with risk management"""
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
            
            # Subscribe to program logs
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
            await send_telegram_alert(f"📱 {name} listener ACTIVE - Smart Balance Mode")
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
            
            # Track processed transactions
            processed_txs = set()
            transaction_counter = 0
            pool_creations_found = 0
            
            # Main message loop
            while True:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=60)
                    data = json.loads(msg)
                    
                    # Update heartbeat
                    last_seen_token[name] = time.time()
                    
                    # Process data
                    if "params" in data:
                        result = data.get("params", {}).get("result", {})
                        value = result.get("value", {})
                        logs = value.get("logs", [])
                        account_keys = value.get("accountKeys", [])
                        signature = value.get("signature", "")
                        
                        # Skip if we've seen this transaction
                        if signature in processed_txs:
                            continue
                        processed_txs.add(signature)
                        
                        # Keep set size manageable
                        if len(processed_txs) > 1000:
                            processed_txs.clear()
                        
                        transaction_counter += 1
                        
                        # Log stats every 100 transactions
                        if transaction_counter % 100 == 0:
                            logging.info(f"[{name}] Processed {transaction_counter} txs, found {pool_creations_found} pool creations")
                        
                        # CHECK FOR POOL CREATION
                        is_pool_creation = False
                        for log in logs:
                            log_lower = log.lower()
                            
                            # Raydium pool creation signatures
                            if name == "Raydium":
                                if any(x in log_lower for x in ["initialize2", "init_pc_amount", "init_coin_amount"]):
                                    is_pool_creation = True
                                    break
                            
                            # Jupiter pool creation
                            elif name == "Jupiter":
                                if "initialize_pool" in log_lower or "create_pool" in log_lower:
                                    is_pool_creation = True
                                    break
                            
                            # PumpFun launch
                            elif name == "PumpFun":
                                if "create" in log_lower:
                                    is_pool_creation = True
                                    break
                            
                            # Moonshot launch
                            elif name == "Moonshot":
                                if "launch" in log_lower or "initialize" in log_lower:
                                    is_pool_creation = True
                                    break
                        
                        if not is_pool_creation:
                            continue
                        
                        pool_creations_found += 1
                        logging.info(f"[{name}] POOL CREATION DETECTED! Total found: {pool_creations_found}")
                        
                        # FIND THE TOKEN MINT - CHECK ALL ACCOUNTS
                        potential_mints = []
                        
                        for i, key in enumerate(account_keys):
                            # Skip obvious non-tokens
                            if key in SYSTEM_PROGRAMS:
                                continue
                            if key == "So11111111111111111111111111111111111111112":  # SOL
                                continue
                            if len(key) != 44:
                                continue
                            if key in seen_tokens:
                                continue
                            
                            # Validate it's a proper address
                            try:
                                Pubkey.from_string(key)
                                # This could be a token mint - add to potential list
                                potential_mints.append(key)
                            except:
                                continue
                        
                        # Process potential mints
                        for potential_mint in potential_mints:
                            if potential_mint in seen_tokens:
                                continue
                                
                            seen_tokens.add(potential_mint)
                            
                            logging.info(f"[{name}] Found potential token: {potential_mint[:8]}...")
                            increment_stat("tokens_scanned", 1)
                            update_last_activity()
                            
                            # Only try to buy on tradeable platforms
                            if name in ["Raydium", "Jupiter"] and is_bot_running():
                                if potential_mint not in BROKEN_TOKENS and potential_mint not in BLACKLIST:
                                    
                                    # ============================================
                                    # SMART BALANCE: QUICK LP CHECK WITH RISK TIERS
                                    # ============================================
                                    
                                    # Wait just 0.2 seconds for pool to initialize
                                    await asyncio.sleep(0.2)
                                    
                                    # Try to check liquidity quickly
                                    lp_amount = 0
                                    risk_level = "UNKNOWN"
                                    buy_amount = ULTRA_RISKY_BUY_AMOUNT  # Default to minimal
                                    
                                    try:
                                        # Quick liquidity check with timeout
                                        lp_check_task = asyncio.create_task(get_liquidity_and_ownership(potential_mint))
                                        lp_data = await asyncio.wait_for(lp_check_task, timeout=0.5)  # 0.5 second timeout
                                        
                                        if lp_data:
                                            lp_amount = lp_data.get("liquidity", 0)
                                            
                                            # Determine risk level and position size
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
                                    
                                    # Log the decision
                                    logging.info(f"[{name}] Risk Assessment:")
                                    logging.info(f"  Liquidity: {lp_amount:.2f} SOL")
                                    logging.info(f"  Risk Level: {risk_level}")
                                    logging.info(f"  Buy Amount: {buy_amount} SOL")
                                    
                                    # Send alert with risk info
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
                                    
                                    # Override buy amount for this specific trade
                                    original_amount = os.getenv("BUY_AMOUNT_SOL")
                                    os.environ["BUY_AMOUNT_SOL"] = str(buy_amount)
                                    
                                    # EXECUTE THE BUY
                                    try:
                                        success = await buy_token(potential_mint)
                                        if success:
                                            await send_telegram_alert(
                                                f"✅ SNIPED ({risk_level} RISK)!\n"
                                                f"Token: {potential_mint[:16]}...\n"
                                                f"Amount: {buy_amount} SOL\n"
                                                f"Monitoring for profits..."
                                            )
                                            # Start monitoring for auto-sell
                                            asyncio.create_task(wait_and_auto_sell(potential_mint))
                                            break  # Only buy one token per pool creation
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
                                        # Restore original buy amount
                                        if original_amount:
                                            os.environ["BUY_AMOUNT_SOL"] = original_amount
                                        
                            else:
                                # Just alert for non-tradeable platforms
                                await send_telegram_alert(
                                    f"👀 New {name} Launch\n"
                                    f"Token: `{potential_mint[:16]}...`"
                                )
                                
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

# QUALITY THRESHOLDS FOR TRENDING
MIN_LP_USD = 5000      # $5k minimum liquidity
MIN_VOLUME_USD = 10000  # $10k minimum volume
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
    """Scan for QUALITY trending tokens only"""
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
            
            consecutive_failures = 0
            
            # Only check top 10 pairs for quality
            processed = 0
            quality_finds = 0
            
            for pair in pairs[:10]:
                mint = pair.get("baseToken", {}).get("address")
                lp_usd = float(pair.get("liquidity", {}).get("usd", 0))
                vol_usd = float(pair.get("volume", {}).get("h24", 0) or pair.get("volume", {}).get("h1", 0))
                
                # Get price change if available
                price_change_h1 = float(pair.get("priceChange", {}).get("h1", 0) if isinstance(pair.get("priceChange"), dict) else 0)
                price_change_h24 = float(pair.get("priceChange", {}).get("h24", 0) if isinstance(pair.get("priceChange"), dict) else 0)
                
                if not mint or mint in seen_trending or mint in BLACKLIST or mint in BROKEN_TOKENS:
                    continue
                
                # QUALITY FILTERS
                if lp_usd < MIN_LP_USD:
                    logging.debug(f"[SKIP] {mint[:8]}... - Low LP: ${lp_usd:.0f} (min: ${MIN_LP_USD})")
                    continue
                    
                if vol_usd < MIN_VOLUME_USD:
                    logging.debug(f"[SKIP] {mint[:8]}... - Low volume: ${vol_usd:.0f} (min: ${MIN_VOLUME_USD})")
                    continue
                
                # Skip if dumping hard
                if price_change_h1 < -30:
                    logging.debug(f"[SKIP] {mint[:8]}... - Dumping: {price_change_h1:.1f}% in 1h")
                    continue
                    
                seen_trending.add(mint)
                processed += 1
                increment_stat("tokens_scanned", 1)
                update_last_activity()
                
                # Check if it's worth buying
                is_mooning = price_change_h1 > 50 or price_change_h24 > 100
                has_momentum = price_change_h1 > 20 and vol_usd > 50000
                
                if is_mooning or has_momentum:
                    quality_finds += 1
                    
                    await send_telegram_alert(
                        f"🔥 QUALITY TRENDING TOKEN 🔥\n\n"
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
                        success = await buy_token(mint)
                        if success:
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
        min_lp = float(os.getenv("RUG_LP_THRESHOLD", 2.0))
        
        if not data or data.get("liquidity", 0) < min_lp:
            logging.info(f"[RUG CHECK] {mint[:8]}... has {data.get('liquidity', 0):.2f} SOL (min: {min_lp})")
            return False
        return True
    except Exception as e:
        logging.error(f"Rug check error for {mint}: {e}")
        return False

async def start_sniper():
    """Start the ELITE sniper bot"""
    await send_telegram_alert(
        "🚀 SNIPER LAUNCHING 🚀\n\n"
        "Mode: Smart Balance\n"
        "Safe LP: 2+ SOL = 0.03 SOL buy\n"
        "Medium LP: 0.5-2 SOL = 0.01 SOL buy\n"
        "Low LP: <0.5 SOL = 0.005 SOL buy\n"
        "Targets: 2x/5x/10x\n\n"
        "Ready to catch launches! 🎯"
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
    
    await send_telegram_alert("🎯 ALL SYSTEMS ACTIVE - Smart Balance Mode!")

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

        logging.info(f"[FORCEBUY] Attempting forced buy for {mint} with {BUY_AMOUNT_SOL} SOL")

        result = await buy_token(mint)
        if result:
            await send_telegram_alert(f"✅ Force buy successful for {mint}")
            await wait_and_auto_sell(mint)
        else:
            await send_telegram_alert(f"❌ Force buy failed for {mint}")
            
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
