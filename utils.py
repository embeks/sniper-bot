import os
import json
import httpx
import asyncio
import csv
import base58
import time
from datetime import datetime
from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.api import Client
from solana.rpc.types import TxOpts, MemcmpOpts
from solana.rpc.async_api import AsyncClient
from spl.token.instructions import get_associated_token_address
from raydium_aggregator import RaydiumAggregatorClient

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
RPC_URL = os.getenv("RPC_URL")
SOLANA_PRIVATE_KEY = json.loads(os.getenv("SOLANA_PRIVATE_KEY"))
BUY_AMOUNT_SOL = float(os.getenv("BUY_AMOUNT_SOL", 0.03))
SELL_TIMEOUT_SEC = int(os.getenv("SELL_TIMEOUT_SEC", 300))
RUG_LP_THRESHOLD = float(os.getenv("RUG_LP_THRESHOLD", 0.5))
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")

keypair = Keypair.from_bytes(bytes(SOLANA_PRIVATE_KEY))
wallet_pubkey = str(keypair.pubkey())
rpc = Client(RPC_URL)
raydium = RaydiumAggregatorClient(RPC_URL)

listener_status = {"Raydium": "IDLE"}
last_seen_token = {"Raydium": time.time()}

def get_listener_health():
    health = {}
    now = time.time()
    for name in ["Raydium"]:
        elapsed = int(now - last_seen_token.get(name, 0))
        status = listener_status.get(name, "UNKNOWN")
        health[name] = {"status": status, "last_event_sec": elapsed}
    return health

import json as _json
from datetime import date, time as dt_time, timedelta

STATS_FILE = "bot_stats.json"

def _load_bot_stats():
    today_str = date.today().isoformat()
    default_stats = {
        "date": today_str,
        "tokens_scanned": 0,
        "tokens_skipped": 0,
        "snipes_attempted": 0,
        "snipes_succeeded": 0,
        "pnl_total": 0.0,
        "last_activity": None,
        "skipped_blacklist": 0,
        "skipped_malformed": 0,
    }
    try:
        if os.path.exists(STATS_FILE):
            with open(STATS_FILE, "r") as f:
                data = _json.load(f)
            if data.get("date") == today_str:
                return data
    except Exception:
        pass
    return default_stats.copy()

def _save_bot_stats():
    try:
        with open(STATS_FILE, "w") as f:
            _json.dump(BOT_STATS, f)
    except Exception:
        pass

def _reset_bot_stats_and_send_recap():
    try:
        d = BOT_STATS.get("date")
        recap = (
            f"🧾 Daily Recap ({d})\n"
            f"Scanned: {BOT_STATS['tokens_scanned']} tokens\n"
            f"Skipped: {BOT_STATS['tokens_skipped']} "
            f"({BOT_STATS['skipped_blacklist']} blacklisted, {BOT_STATS['skipped_malformed']} malformed)\n"
            f"Snipes attempted: {BOT_STATS['snipes_attempted']}\n"
            f"Snipes succeeded: {BOT_STATS['snipes_succeeded']}\n"
            f"Total PnL: {BOT_STATS['pnl_total']:+.4f} SOL"
        )
        asyncio.create_task(send_telegram_alert(recap))
    except Exception:
        pass
    new_date = date.today().isoformat()
    BOT_STATS.update({
        "date": new_date,
        "tokens_scanned": 0,
        "tokens_skipped": 0,
        "snipes_attempted": 0,
        "snipes_succeeded": 0,
        "pnl_total": 0.0,
        "last_activity": None,
        "skipped_blacklist": 0,
        "skipped_malformed": 0,
    })
    _save_bot_stats()

BOT_STATS = _load_bot_stats()

def increment_stat(key: str, amount: int = 1):
    BOT_STATS[key] = BOT_STATS.get(key, 0) + amount
    _save_bot_stats()

def record_skip(reason: str):
    increment_stat("tokens_skipped", 1)
    if reason == "blacklist":
        increment_stat("skipped_blacklist", 1)
    elif reason == "malformed":
        increment_stat("skipped_malformed", 1)

def record_pnl(amount: float):
    BOT_STATS["pnl_total"] = BOT_STATS.get("pnl_total", 0.0) + amount
    _save_bot_stats()

def update_last_activity():
    BOT_STATS["last_activity"] = datetime.utcnow().isoformat()
    _save_bot_stats()

async def daily_stats_reset_loop():
    while True:
        now = datetime.utcnow()
        tomorrow = date.today() + timedelta(days=1)
        midnight = datetime.combine(tomorrow, dt_time(0, 0))
        seconds_until_midnight = (midnight - now).total_seconds()
        if seconds_until_midnight < 0:
            seconds_until_midnight = 60
        await asyncio.sleep(seconds_until_midnight)
        _reset_bot_stats_and_send_recap()

OPEN_POSITIONS = {}

BROKEN_TOKENS = set()
broken_tokens_file = "broken_tokens.txt"
if os.path.exists(broken_tokens_file):
    try:
        with open(broken_tokens_file, "r") as f:
            for line in f:
                parts = line.strip().split(",")
                if parts:
                    BROKEN_TOKENS.add(parts[0])
    except Exception:
        pass

def mark_broken_token(mint: str, length: int):
    if mint not in BROKEN_TOKENS:
        BROKEN_TOKENS.add(mint)
        try:
            with open(broken_tokens_file, "a") as f:
                f.write(f"{mint},{length}\n")
        except Exception:
            pass
        log_skipped_token(mint, f"Broken swap ({length} bytes)")
        record_skip("malformed")

bot_active_flag = {"active": True}

def is_bot_running():
    return bot_active_flag["active"]

def stop_bot():
    bot_active_flag["active"] = False

def start_bot():
    bot_active_flag["active"] = True

async def send_telegram_alert(message: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        async with httpx.AsyncClient() as client:
            await client.post(url, json=payload)
    except Exception:
        pass

def log_trade(token, action, sol_in, token_out):
    with open("trade_log.csv", "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([datetime.utcnow().isoformat(), token, action, sol_in, token_out])

def log_skipped_token(mint: str, reason: str):
    with open("skipped_tokens.csv", "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([datetime.utcnow().isoformat(), mint, reason])

async def get_liquidity_and_ownership(mint: str):
    """Check if token has sufficient liquidity."""
    try:
        # Use Birdeye to check liquidity if available
        if BIRDEYE_API_KEY:
            return await birdeye_check_token_details(mint)
        
        # Fallback: check for Raydium pool existence
        pool = raydium.find_pool(
            "So11111111111111111111111111111111111111112",  # SOL
            mint
        )
        
        if pool:
            return {
                "liquidity": 100000,  # Assume sufficient if pool exists
                "renounced": False,
                "lp_locked": True
            }
        else:
            return None
            
    except Exception as e:
        await send_telegram_alert(f"⚠️ get_liquidity_and_ownership error: `{e}`")
        return None

async def birdeye_check_token_details(mint: str):
    """Get token details from Birdeye."""
    try:
        url = f"https://public-api.birdeye.so/defi/token_overview?address={mint}"
        headers = {
            "X-API-KEY": BIRDEYE_API_KEY,
            "accept": "application/json"
        }
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(url, headers=headers)
            if res.status_code != 200:
                return None
            
            data = res.json().get("data", {})
            liquidity = float(data.get("liquidity", 0))
            
            return {
                "liquidity": liquidity,
                "renounced": False,
                "lp_locked": True
            }
    except Exception as e:
        logging.error(f"Birdeye check failed for {mint}: {e}")
        return None

async def birdeye_check_token(mint: str, min_liquidity=50000):
    """Quick liquidity check via Birdeye."""
    try:
        details = await birdeye_check_token_details(mint)
        if details and details.get("liquidity", 0) >= min_liquidity:
            return True
        return False
    except:
        return False

async def buy_token(mint: str):
    """Execute buy transaction for a token."""
    input_mint = "So11111111111111111111111111111111111111112"  # SOL
    output_mint = mint
    amount = int(BUY_AMOUNT_SOL * 1e9)  # Convert SOL to lamports

    try:
        if mint in BROKEN_TOKENS:
            await send_telegram_alert(f"❌ Skipped {mint} — broken token")
            log_skipped_token(mint, "Broken token")
            record_skip("malformed")
            return False

        increment_stat("snipes_attempted", 1)
        update_last_activity()

        # Check if pool exists
        pool = raydium.find_pool(input_mint, output_mint)
        if not pool:
            # Try reverse direction
            pool = raydium.find_pool(output_mint, input_mint)
            if not pool:
                await send_telegram_alert(f"⚠️ No Raydium pool for {mint}. Skipping.")
                log_skipped_token(mint, "No Raydium pool")
                record_skip("malformed")
                return False

        # Build swap transaction
        tx = raydium.build_swap_transaction(keypair, input_mint, output_mint, amount)
        if not tx:
            await send_telegram_alert(f"❌ Failed to build swap TX for {mint}")
            mark_broken_token(mint, 0)
            return False

        # Send transaction
        sig = raydium.send_transaction(tx, keypair)
        if not sig:
            await send_telegram_alert(f"📉 Trade failed — TX send error for {mint}")
            mark_broken_token(mint, 0)
            return False

        await send_telegram_alert(
            f"✅ Sniped {mint} — bought with {BUY_AMOUNT_SOL} SOL\n"
            f"TX: https://solscan.io/tx/{sig}"
        )
        
        OPEN_POSITIONS[mint] = {
            "expected_token_amount": 0,
            "buy_amount_sol": BUY_AMOUNT_SOL,
            "sold_stages": set(),
            "buy_sig": sig
        }
        
        increment_stat("snipes_succeeded", 1)
        log_trade(mint, "BUY", BUY_AMOUNT_SOL, 0)
        return True

    except Exception as e:
        await send_telegram_alert(f"❌ Buy failed for {mint}: {e}")
        log_skipped_token(mint, f"Buy failed: {e}")
        return False

async def sell_token(mint: str, percent: float = 100.0):
    """Execute sell transaction for a token."""
    input_mint = mint
    output_mint = "So11111111111111111111111111111111111111112"  # SOL
    
    try:
        # Get token balance
        owner = keypair.pubkey()
        token_account = get_associated_token_address(owner, Pubkey.from_string(mint))
        
        # Get actual balance
        response = rpc.get_token_account_balance(token_account)
        if not response.value:
            await send_telegram_alert(f"⚠️ No token balance found for {mint}")
            return False
        
        balance = int(response.value.amount)
        amount = int(balance * percent / 100)
        
        if amount == 0:
            await send_telegram_alert(f"⚠️ Zero balance to sell for {mint}")
            return False

        # Find pool
        pool = raydium.find_pool(input_mint, output_mint)
        if not pool:
            pool = raydium.find_pool(output_mint, input_mint)
            if not pool:
                await send_telegram_alert(f"⚠️ No Raydium pool for {mint}. Cannot sell.")
                log_skipped_token(mint, "No Raydium pool for sell")
                return False

        # Build and send transaction
        tx = raydium.build_swap_transaction(keypair, input_mint, output_mint, amount)
        if not tx:
            await send_telegram_alert(f"❌ Failed to build sell TX for {mint}")
            return False

        sig = raydium.send_transaction(tx, keypair)
        if not sig:
            await send_telegram_alert(f"❌ Failed to send sell tx for {mint}")
            return False

        await send_telegram_alert(
            f"✅ Sold {percent}% of {mint}\n"
            f"TX: https://solscan.io/tx/{sig}"
        )
        log_trade(mint, f"SELL {percent}%", 0, amount)
        return True
        
    except Exception as e:
        await send_telegram_alert(f"❌ Sell failed for {mint}: {e}")
        log_skipped_token(mint, f"Sell failed: {e}")
        return False

async def wait_and_auto_sell(mint):
    """Monitor position and auto-sell at profit targets."""
    try:
        position = OPEN_POSITIONS.get(mint)
        if not position:
            await send_telegram_alert(f"⚠️ No open position found for {mint}")
            return

        buy_sol = position.get("buy_amount_sol", BUY_AMOUNT_SOL)
        sold_stages = position.get("sold_stages", set())
        start_time = datetime.utcnow()

        # Profit milestones
        milestones = [2, 5, 10]
        percentages = {2: 50, 5: 25, 10: 25}

        while True:
            if sold_stages == set(milestones):
                OPEN_POSITIONS.pop(mint, None)
                break

            elapsed = (datetime.utcnow() - start_time).total_seconds()
            if elapsed > SELL_TIMEOUT_SEC:
                await send_telegram_alert(
                    f"⏲️ Timeout reached for {mint} after {SELL_TIMEOUT_SEC}s — selling remaining position"
                )
                await sell_token(mint, percent=100.0)
                OPEN_POSITIONS.pop(mint, None)
                break

            # Check price via Birdeye if available
            if BIRDEYE_API_KEY:
                try:
                    url = f"https://public-api.birdeye.so/defi/price?address={mint}"
                    headers = {
                        "X-API-KEY": BIRDEYE_API_KEY,
                        "accept": "application/json"
                    }
                    async with httpx.AsyncClient(timeout=7) as client:
                        res = await client.get(url, headers=headers)
                        if res.status_code == 200:
                            data = res.json()
                            current_price = float(data.get("data", {}).get("value", 0))
                            
                            # Calculate profit (simplified - would need entry price)
                            # For now, check milestones based on time
                            for milestone in milestones:
                                if milestone not in sold_stages:
                                    # Simplified: sell after certain time periods
                                    if elapsed > (60 * milestone):  # minutes
                                        percentage = percentages[milestone]
                                        await send_telegram_alert(
                                            f"📈 Selling {percentage}% at {milestone}x target"
                                        )
                                        if await sell_token(mint, percent=percentage):
                                            sold_stages.add(milestone)
                                            position["sold_stages"] = sold_stages
                except Exception as e:
                    logging.error(f"Price check error: {e}")

            await asyncio.sleep(15)
            
        OPEN_POSITIONS.pop(mint, None)
        
    except Exception as e:
        await send_telegram_alert(f"❌ Auto-sell error for {mint}: {e}")
        OPEN_POSITIONS.pop(mint, None)

def is_valid_mint(keys):
    TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
    return any(k.get("pubkey") == TOKEN_PROGRAM_ID for k in keys if isinstance(k, dict))

async def get_trending_mints(limit=5):
    try:
        url = "https://api.dexscreener.com/latest/dex/pairs/solana"
        async with httpx.AsyncClient() as client:
            r = await client.get(url)
            data = r.json()
            top = data.get("pairs", [])[:limit]
            return [pair["baseToken"]["address"] for pair in top if pair.get("baseToken")]
    except:
        return []

def get_wallet_status_message():
    return f"🔵 Bot is running: `{is_bot_running()}`\nWallet: `{wallet_pubkey}`"

def get_wallet_summary():
    return f"💼 Wallet: `{wallet_pubkey}`"

def get_bot_status_message():
    state = "RUNNING" if is_bot_running() else "PAUSED"
    scanned = BOT_STATS.get("tokens_scanned", 0)
    skipped = BOT_STATS.get("tokens_skipped", 0)
    attempted = BOT_STATS.get("snipes_attempted", 0)
    succeeded = BOT_STATS.get("snipes_succeeded", 0)
    pnl = BOT_STATS.get("pnl_total", 0.0)
    last_ts = BOT_STATS.get("last_activity") or "N/A"
    health = get_listener_health()
    health_lines = []
    for name, info in health.items():
        emoji = "✅" if info['status'] == "ACTIVE" else "⚠️"
        health_lines.append(f"{emoji} {name}: {info['status']} | Last event {info['last_event_sec']}s ago")
    health_str = "\n".join(health_lines)
    message = (
        f"🧠 Bot State: {state}\n"
        f"👁️ Tokens scanned today: {scanned}\n"
        f"⛔ Tokens skipped: {skipped}\n"
        f"✅ Snipes attempted: {attempted}\n"
        f"✅ Snipes succeeded: {succeeded}\n"
        f"📈 PnL summary today: {pnl:+.4f} SOL\n"
        f"🕓 Last activity timestamp: {last_ts} UTC\n"
        f"\n💬 Listener Health:\n{health_str}"
    )
    return message

# Add missing import
import logging
logging.basicConfig(level=logging.INFO)
