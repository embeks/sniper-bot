import os
import json
import httpx
import asyncio
import csv
import base58
import time  # <--- for heartbeat tracking
from datetime import datetime
from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.api import Client
from solana.transaction import Transaction
from solana.rpc.types import TxOpts, MemcmpOpts
from solana.rpc.async_api import AsyncClient
from spl.token.instructions import approve, get_associated_token_address
from jupiter_aggregator import JupiterAggregatorClient

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
RPC_URL = os.getenv("RPC_URL")
SOLANA_PRIVATE_KEY = json.loads(os.getenv("SOLANA_PRIVATE_KEY"))
BUY_AMOUNT_SOL = float(os.getenv("BUY_AMOUNT_SOL", 0.03))
SELL_TIMEOUT_SEC = int(os.getenv("SELL_TIMEOUT_SEC", 300))
RUG_LP_THRESHOLD = float(os.getenv("RUG_LP_THRESHOLD", 0.5))

keypair = Keypair.from_bytes(bytes(SOLANA_PRIVATE_KEY))
wallet_pubkey = str(keypair.pubkey())
rpc = Client(RPC_URL)
jupiter = JupiterAggregatorClient(RPC_URL)

# === AGENT MODE: Listener Health State (GLOBAL) ===
listener_status = {"Raydium": "IDLE", "Jupiter": "IDLE"}
last_seen_token = {"Raydium": time.time(), "Jupiter": time.time()}  # use epoch time for easy math

def get_listener_health():
    health = {}
    now = time.time()
    for name in ["Raydium", "Jupiter"]:
        elapsed = int(now - last_seen_token.get(name, 0))
        status = listener_status.get(name, "UNKNOWN")
        health[name] = {"status": status, "last_event_sec": elapsed}
    return health
# === END AGENT MODE LISTENER HEALTH ===

# -----------------------------------------------------------------------------
# Bot statistics tracking
#
# We track daily counts of scanned tokens, skipped tokens, snipes attempted,
# snipes succeeded, cumulative PnL, and the last activity timestamp. These
# statistics are persisted to a JSON file (bot_stats.json) and reset each day
# at midnight. A daily recap is sent to Telegram with the summary.

import json as _json
from datetime import date, time as dt_time, timedelta

STATS_FILE = "bot_stats.json"

def _load_bot_stats():
    """Load statistics from disk or initialize default stats for today."""
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
            # If the stored stats are for today, return them; otherwise reset
            if data.get("date") == today_str:
                return data
        # Not found or outdated -> return default
    except Exception:
        pass
    return default_stats.copy()

def _save_bot_stats():
    """Persist the current stats dictionary to disk."""
    try:
        with open(STATS_FILE, "w") as f:
            _json.dump(BOT_STATS, f)
    except Exception:
        pass

def _reset_bot_stats_and_send_recap():
    """Reset stats at midnight and send a recap via Telegram."""
    try:
        # Prepare recap message based on current stats
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
        # Send recap
        asyncio.create_task(send_telegram_alert(recap))
    except Exception:
        pass
    # Reset stats for new day
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

# Initialize global stats
BOT_STATS = _load_bot_stats()

def increment_stat(key: str, amount: int = 1):
    """Increment a numeric stat and persist the change."""
    BOT_STATS[key] = BOT_STATS.get(key, 0) + amount
    _save_bot_stats()

def record_skip(reason: str):
    """Record a skipped token with reason: 'blacklist' or 'malformed'."""
    increment_stat("tokens_skipped", 1)
    if reason == "blacklist":
        increment_stat("skipped_blacklist", 1)
    elif reason == "malformed":
        increment_stat("skipped_malformed", 1)

def record_pnl(amount: float):
    """Add realized PnL (in SOL) to cumulative total."""
    BOT_STATS["pnl_total"] = BOT_STATS.get("pnl_total", 0.0) + amount
    _save_bot_stats()

def update_last_activity():
    """Update the last activity timestamp to now (ISO format)."""
    BOT_STATS["last_activity"] = datetime.utcnow().isoformat()
    _save_bot_stats()

async def daily_stats_reset_loop():
    """Background task that waits until midnight and resets stats each day."""
    while True:
        now = datetime.utcnow()
        # Compute seconds until next midnight UTC
        tomorrow = date.today() + timedelta(days=1)
        midnight = datetime.combine(tomorrow, dt_time(0, 0))
        seconds_until_midnight = (midnight - now).total_seconds()
        if seconds_until_midnight < 0:
            seconds_until_midnight = 60  # fallback
        await asyncio.sleep(seconds_until_midnight)
        _reset_bot_stats_and_send_recap()

# Track open positions for dynamic price-based auto-sell logic.
OPEN_POSITIONS = {}

# Load and track tokens for which Jupiter consistently returns malformed swap transactions.
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
    """Record a mint as broken and persist to disk."""
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
    try:
        async with AsyncClient(RPC_URL) as client:
            filters = [
                {"dataSize": 3248},
                {"memcmp": MemcmpOpts(
                    offset=72,
                    bytes=base58.b58encode(Pubkey.from_string(mint).to_bytes()).decode()
                )}
            ]
            res = await client.get_program_accounts(
                Pubkey.from_string("RVKd61ztZW9jqhDXnTBu6UBFygcBPzjcZijMdtaiPqK"),
                encoding="jsonParsed",
                filters=filters
            )
            if not res.value:
                await send_telegram_alert(
                    f"\ud83d\udcec No LP accounts found for `{mint}`.\n"
                    f"Raydium res.value: ```{json.dumps(res.value, indent=2)}```"
                )
                return None

            info = res.value[0].account.data["parsed"]["info"]
            lp_token_supply = float(info.get("lpMintSupply", 0)) / 1e9
            return {
                "liquidity": lp_token_supply,
                "renounced": False,
                "lp_locked": True
            }
    except Exception as e:
        await send_telegram_alert(f"\u26a0\ufe0f get_liquidity_and_ownership error: `{e}`")
        return None

async def approve_token_if_needed(mint):
    try:
        mint_pubkey = Pubkey.from_string(mint)
        ata = get_associated_token_address(keypair.pubkey(), mint_pubkey)
        tx = Transaction().add(approve(
            program_id=Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"),
            source=ata,
            delegate=keypair.pubkey(),
            owner=keypair.pubkey(),
            amount=9999999999
        ))
        rpc.send_transaction(tx, keypair, opts=TxOpts(skip_confirmation=True))
    except:
        pass

async def buy_token(mint: str):
    input_mint = Pubkey.from_string("So11111111111111111111111111111111111111112")
    output_mint = Pubkey.from_string(mint)
    amount = int(BUY_AMOUNT_SOL * 1e9)

    try:
        # Skip tokens already marked as broken
        if mint in BROKEN_TOKENS:
            await send_telegram_alert(f"❌ Skipped {mint} — Jupiter sent broken transaction")
            log_skipped_token(mint, "Broken token")
            record_skip("malformed")
            return False

        # Increment snipes attempted and update last activity
        increment_stat("snipes_attempted", 1)
        update_last_activity()
        route = await jupiter.get_quote(input_mint, output_mint, amount, user_pubkey=keypair.pubkey())
        if not route:
            await send_telegram_alert(f"\u26a0\ufe0f Jupiter quote failed for {mint}, trying Raydium fallback")
            route = await jupiter.get_quote(input_mint, output_mint, amount, only_direct_routes=True, user_pubkey=keypair.pubkey())

        if not route:
            await send_telegram_alert(f"\u274c No valid quote for {mint} (Jupiter & Raydium failed)")
            log_skipped_token(mint, "No valid quote")
            record_skip("malformed")
            return False

        swap_tx_base64 = await jupiter.get_swap_transaction(route, keypair)
        # If no swap returned (HTTP 400 or error), mark token as broken and skip.
        if not swap_tx_base64 or not isinstance(swap_tx_base64, str):
            await send_telegram_alert(f"❌ Skipped {mint} — Jupiter sent broken transaction")
            mark_broken_token(mint, 0)
            return False

        # Determine the decoded byte length for classification.
        try:
            import base64 as _b64
            decoded_bytes = _b64.b64decode(swap_tx_base64.replace("\n", "").replace(" ", "").strip())
            decoded_length = len(decoded_bytes)
        except Exception:
            decoded_bytes = b""
            decoded_length = 0

        # Build the VersionedTransaction from the swap. If parsing fails, mark broken.
        versioned_tx = jupiter.build_swap_transaction(swap_tx_base64, keypair)
        if not versioned_tx or decoded_length < 900:
            await send_telegram_alert(f"❌ Skipped {mint} — Jupiter sent broken transaction")
            mark_broken_token(mint, decoded_length)
            return False

        # Send the transaction. If sending fails, mark broken.
        sig = jupiter.send_transaction(versioned_tx, keypair)
        if not sig:
            await send_telegram_alert(f"📉 Trade failed — fallback RPC used, still broken for {mint}")
            mark_broken_token(mint, decoded_length)
            return False

        await send_telegram_alert(f"✅ Sniped {mint} — bought at {BUY_AMOUNT_SOL} SOL\nhttps://solscan.io/tx/{sig}")
        # Record the position for dynamic price monitoring
        try:
            expected_token_amount = int(route.get("outAmount", 0))
        except Exception:
            expected_token_amount = 0
        OPEN_POSITIONS[mint] = {
            "expected_token_amount": expected_token_amount,
            "buy_amount_sol": BUY_AMOUNT_SOL,
            "sold_stages": set(),
        }
        # Update stats for successful snipe
        increment_stat("snipes_succeeded", 1)
        log_trade(mint, "BUY", BUY_AMOUNT_SOL, 0)
        return True

    except Exception as e:
        await send_telegram_alert(f"\u274c Buy failed for {mint}: {e}")
        log_skipped_token(mint, f"Buy failed: {e}")
        return False

async def sell_token(mint: str, percent: float = 100.0):
    input_mint = Pubkey.from_string(mint)
    output_mint = Pubkey.from_string("So11111111111111111111111111111111111111112")
    amount = int(BUY_AMOUNT_SOL * 1e9 * percent / 100)

    try:
        route = await jupiter.get_quote(input_mint, output_mint, amount, user_pubkey=keypair.pubkey())
        if not route:
            route = await jupiter.get_quote(input_mint, output_mint, amount, only_direct_routes=True, user_pubkey=keypair.pubkey())

        if not route:
            await send_telegram_alert(f"\u274c No sell quote for {mint}")
            log_skipped_token(mint, "No sell quote")
            return False

        swap_tx_base64 = await jupiter.get_swap_transaction(route, keypair)
        if not swap_tx_base64:
            await send_telegram_alert(f"\u274c Sell swap fetch failed for {mint}")
            log_skipped_token(mint, "Sell swap fetch failed")
            return False

        versioned_tx = jupiter.build_swap_transaction(swap_tx_base64, keypair)
        if not versioned_tx:
            await send_telegram_alert(f"\u274c Failed to build VersionedTransaction for {mint}")
            log_skipped_token(mint, "Sell TX build failed")
            return False

        sig = jupiter.send_transaction(versioned_tx, keypair)
        if not sig:
            await send_telegram_alert(f"\u274c Failed to send sell tx for {mint}")
            log_skipped_token(mint, "Sell TX send failed")
            return False

        await send_telegram_alert(f"\u2705 Sell {percent}% sent: https://solscan.io/tx/{sig}")
        log_trade(mint, f"SELL {percent}%", 0, route.get("outAmount", 0) / 1e9)
        return True
    except Exception as e:
        await send_telegram_alert(f"\u274c Sell failed for {mint}: {e}")
        log_skipped_token(mint, f"Sell failed: {e}")
        return False

async def wait_and_auto_sell(mint):
    """
    Monitor the token's price in real time and execute sell orders based on
    configurable profit targets and stop-loss conditions.

    The bot will:
      • Sell 50% of the position when the price reaches 2× the entry (2x).
      • Sell 25% when the price reaches 5× the entry (5x).
      • Sell the remaining 25% when the price reaches 10× the entry (10x).
      • Exit the entire position if the price drops 20% below the entry.
      • If no price movement for 5 minutes (no 2x achieved), sell all.

    It sends Telegram alerts with the PnL for each sale and logs the trades. If an
    exception occurs, the function reports the error via Telegram.
    """
    try:
        position = OPEN_POSITIONS.get(mint)
        if not position:
            await send_telegram_alert(f"⚠️ No open position found for {mint}. Skipping auto-sell.")
            return

        expected_amount = position.get("expected_token_amount", 0)
        buy_sol = position.get("buy_amount_sol", BUY_AMOUNT_SOL)
        sold_stages = position.get("sold_stages", set())
        start_time = datetime.utcnow()

        # Define profit milestones and percentages to sell
        milestones = [2, 5, 10]
        percentages = {2: 50, 5: 25, 10: 25}

        while True:
            # Break if all milestones met or position no longer exists
            if sold_stages == set(milestones):
                break

            # Check elapsed time
            elapsed = (datetime.utcnow() - start_time).total_seconds()
            if elapsed > 300:  # 5 minutes fallback
                await send_telegram_alert(f"⏲️ No price movement for {mint} after 5 minutes — exiting position")
                await sell_token(mint, percent=100.0)
                OPEN_POSITIONS.pop(mint, None)
                break

            # Fetch current price by quoting the full expected token amount back to SOL
            try:
                quote = await jupiter.get_quote(
                    input_mint=Pubkey.from_string(mint),
                    output_mint=Pubkey.from_string("So11111111111111111111111111111111111111112"),
                    amount=expected_amount,
                    user_pubkey=keypair.pubkey()
                )
                if quote and "outAmount" in quote:
                    current_sol = quote["outAmount"] / 1e9
                    ratio = current_sol / buy_sol
                else:
                    ratio = 0
            except Exception:
                ratio = 0

            # Stop-loss: price dropped below 80% of entry
            if ratio <= 0.8:
                await send_telegram_alert(f"📉 Price drop detected for {mint} (ratio {ratio:.2f}) — selling all")
                await sell_token(mint, percent=100.0)
                OPEN_POSITIONS.pop(mint, None)
                break

            # Iterate milestones and sell if threshold met
            for milestone in milestones:
                if milestone not in sold_stages and ratio >= milestone:
                    percent = percentages[milestone]
                    success = await sell_token(mint, percent=percent)
                    if success:
                        # Calculate PnL in SOL terms
                        pnl = current_sol - buy_sol
                        await send_telegram_alert(f"📈 Sold {percent}% at {milestone}x — locked in profit (PnL: +{pnl:.4f} SOL)")
                        sold_stages.add(milestone)
                    # Update stored sold stages
                    position["sold_stages"] = sold_stages
            # Sleep briefly before next check to avoid spamming
            await asyncio.sleep(15)
        # Clean up after all sells
        OPEN_POSITIONS.pop(mint, None)
    except Exception as e:
        await send_telegram_alert(f"\u274c Auto-sell error for {mint}: {e}")

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
    return f"\ud83d\udd32 Bot is running: `{is_bot_running()}`\nWallet: `{wallet_pubkey}`"

def get_wallet_summary():
    return f"\ud83d\udcbc Wallet: `{wallet_pubkey}`"

def get_bot_status_message():
    """Construct a multi-line status summary for Telegram."""
    # Determine bot state
    state = "RUNNING" if is_bot_running() else "PAUSED"
    scanned = BOT_STATS.get("tokens_scanned", 0)
    skipped = BOT_STATS.get("tokens_skipped", 0)
    attempted = BOT_STATS.get("snipes_attempted", 0)
    succeeded = BOT_STATS.get("snipes_succeeded", 0)
    pnl = BOT_STATS.get("pnl_total", 0.0)
    last_ts = BOT_STATS.get("last_activity") or "N/A"
    # --- Add listener health report to status ---
    health = get_listener_health()
    health_lines = []
    for name, info in health.items():
        emoji = "✅" if info['status'] == "ACTIVE" else "⚠️"
        health_lines.append(f"{emoji} {name}: {info['status']} | Last event {info['last_event_sec']}s ago")
    health_str = "\n".join(health_lines)
    message = (
        f"\U0001f9e0 Bot State: {state}\n"
        f"\U0001f441\ufe0f Tokens scanned today: {scanned}\n"
        f"\u26d4 Tokens skipped: {skipped}\n"
        f"\u2705 Snipes attempted: {attempted}\n"
        f"\u2705 Snipes succeeded: {succeeded}\n"
        f"\U0001f4c8 PnL summary today: {pnl:+.4f} SOL\n"
        f"\U0001f553 Last activity timestamp: {last_ts} UTC\n"
        f"\n\U0001f4ac Listener Health:\n{health_str}"
    )
    return message
