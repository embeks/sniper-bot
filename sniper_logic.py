# === sniper_logic.py (UPGRADED with listener health, heartbeat, and retries) ===

import asyncio
import json
import os
import websockets
import logging
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
from jupiter_aggregator import JupiterAggregatorClient

load_dotenv()

FORCE_TEST_MINT = os.getenv("FORCE_TEST_MINT")
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
HELIUS_API = os.getenv("HELIUS_API")
RUG_LP_THRESHOLD = float(os.getenv("RUG_LP_THRESHOLD", 0.75))
TREND_SCAN_INTERVAL = int(os.getenv("TREND_SCAN_INTERVAL", 30))
RPC_URL = os.getenv("RPC_URL")
SLIPPAGE_BPS = 100
seen_tokens = set()

TASKS = []
aggregator = JupiterAggregatorClient(RPC_URL)

# === Enhanced Mempool Listener with Reconnects + Heartbeats ===
async def mempool_listener(name):
    url = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API}"
    retry_attempts = 0

    while retry_attempts < 3:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                await ws.send(json.dumps({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "logsSubscribe",
                    "params": [
                        {"mentions": [TOKEN_PROGRAM_ID]},
                        {"commitment": "processed"}
                    ]
                }))
                logging.info(f"[🔁] {name} listener subscribed.")
                await send_telegram_alert(f"📱 {name} listener live.")
                listener_status[name] = "ACTIVE"
                last_seen_token[name] = datetime.utcnow()

                while True:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=60)
                        data = json.loads(msg)
                        logs = data.get("params", {}).get("result", {}).get("value", {}).get("logs", [])

                        for log in logs:
                            if "MintTo" in log or "InitializeMint" in log:
                                keys = data["params"]["result"]["value"].get("accountKeys", [])
                                for key in keys:
                                    if key in seen_tokens or not is_bot_running():
                                        continue
                                    seen_tokens.add(key)
                                    print(f"[🧠] Found token: {key}")
                                    increment_stat("tokens_scanned", 1)
                                    update_last_activity()
                                    last_seen_token[name] = datetime.utcnow()

                                    if is_valid_mint([{ 'pubkey': key }]):
                                        if key in BROKEN_TOKENS:
                                            await send_telegram_alert(f"❌ Skipped {key} — Jupiter sent broken transaction")
                                            log_skipped_token(key, "Broken token")
                                            record_skip("malformed")
                                            continue
                                        await send_telegram_alert(f"[🟡] Valid token: {key}")
                                        if await rug_filter_passes(key):
                                            if await buy_token(key):
                                                await wait_and_auto_sell(key)
                                    else:
                                        log_skipped_token(key, "Invalid mint")

                    except asyncio.TimeoutError:
                        logging.info(f"[⏳] {name} heartbeat — no token seen in last 60s.")

        except Exception as e:
            logging.warning(f"[{name} ERROR] {e}")
            retry_attempts += 1
            listener_status[name] = f"RETRYING ({retry_attempts})"
            await asyncio.sleep(5)

    # Final failure
    await send_telegram_alert(f"⚠️ {name} listener failed 3×. Reconnect failed. Last error: {e}")
    listener_status[name] = "FAILED"


async def trending_scanner():
    while True:
        try:
            if not is_bot_running():
                await asyncio.sleep(5)
                continue

            mints = await get_trending_mints()
            for mint in mints:
                if mint in seen_tokens:
                    continue
                seen_tokens.add(mint)
                increment_stat("tokens_scanned", 1)
                update_last_activity()
                if mint in BROKEN_TOKENS:
                    await send_telegram_alert(f"❌ Skipped {mint} — Jupiter sent broken transaction")
                    log_skipped_token(mint, "Broken token")
                    record_skip("malformed")
                    continue
                await send_telegram_alert(f"[🔥] Trending token: {mint}")
                if await rug_filter_passes(mint):
                    if await buy_token(mint):
                        await wait_and_auto_sell(mint)

            await asyncio.sleep(TREND_SCAN_INTERVAL)
        except Exception as e:
            logging.warning(f"[Trending Scanner ERROR] {e}")
            await asyncio.sleep(TREND_SCAN_INTERVAL)


async def start_sniper():
    await send_telegram_alert("✅ Sniper bot launching...")

    if FORCE_TEST_MINT:
        await send_telegram_alert(f"🚨 Forced Test Buy (LP check skipped): {FORCE_TEST_MINT}")
        if await buy_token(FORCE_TEST_MINT):
            await wait_and_auto_sell(FORCE_TEST_MINT)

    TASKS.append(asyncio.create_task(daily_stats_reset_loop()))
    TASKS.extend([
        asyncio.create_task(mempool_listener("Raydium")),
        asyncio.create_task(mempool_listener("Jupiter")),
        asyncio.create_task(trending_scanner())
    ])


async def start_sniper_with_forced_token(mint: str):
    if not is_bot_running():
        await send_telegram_alert(f"⛔ Bot is paused. Cannot force buy {mint}")
        return

    if mint in BROKEN_TOKENS:
        await send_telegram_alert(f"❌ Skipped {mint} — Jupiter sent broken transaction")
        log_skipped_token(mint, "Broken token")
        return

    await send_telegram_alert(f"🚨 Force Buy (skipping LP check): {mint}")
    logging.info(f"[FORCEBUY] Attempting forced buy for {mint} with {BUY_AMOUNT_SOL} SOL")
    try:
        success = await buy_token(mint)
        if success:
            await wait_and_auto_sell(mint)
    except Exception as e:
        await send_telegram_alert(f"❌ Force buy error for {mint}: {e}")
        logging.exception(f"[FORCEBUY] Exception: {e}")


async def stop_all_tasks():
    for task in TASKS:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    TASKS.clear()
    await send_telegram_alert("🚩 All sniper tasks stopped.")
