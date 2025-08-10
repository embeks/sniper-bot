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
RUG_LP_THRESHOLD = float(os.getenv("RUG_LP_THRESHOLD", 0.01))
TREND_SCAN_INTERVAL = int(os.getenv("TREND_SCAN_INTERVAL", 30))
RPC_URL = os.getenv("RPC_URL")
SLIPPAGE_BPS = 100
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")

seen_tokens = set()
BLACKLIST = set()
TASKS = []

last_alert_sent = {"Raydium": 0, "Jupiter": 0, "PumpFun": 0, "Moonshot": 0}
alert_cooldown_sec = 1800

# SYSTEM PROGRAMS TO IGNORE - MINIMAL LIST
SYSTEM_PROGRAMS = [
    "11111111111111111111111111111111",
    "ComputeBudget111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
]

async def mempool_listener(name, program_id=None):
    """ULTRA AGGRESSIVE TOKEN HUNTER WITH DEBUG MODE"""
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
            logging.info(f"[🔁] {name} listener subscribed with ID: {subscription_id}")
            await send_telegram_alert(f"📱 {name} listener DEBUG MODE ACTIVE! 🔍")
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
            debug_counter = 0
            
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
                        
                        # EXTREME DEBUG - Show what we're actually getting
                        debug_counter += 1
                        if debug_counter % 50 == 0:  # Every 50th transaction
                            if account_keys and len(account_keys) > 0:
                                logging.info(f"[DEBUG {name}] Transaction sample:")
                                logging.info(f"  Signature: {signature[:16]}...")
                                logging.info(f"  Total accounts: {len(account_keys)}")
                                logging.info(f"  First 5 accounts:")
                                for i, acc in enumerate(account_keys[:5]):
                                    logging.info(f"    [{i}]: {acc}")
                                if logs:
                                    logging.info(f"  First log: {logs[0][:100] if logs[0] else 'Empty'}")
                        
                        # Skip if we've seen this transaction
                        if signature in processed_txs:
                            continue
                        processed_txs.add(signature)
                        
                        # Keep set size manageable
                        if len(processed_txs) > 1000:
                            processed_txs.clear()
                        
                        # Look for new tokens in ALL positions
                        found_new_token = False
                        
                        # Check EVERY account
                        for i, key in enumerate(account_keys):
                            # Skip obvious non-tokens
                            if key == "So11111111111111111111111111111111111111112":
                                continue
                            if any(sys_prog in key for sys_prog in SYSTEM_PROGRAMS):
                                continue
                            if len(key) != 44:
                                continue
                            if key in seen_tokens:
                                continue
                            
                            # Validate it's a proper address
                            try:
                                Pubkey.from_string(key)
                                
                                # Check if this could be a new token based on logs
                                is_potential_token = False
                                
                                # For Raydium - specific positions
                                if name == "Raydium" and i in [8, 9, 10, 11]:
                                    is_potential_token = True
                                    
                                # For any platform - check logs
                                for log in logs:
                                    log_lower = log.lower()
                                    if any(word in log_lower for word in [
                                        "initialize", "mint", "create", "pool", 
                                        "initializeaccount", "initializemint", "mintto",
                                        "initialize2", "swap"
                                    ]):
                                        is_potential_token = True
                                        break
                                
                                # If potential token, process it
                                if is_potential_token and is_bot_running():
                                    seen_tokens.add(key)
                                    found_new_token = True
                                    
                                    logging.info(f"")
                                    logging.info(f"[💎💎💎] POTENTIAL NEW TOKEN DETECTED!")
                                    logging.info(f"  Platform: {name}")
                                    logging.info(f"  Mint: {key}")
                                    logging.info(f"  Position: {i} of {len(account_keys)}")
                                    logging.info(f"  Signature: {signature[:16]}...")
                                    logging.info(f"")
                                    
                                    increment_stat("tokens_scanned", 1)
                                    update_last_activity()
                                    
                                    # Send alert
                                    alert_msg = f"🚨 NEW TOKEN FOUND 🚨\n\n"
                                    alert_msg += f"Platform: {name}\n"
                                    alert_msg += f"Mint: `{key}`\n"
                                    alert_msg += f"Position: {i}/{len(account_keys)}\n"
                                    alert_msg += f"Sig: {signature[:16]}..."
                                    
                                    await send_telegram_alert(alert_msg)
                                    
                                    # Try to buy if tradeable
                                    if name in ["Raydium", "Jupiter"]:
                                        if key not in BROKEN_TOKENS and key not in BLACKLIST:
                                            logging.info(f"[🎯] ATTEMPTING SNIPE: {key}")
                                            await send_telegram_alert(f"🎯 SNIPING: {key[:16]}...")
                                            
                                            if await buy_token(key):
                                                await send_telegram_alert(f"✅ SNIPED! Now monitoring...")
                                                await wait_and_auto_sell(key)
                                            else:
                                                await send_telegram_alert(f"❌ Snipe failed: {key[:16]}")
                                    else:
                                        await send_telegram_alert(f"👀 Found on {name}: {key[:16]}")
                                        
                            except Exception as e:
                                continue
                        
                        # Log if we found something
                        if found_new_token:
                            logging.info(f"[{name}] Processed transaction with new token(s)")
                                
                except asyncio.TimeoutError:
                    logging.debug(f"[⏳] {name} no new events in 60s (normal)")
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

MIN_LP_USD = 100
MIN_VOLUME_USD = 100
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
    """Scan for trending tokens"""
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
                    logging.warning(f"[Trending Scanner] Both APIs unavailable ({consecutive_failures} failures)")
                    consecutive_failures = 0
                await asyncio.sleep(TREND_SCAN_INTERVAL * 2)
                continue
            
            consecutive_failures = 0
            
            processed = 0
            for pair in pairs[:20]:
                mint = pair.get("baseToken", {}).get("address")
                lp_usd = float(pair.get("liquidity", {}).get("usd", 0))
                vol_usd = float(pair.get("volume", {}).get("h24", 0) or pair.get("volume", {}).get("h1", 0))
                
                if not mint or mint in seen_trending or mint in BLACKLIST or mint in BROKEN_TOKENS:
                    continue
                    
                if lp_usd < MIN_LP_USD or vol_usd < MIN_VOLUME_USD:
                    logging.debug(f"[SKIP] {mint[:8]}... - LP: ${lp_usd:.0f}, Vol: ${vol_usd:.0f}")
                    continue
                    
                seen_trending.add(mint)
                processed += 1
                increment_stat("tokens_scanned", 1)
                update_last_activity()
                
                await send_telegram_alert(
                    f"[🔥 TRENDING] {mint}\n"
                    f"LP: ${lp_usd:,.0f} | Vol: ${vol_usd:,.0f}\n"
                    f"Source: {source}"
                )
                
                if await buy_token(mint):
                    await wait_and_auto_sell(mint)
            
            if processed > 0:
                logging.info(f"[Trending Scanner] Processed {processed} tokens from {source}")
                        
            await asyncio.sleep(TREND_SCAN_INTERVAL)
            
        except Exception as e:
            logging.error(f"[Trending Scanner ERROR] {e}")
            await asyncio.sleep(TREND_SCAN_INTERVAL)

async def rug_filter_passes(mint: str) -> bool:
    """Ultra relaxed rug filter"""
    try:
        data = await get_liquidity_and_ownership(mint)
        if not data or data.get("liquidity", 0) < 100:
            logging.info(f"[RUG CHECK] {mint} has very low LP but proceeding anyway")
        return True
    except Exception as e:
        logging.error(f"Rug check error for {mint}: {e}")
        return True

async def start_sniper():
    """Start the sniper bot"""
    await send_telegram_alert(
        "🚀 SNIPER LAUNCHING - DEBUG MODE! 🚀\n"
        "Mode: ULTRA AGGRESSIVE\n"
        "Debug: ENABLED\n"
        "Checking all account positions...\n"
        "LET'S FIND THOSE TOKENS! 🔍"
    )

    if FORCE_TEST_MINT:
        await send_telegram_alert(f"🚨 Forced Test Buy: {FORCE_TEST_MINT}")
        if await buy_token(FORCE_TEST_MINT):
            await wait_and_auto_sell(FORCE_TEST_MINT)

    TASKS.append(asyncio.create_task(daily_stats_reset_loop()))
    TASKS.extend([
        asyncio.create_task(mempool_listener("Raydium")),
        asyncio.create_task(mempool_listener("Jupiter")),
        asyncio.create_task(mempool_listener("PumpFun")),
        asyncio.create_task(mempool_listener("Moonshot")),
        asyncio.create_task(trending_scanner())
    ])
    
    await send_telegram_alert("🎯 ALL SYSTEMS ACTIVE - DEBUG MODE ON!")

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
