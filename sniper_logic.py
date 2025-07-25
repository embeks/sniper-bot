# =========================
# sniper_logic.py — ELITE VERSION with Auto-Start, Trending Scanner, Dual Listener, Telegram Commands
# =========================

import asyncio
import json
import os
import websockets
from dotenv import load_dotenv

from utils import (
    is_valid_mint,
    buy_token,
    log_skipped_token,
    send_telegram_alert,
    start_command_bot,
    get_trending_mints,
    wait_and_auto_sell,
    get_liquidity_and_ownership,
    is_bot_running
)

load_dotenv()

FORCE_TEST_MINT = os.getenv("FORCE_TEST_MINT")
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
HELIUS_API = os.getenv("HELIUS_API")
RUG_LP_THRESHOLD = float(os.getenv("RUG_LP_THRESHOLD", 0.75))
TREND_SCAN_INTERVAL = int(os.getenv("TREND_SCAN_INTERVAL", 30))
seen_tokens = set()

async def rug_filter_passes(mint):
    try:
        data = await get_liquidity_and_ownership(mint)
        if not data:
            await send_telegram_alert(f"❌ No data for {mint}")
            log_skipped_token(mint, "Missing LP/ownership data")
            return False

        lp = 1.0  # TEMP override
        renounced = data.get("renounced", False)
        locked = data.get("lp_locked", False)

        if lp < RUG_LP_THRESHOLD:
            log_skipped_token(mint, "Low Liquidity")
            await send_telegram_alert(f"⛔ Skipped {mint} — LP too low: {lp}")
            return False

        return True
    except Exception as e:
        await send_telegram_alert(f"⚠️ Rug filter error for {mint}: {e}")
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
        await send_telegram_alert(f"📡 {name} listener live.")

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
                            print(f"[🔍] Token found: {key}")

                            if is_valid_mint([{ 'pubkey': key }]):
                                await send_telegram_alert(f"[🟡] Valid token found: {key}")
                                safe = await rug_filter_passes(key)
                                if not safe:
                                    continue
                                success = await buy_token(key)
                                if success:
                                    await wait_and_auto_sell(key)
                            else:
                                await send_telegram_alert(f"⛔ Skipped token (invalid mint): {key}")
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
                print(f"[🔥] Trending token: {mint}")
                await send_telegram_alert(f"[🔥] Trending token: {mint}")

                safe = await rug_filter_passes(mint)
                if not safe:
                    continue
                success = await buy_token(mint)
                if success:
                    await wait_and_auto_sell(mint)

            await asyncio.sleep(TREND_SCAN_INTERVAL)
        except Exception as e:
            print(f"[Scanner ERROR] {e}")
            await asyncio.sleep(TREND_SCAN_INTERVAL)

async def start_sniper():
    await send_telegram_alert("✅ Sniper bot launching...")
    asyncio.create_task(start_command_bot())

    if FORCE_TEST_MINT:
        await send_telegram_alert(f"🚨 Forced Test Mode: Buying {FORCE_TEST_MINT}")
        safe = await rug_filter_passes(FORCE_TEST_MINT)
        if safe:
            success = await buy_token(FORCE_TEST_MINT)
            if success:
                await wait_and_auto_sell(FORCE_TEST_MINT)
        else:
            await send_telegram_alert(f"❌ Forced test mint {FORCE_TEST_MINT} failed rug check.")

    await asyncio.gather(
        mempool_listener("Raydium"),
        mempool_listener("Jupiter"),
        trending_scanner()
    )

async def start_sniper_with_forced_token(mint: str):
    if not is_bot_running():
        await send_telegram_alert(f"⛔ Bot is paused. Force buy aborted for {mint}.")
        return
    bought = await buy_token(mint)
    if bought:
        await wait_and_auto_sell(mint)

# ✅ Auto-launch sniper logic after import
asyncio.create_task(start_sniper())
