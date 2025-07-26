# sniper_logic.py — ELITE VERSION (Force Buy skips LP check)

import asyncio
import json
import os
import websockets
import logging
from dotenv import load_dotenv

from utils import (
    is_valid_mint,
    buy_token,
    log_skipped_token,
    send_telegram_alert,
    get_trending_mints,
    wait_and_auto_sell,
    get_liquidity_and_ownership,
    is_bot_running,
    keypair,
    BUY_AMOUNT_SOL
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

async def rug_filter_passes(mint):
    try:
        data = await get_liquidity_and_ownership(mint)
        if not data:
            await send_telegram_alert(f"❌ No LP/ownership data for {mint}")
            log_skipped_token(mint, "No LP/ownership")
            return False

        lp = float(data.get("liquidity", 0))
        if lp < RUG_LP_THRESHOLD:
            await send_telegram_alert(f"⚠️ Skipping {mint} — LP too low: {lp}")
            log_skipped_token(mint, "Low LP")
            return False

        return True
    except Exception as e:
        await send_telegram_alert(f"⚠️ Rug check error for {mint}: {e}")
        return False

async def mempool_listener(name):
    url = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API}"
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [TOKEN_PROGRAM_ID]},
                {"commitment": "processed"}
            ]
        }))
        print(f"[🔁] {name} listener subscribed.")
        await send_telegram_alert(f"📱 {name} listener live.")

        while True:
            try:
                msg = await ws.recv()
                data = json.loads(msg)
                logs = data.get("params", {}).get("result", {}).get("value", {}).get("logs", [])

                for log in logs:
                    if "Instruction: MintTo" in log or "Instruction: InitializeMint" in log:
                        keys = data["params"]["result"]["value"].get("accountKeys", [])
                        for key in keys:
                            if key in seen_tokens or not is_bot_running():
                                continue
                            seen_tokens.add(key)
                            print(f"[🧠] Found token: {key}")

                            if is_valid_mint([{ 'pubkey': key }]):
                                await send_telegram_alert(f"[🟡] Valid token: {key}")
                                if await rug_filter_passes(key):
                                    if await buy_token(key):
                                        await wait_and_auto_sell(key)
                            else:
                                log_skipped_token(key, "Invalid mint")
            except Exception as e:
                print(f"[{name} ERROR] {e}")
                await asyncio.sleep(1)

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
                await send_telegram_alert(f"[🔥] Trending token: {mint}")

                if await rug_filter_passes(mint):
                    if await buy_token(mint):
                        await wait_and_auto_sell(mint)

            await asyncio.sleep(TREND_SCAN_INTERVAL)
        except Exception as e:
            print(f"[Scanner ERROR] {e}")
            await asyncio.sleep(TREND_SCAN_INTERVAL)

async def start_sniper():
    await send_telegram_alert("✅ Sniper bot launching...")

    if FORCE_TEST_MINT:
        await send_telegram_alert(f"🚨 Forced Test Buy (LP check skipped): {FORCE_TEST_MINT}")
        if await buy_token(FORCE_TEST_MINT):
            await wait_and_auto_sell(FORCE_TEST_MINT)

    TASKS.extend([
        asyncio.create_task(mempool_listener("Raydium")),
        asyncio.create_task(mempool_listener("Jupiter")),
        asyncio.create_task(trending_scanner())
    ])

async def start_sniper_with_forced_token(mint: str):
    if not is_bot_running():
        await send_telegram_alert(f"⛔ Bot is paused. Cannot force buy {mint}")
        return

    await send_telegram_alert(f"🚨 Force Buy (skipping LP check): {mint}")
    logging.info(f"[FORCEBUY] Getting quote for {mint} with {BUY_AMOUNT_SOL} SOL")

    try:
        route = await aggregator.get_quote(
            input_mint=Pubkey.from_string("So11111111111111111111111111111111111111112"),
            output_mint=Pubkey.from_string(mint),
            amount=int(BUY_AMOUNT_SOL * 1e9),
            slippage_bps=SLIPPAGE_BPS,
            user_pubkey=keypair.pubkey()
        )

        if not route:
            await send_telegram_alert(f"❌ Quote failed for {mint}")
            logging.error(f"[FORCEBUY] Quote failed: No route returned for {mint}")
            return

        await send_telegram_alert(f"✅ Quote received. Building swap for {mint}")
        logging.info(f"[FORCEBUY] Quote received: {route}")

        tx_base64 = await aggregator.get_swap_transaction(route, keypair)
        if not tx_base64 or not isinstance(tx_base64, str):
            await send_telegram_alert(f"❌ Jupiter quote returned no swapTransaction for {mint}")
            logging.error(f"[FORCEBUY] No swapTransaction string returned for {mint}")
            return

        transaction = aggregator.build_swap_transaction(tx_base64, keypair)
        if not transaction:
            await send_telegram_alert(f"❌ Failed to build swap transaction for {mint}")
            logging.error(f"[FORCEBUY] Swap TXN build failed for {mint}")
            return

        sig = aggregator.send_transaction(transaction, keypair)
        if not sig:
            await send_telegram_alert(f"❌ Failed to send transaction for {mint}")
            logging.error(f"[FORCEBUY] Transaction send failed for {mint}")
        else:
            await send_telegram_alert(f"✅ TX Sent: https://solscan.io/tx/{sig}")
            logging.info(f"[FORCEBUY] ✅ TX sent for {mint}: {sig}")

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
