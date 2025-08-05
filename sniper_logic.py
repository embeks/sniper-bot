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
    """WebSocket listener for new token mints."""
    url = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API}"
    retry_attempts = 0
    max_retries = 3
    retry_delay = 5
    heartbeat_interval = 60
    max_inactive = 300

    async def heartbeat_watchdog():
        while True:
            await asyncio.sleep(heartbeat_interval)
            now = time.time()
            elapsed = now - last_seen_token[name]
            if elapsed < heartbeat_interval * 2:
                logging.info(f"✅ {name} listener heartbeat ({int(elapsed)}s since last event)")
            else:
                logging.warning(f"⚠️ {name} listener no token for {int(elapsed)}s")
            if elapsed > max_inactive:
                if now - last_alert_sent[name] > alert_cooldown_sec:
                    msg = f"⚠️ {name} listener inactive for 5m — restarting..."
                    logging.error(msg)
                    listener_status[name] = "RESTARTING"
                    await send_telegram_alert(msg)
                    last_alert_sent[name] = now
                else:
                    logging.warning(f"[{name} RESTART] Inactive >5min, suppressing repeat alert")
                raise Exception("ListenerInactive")

    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
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
                
                logging.info(f"[🔁] {name} listener subscribed to Raydium logs.")
                await send_telegram_alert(f"📱 {name} listener live - monitoring Raydium pools.")
                listener_status[name] = "ACTIVE"
                last_seen_token[name] = time.time()

                watchdog_task = asyncio.create_task(heartbeat_watchdog())

                while True:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=60)
                        data = json.loads(msg)
                        
                        if "params" in data:
                            logs = data.get("params", {}).get("result", {}).get("value", {}).get("logs", [])
                            
                            # Look for pool initialization
                            for log in logs:
                                if "initialize2" in log or "InitializeInstruction2" in log:
                                    last_seen_token[name] = time.time()
                                    
                                    # Extract account keys
                                    account_keys = data["params"]["result"]["value"].get("accountKeys", [])
                                    if len(account_keys) > 10:
                                        # Get potential token mints from the transaction
                                        # Typically positions 8 and 9 are the token mints
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
                        logging.debug(f"[⏳] {name} heartbeat — no new pools in last 60s.")

        except Exception as e:
            logging.warning(f"[{name} ERROR] {e}")
            retry_attempts += 1
            listener_status[name] = f"RETRYING ({retry_attempts})"
            
            try:
                watchdog_task.cancel()
                await watchdog_task
            except Exception:
                pass
                
            if retry_attempts >= max_retries:
                msg = f"⚠️ {name} listener failed {max_retries}×. Last error: {e}"
                await send_telegram_alert(msg)
                listener_status[name] = "FAILED"
                break
                
            await asyncio.sleep(retry_delay)
        else:
            retry_attempts = 0
        finally:
            try:
                watchdog_task.cancel()
                await watchdog_task
            except Exception:
                pass

import httpx

MIN_LP_USD = 1000
MIN_VOLUME_USD = 1000
seen_trending = set()

async def get_trending_pairs_dexscreener():
    """Fetch trending pairs from DexScreener."""
    url = "https://api.dexscreener.com/latest/dex/pairs/solana"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("pairs", [])
    except Exception as e:
        logging.error(f"DexScreener API error: {e}")
    return None

async def get_trending_pairs_birdeye():
    """Fetch trending pairs from Birdeye."""
    url = "https://public-api.birdeye.so/public/tokenlist?chain=solana"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                pairs = []
                for tok in data.get("data", [])[:20]:
                    mint = tok.get("address")
                    lp_usd = float(tok.get("liquidity_usd", 0))
                    vol_usd = float(tok.get("volume_24h_usd", 0))
                    pair = {
                        "baseToken": {"address": mint},
                        "liquidity": {"usd": lp_usd},
                        "volume": {"h1": vol_usd},
                    }
                    pairs.append(pair)
                return pairs
    except Exception as e:
        logging.error(f"Birdeye API error: {e}")
    return None

async def trending_scanner():
    """Scan for trending tokens to snipe."""
    global seen_trending
    
    while True:
        try:
            if not is_bot_running():
                await asyncio.sleep(5)
                continue

            # Try DexScreener first, fall back to Birdeye
            pairs = await get_trending_pairs_dexscreener()
            source = "DEXScreener"
            if not pairs:
                pairs = await get_trending_pairs_birdeye()
                source = "Birdeye"
                
            if not pairs:
                logging.warning("[Trending Scanner] Both APIs unavailable")
                await asyncio.sleep(TREND_SCAN_INTERVAL)
                continue

            # Process top trending tokens
            for pair in pairs[:10]:
                mint = pair.get("baseToken", {}).get("address")
                lp_usd = float(pair.get("liquidity", {}).get("usd", 0))
                vol_usd = float(pair.get("volume", {}).get("h1", 0))
                
                if not mint or mint in seen_trending or mint in BLACKLIST or mint in BROKEN_TOKENS:
                    continue
                    
                if lp_usd < MIN_LP_USD or vol_usd < MIN_VOLUME_USD:
                    logging.info(f"[SKIP] {mint} - LP: ${lp_usd}, Vol: ${vol_usd}")
                    continue
                    
                seen_trending.add(mint)
                increment_stat("tokens_scanned", 1)
                update_last_activity()
                
                await send_telegram_alert(
                    f"[🔥] ({source}) Trending token: {mint}\n"
                    f"LP: ${lp_usd:,.0f} | Vol: ${vol_usd:,.0f}"
                )
                
                if await rug_filter_passes(mint):
                    if await buy_token(mint):
                        await wait_and_auto_sell(mint)
                        
            await asyncio.sleep(TREND_SCAN_INTERVAL)
            
        except Exception as e:
            logging.warning(f"[Trending Scanner ERROR] {e}")
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
