import asyncio
import json
import os
import websockets
import logging
import time
from datetime import datetime, timedelta
from dotenv import load_dotenv

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
RUG_LP_THRESHOLD = float(os.getenv("RUG_LP_THRESHOLD", 0.75))
TREND_SCAN_INTERVAL = int(os.getenv("TREND_SCAN_INTERVAL", 30))
RPC_URL = os.getenv("RPC_URL")
SLIPPAGE_BPS = 100

seen_tokens = set()
BLACKLIST = set()
TASKS = []

last_alert_sent = {"Raydium": 0}
alert_cooldown_sec = 1800

async def mempool_listener(name):
    """WebSocket listener for new token mints - FIXED VERSION."""
    url = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API}"
    retry_attempts = 0
    max_retries = 10  # Increased retries
    retry_delay = 10  # Increased delay
    heartbeat_interval = 30  # More frequent heartbeat
    max_inactive = 300
    
    while retry_attempts < max_retries:
        ws = None
        watchdog_task = None
        
        try:
            # Create WebSocket with better timeout settings
            ws = await websockets.connect(
                url, 
                ping_interval=20,
                ping_timeout=10,
                close_timeout=10,
                max_size=10**7  # Increased max message size
            )
            
            # Subscribe to Raydium logs
            await ws.send(json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "logsSubscribe",
                "params": [
                    {
                        "mentions": ["675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"]  # Raydium V4
                    },
                    {"commitment": "processed"}
                ]
            }))
            
            # Wait for subscription confirmation
            response = await asyncio.wait_for(ws.recv(), timeout=10)
            response_data = json.loads(response)
            
            if "result" not in response_data:
                logging.error(f"[{name}] Failed to subscribe: {response_data}")
                raise Exception("Subscription failed")
            
            subscription_id = response_data["result"]
            logging.info(f"[🔁] {name} listener subscribed with ID: {subscription_id}")
            await send_telegram_alert(f"📱 {name} listener live - monitoring Raydium pools.")
            listener_status[name] = "ACTIVE"
            last_seen_token[name] = time.time()
            retry_attempts = 0  # Reset on successful connection
            
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
                    
                    # Update heartbeat on any message
                    last_seen_token[name] = time.time()
                    
                    if "params" in data:
                        logs = data.get("params", {}).get("result", {}).get("value", {}).get("logs", [])
                        
                        # Look for pool initialization
                        for log in logs:
                            if "initialize2" in log or "InitializeInstruction2" in log:
                                # Extract account keys
                                account_keys = data["params"]["result"]["value"].get("accountKeys", [])
                                if len(account_keys) > 10:
                                    # Get potential token mints
                                    for i in [8, 9]:
                                        if i < len(account_keys):
                                            potential_mint = account_keys[i]
                                            
                                            # Skip SOL and already seen tokens
                                            if (potential_mint == "So11111111111111111111111111111111111111112" or 
                                                potential_mint in seen_tokens or 
                                                not is_bot_running()):
                                                continue
                                            
                                            seen_tokens.add(potential_mint)
                                            logging.info(f"[🧠] New Raydium pool token: {potential_mint}")
                                            increment_stat("tokens_scanned", 1)
                                            update_last_activity()
                                            
                                            await send_telegram_alert(f"[🟡] New token in Raydium pool: {potential_mint}")
                                            
                                            # Check token validity and buy
                                            if potential_mint not in BROKEN_TOKENS and potential_mint not in BLACKLIST:
                                                if await rug_filter_passes(potential_mint):
                                                    if await buy_token(potential_mint):
                                                        await wait_and_auto_sell(potential_mint)
                                            else:
                                                log_skipped_token(potential_mint, "Blacklisted or broken")
                                                record_skip("blacklist")
                                                
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
            # Clean up
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
            
            # Exponential backoff
            wait_time = min(retry_delay * (2 ** (retry_attempts - 1)), 300)
            logging.info(f"[{name}] Retrying in {wait_time}s (attempt {retry_attempts}/{max_retries})")
            await asyncio.sleep(wait_time)

import httpx

MIN_LP_USD = 1000
MIN_VOLUME_USD = 1000
seen_trending = set()

async def get_trending_pairs_dexscreener():
    """Fetch trending pairs from DexScreener - FIXED."""
    url = "https://api.dexscreener.com/latest/dex/pairs/solana"
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json"
            }
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("pairs", [])
            else:
                logging.debug(f"DexScreener returned status {resp.status_code}")
    except Exception as e:
        logging.debug(f"DexScreener API error: {e}")
    return None

async def get_trending_pairs_birdeye():
    """Fetch trending pairs from Birdeye - FIXED."""
    # Skip Birdeye if no API key
    BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")
    if not BIRDEYE_API_KEY:
        return None
        
    url = "https://public-api.birdeye.so/defi/tokenlist"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
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
                return pairs
            else:
                logging.debug(f"Birdeye returned status {resp.status_code}")
    except Exception as e:
        logging.debug(f"Birdeye API error: {e}")
    return None

async def trending_scanner():
    """Scan for trending tokens to snipe - FIXED."""
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
            
            # Fall back to Birdeye if DexScreener fails
            if not pairs:
                pairs = await get_trending_pairs_birdeye()
                source = "Birdeye"
            
            if not pairs:
                consecutive_failures += 1
                if consecutive_failures >= max_consecutive_failures:
                    logging.warning(f"[Trending Scanner] Both APIs unavailable ({consecutive_failures} failures)")
                    consecutive_failures = 0  # Reset counter
                await asyncio.sleep(TREND_SCAN_INTERVAL * 2)  # Wait longer on failure
                continue
            
            # Reset failure counter on success
            consecutive_failures = 0
            
            # Process top trending tokens
            processed = 0
            for pair in pairs[:10]:
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
                    f"[🔥] ({source}) Trending token: {mint}\n"
                    f"LP: ${lp_usd:,.0f} | Vol: ${vol_usd:,.0f}"
                )
                
                if await rug_filter_passes(mint):
                    if await buy_token(mint):
                        await wait_and_auto_sell(mint)
            
            if processed > 0:
                logging.info(f"[Trending Scanner] Processed {processed} tokens from {source}")
                        
            await asyncio.sleep(TREND_SCAN_INTERVAL)
            
        except Exception as e:
            logging.error(f"[Trending Scanner ERROR] {e}")
            await asyncio.sleep(TREND_SCAN_INTERVAL)

async def rug_filter_passes(mint: str) -> bool:
    """Check if token passes basic rug filters."""
    try:
        data = await get_liquidity_and_ownership(mint)
        if not data or not data.get("liquidity") or data.get("liquidity", 0) < 10000:
            await send_telegram_alert(f"⛔ {mint} rug filter failed (low or no LP)")
            log_skipped_token(mint, "Rug/Low LP")
            record_skip("malformed")
            return False
        return True
    except Exception as e:
        await send_telegram_alert(f"⛔ Rug check error for {mint}: {e}")
        return False

async def start_sniper():
    """Start the sniper bot with all listeners."""
    await send_telegram_alert("✅ Sniper bot launching (Raydium-only mode)...")

    # Test mint if configured
    if FORCE_TEST_MINT:
        await send_telegram_alert(f"🚨 Forced Test Buy: {FORCE_TEST_MINT}")
        if await buy_token(FORCE_TEST_MINT):
            await wait_and_auto_sell(FORCE_TEST_MINT)

    # Start all tasks
    TASKS.append(asyncio.create_task(daily_stats_reset_loop()))
    TASKS.extend([
        asyncio.create_task(mempool_listener("Raydium")),
        asyncio.create_task(trending_scanner())
    ])

async def start_sniper_with_forced_token(mint: str):
    """Force buy a specific token (for testing)."""
    try:
        await send_telegram_alert(f"🚨 Force buy initiated for: {mint}")
        
        if not is_bot_running():
            await send_telegram_alert(f"⛔ Bot is paused. Cannot force buy {mint}")
            return

        if mint in BROKEN_TOKENS or mint in BLACKLIST:
            await send_telegram_alert(f"❌ {mint} is blacklisted or marked as broken")
            log_skipped_token(mint, "Blacklisted or broken token")
            return

        # Skip LP check for force buy
        await send_telegram_alert(f"🚨 Executing force buy for {mint}")
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
        await send_telegram_alert(f"❌ Force buy error for {mint}: {e}")
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
