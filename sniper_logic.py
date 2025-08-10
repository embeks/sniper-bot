import asyncio
import json
import os
import websockets
import logging
import time
from datetime import datetime, timedelta
from dotenv import load_dotenv
import httpx

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
RUG_LP_THRESHOLD = float(os.getenv("RUG_LP_THRESHOLD", 0.1))  # LOWERED FOR MORE CATCHES
TREND_SCAN_INTERVAL = int(os.getenv("TREND_SCAN_INTERVAL", 30))
RPC_URL = os.getenv("RPC_URL")
SLIPPAGE_BPS = 100
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")

seen_tokens = set()
BLACKLIST = set()
TASKS = []

last_alert_sent = {"Raydium": 0, "Jupiter": 0, "PumpFun": 0, "Moonshot": 0}
alert_cooldown_sec = 1800

async def mempool_listener(name, program_id=None):
    """WebSocket listener - AGGRESSIVE VERSION FOR MAXIMUM CATCHES."""
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
            program_id = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"  # Raydium V4
        elif name == "Jupiter":
            program_id = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"  # Jupiter V6
        elif name == "PumpFun":
            program_id = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"  # Pump.fun
        elif name == "Moonshot":
            program_id = "MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG"  # Moonshot
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
            await send_telegram_alert(f"📱 {name} listener LIVE - HUNTING FOR GAINS! 💰")
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
            
            # Main message loop
            while True:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=60)
                    data = json.loads(msg)
                    
                    # Update heartbeat
                    last_seen_token[name] = time.time()
                    
                    # AGGRESSIVE DETECTION - LOG EVERYTHING
                    if "params" in data:
                        logs = data.get("params", {}).get("result", {}).get("value", {}).get("logs", [])
                        account_keys = data["params"]["result"]["value"].get("accountKeys", [])
                        
                        # DEBUG: Log activity to see what's happening
                        if logs and len(logs) > 0:
                            logging.info(f"[{name}] ACTIVITY DETECTED! Logs: {logs[0][:200]}")
                            
                        # AGGRESSIVE TOKEN DETECTION - CHECK ALL ACCOUNTS
                        for key in account_keys:
                            if (key != "So11111111111111111111111111111111111111112" and 
                                is_valid_mint(key) and 
                                key not in seen_tokens and
                                is_bot_running()):
                                
                                # NEW TOKEN FOUND!
                                seen_tokens.add(key)
                                logging.info(f"[💎] NEW TOKEN DETECTED on {name}: {key}")
                                increment_stat("tokens_scanned", 1)
                                update_last_activity()
                                
                                # Alert based on platform
                                if name == "Raydium":
                                    await send_telegram_alert(f"[🟡 RAYDIUM] New pool token: {key}")
                                elif name == "Jupiter":
                                    await send_telegram_alert(f"[🔵 JUPITER] New swap token: {key}")
                                elif name == "PumpFun":
                                    await send_telegram_alert(f"[🟢 PUMP.FUN] New launch: {key}")
                                elif name == "Moonshot":
                                    await send_telegram_alert(f"[🌙 MOONSHOT] New launch: {key}")
                                else:
                                    await send_telegram_alert(f"[🆕] New token on {name}: {key}")
                                
                                # Try to buy if it's Raydium/Jupiter (tradeable)
                                if name in ["Raydium", "Jupiter"]:
                                    if key not in BROKEN_TOKENS and key not in BLACKLIST:
                                        # AGGRESSIVE MODE - Skip rug filter for speed
                                        logging.info(f"[🎯] ATTEMPTING SNIPE: {key}")
                                        if await buy_token(key):
                                            await wait_and_auto_sell(key)
                                    else:
                                        log_skipped_token(key, "Blacklisted or broken")
                                        record_skip("blacklist")
                                else:
                                    # Pump.fun/Moonshot - just alert for now
                                    logging.info(f"[{name}] Token detected but needs special handling: {key}")
                        
                        # Also check for specific keywords (less strict)
                        for log in logs:
                            log_lower = log.lower()
                            if any(keyword in log_lower for keyword in ["initialize", "create", "mint", "pool", "swap", "trade", "buy", "sell"]):
                                logging.debug(f"[{name}] Keyword match in log: {log[:100]}")
                                
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

MIN_LP_USD = 500  # LOWERED FOR MORE OPPORTUNITIES
MIN_VOLUME_USD = 500  # LOWERED FOR MORE OPPORTUNITIES
seen_trending = set()

async def get_trending_pairs_dexscreener():
    """Fetch trending pairs from DexScreener - FIXED WITH RETRIES."""
    url = "https://api.dexscreener.com/latest/dex/pairs/solana"
    
    for attempt in range(3):  # Try 3 times
        try:
            async with httpx.AsyncClient(
                timeout=30, 
                follow_redirects=True,
                verify=False  # Skip SSL verification
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
    """Fetch trending pairs from Birdeye - FIXED WITH RETRIES."""
    if not BIRDEYE_API_KEY:
        return None
        
    url = "https://public-api.birdeye.so/defi/tokenlist"
    
    for attempt in range(3):  # Try 3 times
        try:
            async with httpx.AsyncClient(
                timeout=30,
                verify=False  # Skip SSL verification
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
    """Scan for trending tokens - AGGRESSIVE MODE."""
    global seen_trending
    consecutive_failures = 0
    max_consecutive_failures = 5
    
    while True:
        try:
            if not is_bot_running():
                await asyncio.sleep(5)
                continue

            # Try DexScreener first
            pairs = await get_trending_pairs_dexscreener()
            source = "DEXScreener"
            
            # Fall back to Birdeye
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
            
            # Reset failure counter
            consecutive_failures = 0
            
            # Process MORE tokens (top 20 instead of 10)
            processed = 0
            for pair in pairs[:20]:  # INCREASED FROM 10
                mint = pair.get("baseToken", {}).get("address")
                lp_usd = float(pair.get("liquidity", {}).get("usd", 0))
                vol_usd = float(pair.get("volume", {}).get("h24", 0) or pair.get("volume", {}).get("h1", 0))
                
                if not mint or mint in seen_trending or mint in BLACKLIST or mint in BROKEN_TOKENS:
                    continue
                    
                # LOWERED THRESHOLDS FOR MORE OPPORTUNITIES
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
                
                # AGGRESSIVE MODE - Try to buy trending tokens
                if await buy_token(mint):
                    await wait_and_auto_sell(mint)
            
            if processed > 0:
                logging.info(f"[Trending Scanner] Processed {processed} tokens from {source}")
                        
            await asyncio.sleep(TREND_SCAN_INTERVAL)
            
        except Exception as e:
            logging.error(f"[Trending Scanner ERROR] {e}")
            await asyncio.sleep(TREND_SCAN_INTERVAL)

async def rug_filter_passes(mint: str) -> bool:
    """RELAXED rug filter for more opportunities."""
    try:
        data = await get_liquidity_and_ownership(mint)
        # SUPER RELAXED - Only skip if NO liquidity at all
        if not data or data.get("liquidity", 0) < 100:  # Lowered from 10000
            logging.info(f"[RUG CHECK] {mint} has very low LP but proceeding anyway")
            # Don't return False - let it through!
        return True  # Let everything through for max opportunities
    except Exception as e:
        logging.error(f"Rug check error for {mint}: {e}")
        return True  # On error, let it through!

async def start_sniper():
    """Start the ELITE sniper bot - MAXIMUM AGGRESSION MODE."""
    await send_telegram_alert(
        "🚀 ELITE SNIPER LAUNCHING! 🚀\n"
        "Mode: MAXIMUM AGGRESSION\n"
        "Targets: ALL PLATFORMS\n"
        "Filters: MINIMAL\n"
        "LET'S MAKE MONEY! 💰"
    )

    # Test mint if configured
    if FORCE_TEST_MINT:
        await send_telegram_alert(f"🚨 Forced Test Buy: {FORCE_TEST_MINT}")
        if await buy_token(FORCE_TEST_MINT):
            await wait_and_auto_sell(FORCE_TEST_MINT)

    # Start ALL listeners
    TASKS.append(asyncio.create_task(daily_stats_reset_loop()))
    TASKS.extend([
        asyncio.create_task(mempool_listener("Raydium")),
        asyncio.create_task(mempool_listener("Jupiter")),
        asyncio.create_task(mempool_listener("PumpFun")),
        asyncio.create_task(mempool_listener("Moonshot")),
        asyncio.create_task(trending_scanner())
    ])
    
    await send_telegram_alert("🎯 ALL SYSTEMS ACTIVE - HUNTING FOR 100X GAINS!")

async def start_sniper_with_forced_token(mint: str):
    """Force buy a specific token."""
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
    """Stop all running tasks."""
    for task in TASKS:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    TASKS.clear()
    await send_telegram_alert("🛑 All sniper tasks stopped.")
