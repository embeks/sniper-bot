import os
import ast
import json
import base64
import logging
import httpx
import asyncio
import time
import csv
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

# Solana / Solders
from dotenv import load_dotenv
from solders.transaction import VersionedTransaction
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.publickey import PublicKey as SolPublicKey
from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from spl.token.instructions import get_associated_token_address as spl_get_ata

# Import Raydium client
from raydium_aggregator import RaydiumAggregatorClient

# -----------------------------------------------------------------------------
# Setup & Environment
# -----------------------------------------------------------------------------
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

RPC_URL = os.getenv("RPC_URL")
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_USER_ID = os.getenv("TELEGRAM_USER_ID")  # kept for compatibility
BUY_AMOUNT_SOL = float(os.getenv("BUY_AMOUNT_SOL", 0.03))
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")
JUPITER_BASE_URL = os.getenv("JUPITER_BASE_URL", "https://quote-api.jup.ag")
SELL_MULTIPLIERS = os.getenv("SELL_MULTIPLIERS", "2,5,10").split(",")
SELL_TIMEOUT_SEC = int(os.getenv("SELL_TIMEOUT_SEC", 300))
RUG_LP_THRESHOLD = float(os.getenv("RUG_LP_THRESHOLD", 0.5))
BLACKLISTED_TOKENS = os.getenv("BLACKLISTED_TOKENS", "").split(",") if os.getenv("BLACKLISTED_TOKENS") else []

# Profit-based trading configuration
TAKE_PROFIT_1 = float(os.getenv("TAKE_PROFIT_1", 2.0))   # 2x
TAKE_PROFIT_2 = float(os.getenv("TAKE_PROFIT_2", 5.0))   # 5x
TAKE_PROFIT_3 = float(os.getenv("TAKE_PROFIT_3", 10.0))  # 10x
SELL_PERCENT_1 = float(os.getenv("SELL_PERCENT_1", 50))  # %
SELL_PERCENT_2 = float(os.getenv("SELL_PERCENT_2", 25))  # %
SELL_PERCENT_3 = float(os.getenv("SELL_PERCENT_3", 25))  # %
STOP_LOSS_PERCENT = float(os.getenv("STOP_LOSS_PERCENT", 50))      # %
TRAILING_STOP_PERCENT = float(os.getenv("TRAILING_STOP_PERCENT", 20))  # %
MAX_HOLD_TIME_SEC = int(os.getenv("MAX_HOLD_TIME_SEC", 3600))      # 1h
PRICE_CHECK_INTERVAL_SEC = int(os.getenv("PRICE_CHECK_INTERVAL_SEC", 10))

# Timer-based fallback % splits (unchanged defaults)
AUTO_SELL_PERCENT_2X = 50
AUTO_SELL_PERCENT_5X = 25
AUTO_SELL_PERCENT_10X = 25

# -----------------------------------------------------------------------------
# Clients
# -----------------------------------------------------------------------------
rpc = Client(RPC_URL, commitment=Confirmed)
raydium = RaydiumAggregatorClient(RPC_URL)

# -----------------------------------------------------------------------------
# Wallet Load (supports `[1,2,3,...]` or base58 string)
# -----------------------------------------------------------------------------
try:
    if SOLANA_PRIVATE_KEY and SOLANA_PRIVATE_KEY.strip().startswith("["):
        private_key_array = ast.literal_eval(SOLANA_PRIVATE_KEY)
        keypair = Keypair.from_seed(bytes(private_key_array[:32]))
    else:
        keypair = Keypair.from_base58_string(SOLANA_PRIVATE_KEY)
except Exception as e:
    raise ValueError(f"Failed to load wallet from SOLANA_PRIVATE_KEY: {e}")

wallet_pubkey = str(keypair.pubkey())

# Use TELEGRAM_CHAT_ID but support TELEGRAM_USER_ID
if not TELEGRAM_CHAT_ID and TELEGRAM_USER_ID:
    TELEGRAM_CHAT_ID = TELEGRAM_USER_ID

# -----------------------------------------------------------------------------
# Global State
# -----------------------------------------------------------------------------
OPEN_POSITIONS: Dict[str, Dict[str, Any]] = {}
BROKEN_TOKENS = set()
BOT_RUNNING = True
BLACKLIST_FILE = "blacklist.json"
TRADES_CSV_FILE = "trades.csv"
BLACKLIST = set(BLACKLISTED_TOKENS) if BLACKLISTED_TOKENS else set()

# -----------------------------------------------------------------------------
# Stats & Listener Status
# -----------------------------------------------------------------------------
daily_stats = {
    "tokens_scanned": 0,
    "snipes_attempted": 0,
    "snipes_succeeded": 0,
    "sells_executed": 0,
    "profit_sol": 0.0,
    "skip_reasons": {
        "low_lp": 0,
        "blacklist": 0,
        "malformed": 0,
        "buy_failed": 0
    }
}

# Status tracking
listener_status = {"Raydium": "OFFLINE", "Jupiter": "OFFLINE"}
last_activity = time.time()
last_seen_token = {"Raydium": time.time(), "Jupiter": time.time()}

def update_last_activity():
    global last_activity
    last_activity = time.time()

def increment_stat(stat_name: str, value: int = 1):
    if stat_name in daily_stats:
        daily_stats[stat_name] += value

def record_skip(reason: str):
    if reason in daily_stats["skip_reasons"]:
        daily_stats["skip_reasons"][reason] += 1

def is_bot_running():
    return BOT_RUNNING

def start_bot():
    global BOT_RUNNING
    BOT_RUNNING = True

def stop_bot():
    global BOT_RUNNING
    BOT_RUNNING = False

def mark_broken_token(mint: str, error_code: int):
    BROKEN_TOKENS.add(mint)
    log_skipped_token(mint, f"Marked as broken (error {error_code})")

def is_valid_mint(mint: str) -> bool:
    try:
        Pubkey.from_string(mint)
        return True
    except Exception:
        return False

# -----------------------------------------------------------------------------
# Telegram & Logging
# -----------------------------------------------------------------------------
async def send_telegram_alert(message: str):
    """Send alert to Telegram."""
    try:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            logging.error("Telegram env vars missing; cannot send alert.")
            return
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(url, json=payload)
    except Exception as e:
        logging.error(f"Telegram send failed: {e}")

def log_trade(mint: str, action: str, sol_amount: float, token_amount: float):
    """Log trade to CSV file."""
    try:
        with open(TRADES_CSV_FILE, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                datetime.now().isoformat(),
                mint,
                action,
                sol_amount,
                token_amount
            ])
    except Exception as e:
        logging.error(f"Failed to log trade: {e}")

def log_skipped_token(mint: str, reason: str):
    logging.info(f"[SKIP] {mint}: {reason}")

def get_wallet_summary() -> str:
    """Get wallet balance summary."""
    try:
        balance = rpc.get_balance(keypair.pubkey()).value / 1e9
        return f"Balance: {balance:.4f} SOL\nAddress: {wallet_pubkey}"
    except Exception:
        return "Failed to fetch wallet info"

def get_bot_status_message() -> str:
    """Get detailed bot status."""
    elapsed = int(time.time() - last_activity)
    raydium_elapsed = int(time.time() - last_seen_token["Raydium"])
    jupiter_elapsed = int(time.time() - last_seen_token["Jupiter"])

    return f"""
🤖 Bot: {'RUNNING' if BOT_RUNNING else 'PAUSED'}
📊 Daily Stats:
  • Scanned: {daily_stats['tokens_scanned']}
  • Attempted: {daily_stats['snipes_attempted']}
  • Succeeded: {daily_stats['snipes_succeeded']}
  • Sells: {daily_stats['sells_executed']}
  • P&L: {daily_stats['profit_sol']:.4f} SOL

⏱ Last Activity: {elapsed}s ago
🔌 Listeners:
  • Raydium: {listener_status['Raydium']} ({raydium_elapsed}s)
  • Jupiter: {listener_status['Jupiter']} ({jupiter_elapsed}s)

📈 Open Positions: {len(OPEN_POSITIONS)}
🚫 Broken Tokens: {len(BROKEN_TOKENS)}
"""

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def to_solana_pubkey(pk: Pubkey | str) -> SolPublicKey:
    """Convert solders.Pubkey (or str) to solana.PublicKey for SPL helpers."""
    if isinstance(pk, Pubkey):
        return SolPublicKey(str(pk))
    return SolPublicKey(str(pk))

async def get_liquidity_and_ownership(mint: str) -> Optional[Dict[str, Any]]:
    """Get liquidity info for a token using Raydium vaults (rough estimate)."""
    try:
        pool = raydium.find_pool("So11111111111111111111111111111111111111112", mint)
        if pool:
            sol_vault = Pubkey.from_string(
                pool["baseVault"] if pool["baseMint"] == "So11111111111111111111111111111111111111112" else pool["quoteVault"]
            )
            sol_balance = rpc.get_balance(sol_vault).value / 1e9
            return {"liquidity": sol_balance * 2}  # rough: assumes symmetric
    except Exception as e:
        logging.error(f"Failed to get liquidity: {e}")
    return None

async def get_trending_mints():
    """Placeholder for trending mints source."""
    return []

async def daily_stats_reset_loop():
    """Reset daily stats at midnight."""
    while True:
        try:
            now = datetime.now()
            midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
            seconds_until_midnight = (midnight - now).total_seconds()
            await asyncio.sleep(seconds_until_midnight)

            # Reset stats
            daily_stats["tokens_scanned"] = 0
            daily_stats["snipes_attempted"] = 0
            daily_stats["snipes_succeeded"] = 0
            daily_stats["sells_executed"] = 0
            daily_stats["profit_sol"] = 0.0
            for key in daily_stats["skip_reasons"]:
                daily_stats["skip_reasons"][key] = 0

            await send_telegram_alert("📊 Daily stats reset")
        except Exception as e:
            logging.error(f"Stats reset error: {e}")
            await asyncio.sleep(3600)

# -----------------------------------------------------------------------------
# Jupiter Integration
# -----------------------------------------------------------------------------
async def get_jupiter_quote(input_mint: str, output_mint: str, amount: int, slippage_bps: int = 100):
    """Get swap quote from Jupiter API."""
    try:
        url = f"{JUPITER_BASE_URL}/v6/quote"
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(slippage_bps),
            "onlyDirectRoutes": "false",
            "asLegacyTransaction": "false"
        }
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url, params=params)
            if response.status_code == 200:
                quote = response.json()
                logging.info(f"[Jupiter] Quote: {amount/1e9:.4f} input -> outAmount={quote.get('outAmount')}")
                return quote
            else:
                logging.warning(f"[Jupiter] Quote failed: {response.status_code}")
                return None
    except Exception as e:
        logging.error(f"[Jupiter] Quote error: {e}")
        return None

async def get_jupiter_swap_transaction(quote: dict, user_pubkey: str):
    """Get the swap transaction from Jupiter."""
    try:
        url = f"{JUPITER_BASE_URL}/v6/swap"
        body = {
            "quoteResponse": quote,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": 500000,  # 0.0005 SOL
            "slippageBps": 300
        }
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(url, json=body)
            if response.status_code == 200:
                data = response.json()
                if "swapTransaction" not in data:
                    logging.warning("[Jupiter] swapTransaction missing in response")
                    return None
                return data
            else:
                logging.warning(f"[Jupiter] Swap failed: {response.status_code}")
                return None
    except Exception as e:
        logging.error(f"[Jupiter] Swap build error: {e}")
        return None

async def execute_jupiter_swap(mint: str, amount_lamports: int) -> Optional[str]:
    """Execute a swap using Jupiter with proper solders signing + raw send."""
    try:
        input_mint = "So11111111111111111111111111111111111111112"  # SOL
        output_mint = mint

        logging.info(f"[Jupiter] Getting quote for {amount_lamports/1e9:.4f} SOL -> {mint[:8]}...")
        quote = await get_jupiter_quote(input_mint, output_mint, amount_lamports)
        if not quote:
            return None

        out_amount = int(quote.get("outAmount", 0))
        if out_amount == 0:
            logging.warning("[Jupiter] Quote returned zero output amount")
            return None

        logging.info("[Jupiter] Building swap transaction...")
        swap_data = await get_jupiter_swap_transaction(quote, wallet_pubkey)
        if not swap_data or "swapTransaction" not in swap_data:
            return None

        tx_bytes = base64.b64decode(swap_data["swapTransaction"])
        tx = VersionedTransaction.from_bytes(tx_bytes)

        # Sign using solders
        try:
            tx.sign([keypair])
        except Exception as e:
            logging.error(f"[Jupiter] Signing failed: {e}")
            return None

        # Send raw bytes
        try:
            sig = rpc.send_raw_transaction(
                bytes(tx),
                opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed, max_retries=3)
            )
            if sig.value:
                sig_str = str(sig.value)
                logging.info(f"[Jupiter] Transaction sent: {sig_str}")
                await asyncio.sleep(2)
                return sig_str
            logging.error(f"[Jupiter] send_raw_transaction returned no value: {sig}")
            return None
        except Exception as e:
            logging.error(f"[Jupiter] Send error: {e}")
            return None

    except Exception as e:
        logging.error(f"[Jupiter] Swap execution error: {e}")
        return None

async def execute_jupiter_sell(mint: str, amount: int) -> Optional[str]:
    """Execute a sell using Jupiter with proper solders signing + raw send."""
    try:
        input_mint = mint
        output_mint = "So11111111111111111111111111111111111111112"  # SOL

        logging.info(f"[Jupiter] Getting sell quote for {mint[:8]}...")
        quote = await get_jupiter_quote(input_mint, output_mint, amount)
        if not quote:
            return None

        swap_data = await get_jupiter_swap_transaction(quote, wallet_pubkey)
        if not swap_data or "swapTransaction" not in swap_data:
            return None

        tx_bytes = base64.b64decode(swap_data["swapTransaction"])
        tx = VersionedTransaction.from_bytes(tx_bytes)

        try:
            tx.sign([keypair])
        except Exception as e:
            logging.error(f"[Jupiter] Sell signing failed: {e}")
            return None

        try:
            sig = rpc.send_raw_transaction(
                bytes(tx),
                opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed, max_retries=3)
            )
            if sig.value:
                sig_str = str(sig.value)
                logging.info(f"[Jupiter] Sell transaction sent: {sig_str}")
                await asyncio.sleep(2)
                return sig_str
            logging.error("[Jupiter] Failed to send sell tx (no value)")
            return None
        except Exception as e:
            logging.error(f"[Jupiter] Sell send error: {e}")
            return None

    except Exception as e:
        logging.error(f"[Jupiter] Sell execution error: {e}")
        return None

# -----------------------------------------------------------------------------
# Transaction P&L Helpers
# -----------------------------------------------------------------------------
async def _get_transaction_json(signature: str, retries: int = 12, delay: float = 1.0) -> Optional[Dict[str, Any]]:
    """Polls the RPC for a transaction (jsonParsed). Waits up to ~12s by default."""
    for _ in range(retries):
        try:
            resp = rpc.get_transaction(signature, encoding="jsonParsed", max_supported_transaction_version=0)
            if resp and resp.value:
                return resp.value  # dict
        except Exception as e:
            logging.debug(f"get_transaction error: {e}")
        await asyncio.sleep(delay)
    return None

async def get_sol_delta_from_tx(signature: str) -> Optional[float]:
    """
    Returns SOL delta (in SOL) for our wallet from a confirmed transaction.
    Positive if we received SOL, negative if we spent SOL.
    """
    tx = await _get_transaction_json(signature)
    if not tx:
        logging.warning(f"Could not fetch transaction for P&L calc: {signature}")
        return None
    try:
        keys = tx["transaction"]["message"]["accountKeys"]
        # keys can be dicts with {"pubkey": "..."}
        key_list = [k["pubkey"] if isinstance(k, dict) and "pubkey" in k else (k if isinstance(k, str) else str(k)) for k in keys]
        if wallet_pubkey not in key_list:
            logging.warning("Wallet pubkey not in accountKeys; using index 0 for SOL delta")
            idx = 0
        else:
            idx = key_list.index(wallet_pubkey)

        pre_bal = tx["meta"]["preBalances"][idx]
        post_bal = tx["meta"]["postBalances"][idx]
        delta_sol = (post_bal - pre_bal) / 1e9
        return float(delta_sol)
    except Exception as e:
        logging.error(f"Failed to parse SOL delta from tx: {e}")
        return None

def _allocate_cost(position: Dict[str, Any], sell_percent: float) -> float:
    """
    Allocates a portion of the original cost based on percent sold,
    taken from position['remaining_cost_sol'].
    """
    remaining_cost = float(position.get("remaining_cost_sol", 0.0))
    if remaining_cost <= 0:
        return 0.0
    portion = max(0.0, min(1.0, sell_percent / 100.0))
    allocated = remaining_cost * portion
    position["remaining_cost_sol"] = max(0.0, remaining_cost - allocated)
    return allocated

# -----------------------------------------------------------------------------
# Buy / Sell
# -----------------------------------------------------------------------------
async def buy_token(mint: str):
    """Execute buy transaction for a token - Jupiter first, Raydium fallback."""
    amount = int(BUY_AMOUNT_SOL * 1e9)  # lamports

    try:
        if mint in BROKEN_TOKENS:
            await send_telegram_alert(f"❌ Skipped {mint} — broken token")
            log_skipped_token(mint, "Broken token")
            record_skip("malformed")
            return False

        if mint in BLACKLIST:
            await send_telegram_alert(f"⛔ Skipping blacklisted token {mint}")
            log_skipped_token(mint, "Blacklist")
            record_skip("blacklist")
            return False

        increment_stat("snipes_attempted", 1)
        update_last_activity()

        # Jupiter path
        logging.info(f"[Buy] Attempting Jupiter swap for {mint[:8]}...")
        jupiter_sig = await execute_jupiter_swap(mint, amount)

        if jupiter_sig:
            await send_telegram_alert(
                f"✅ Sniped {mint} via Jupiter — bought with {BUY_AMOUNT_SOL} SOL\n"
                f"TX: https://solscan.io/tx/{jupiter_sig}"
            )
            OPEN_POSITIONS[mint] = {
                "expected_token_amount": 0,       # optional: set from quote if you track it
                "buy_amount_sol": BUY_AMOUNT_SOL, # original cost
                "remaining_cost_sol": BUY_AMOUNT_SOL,  # used for cost allocation on partial sells
                "sold_stages": set(),
                "buy_sig": jupiter_sig
            }
            increment_stat("snipes_succeeded", 1)
            log_trade(mint, "BUY", BUY_AMOUNT_SOL, 0)
            return True

        # Raydium fallback (for brand new tokens)
        logging.info(f"[Buy] Jupiter failed, trying Raydium for {mint[:8]}...")
        input_mint = "So11111111111111111111111111111111111111112"  # SOL
        output_mint = mint

        pool = raydium.find_pool(input_mint, output_mint)
        if not pool:
            pool = raydium.find_pool(output_mint, input_mint)
            if not pool:
                await send_telegram_alert(f"⚠️ No pool found on Jupiter or Raydium for {mint}. Skipping.")
                log_skipped_token(mint, "No pool on Jupiter or Raydium")
                record_skip("malformed")
                return False

        tx = raydium.build_swap_transaction(keypair, input_mint, output_mint, amount)
        if not tx:
            await send_telegram_alert(f"❌ Failed to build Raydium swap TX for {mint}")
            mark_broken_token(mint, 0)
            return False

        sig = raydium.send_transaction(tx, keypair)
        if not sig:
            await send_telegram_alert(f"📉 Trade failed — TX send error for {mint}")
            mark_broken_token(mint, 0)
            return False

        await send_telegram_alert(
            f"✅ Sniped {mint} via Raydium — bought with {BUY_AMOUNT_SOL} SOL\n"
            f"TX: https://solscan.io/tx/{sig}"
        )
        OPEN_POSITIONS[mint] = {
            "expected_token_amount": 0,
            "buy_amount_sol": BUY_AMOUNT_SOL,
            "remaining_cost_sol": BUY_AMOUNT_SOL,
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
    """Execute sell transaction for a token - Jupiter first, Raydium fallback.
       On success, compute SOL proceeds from chain, allocate cost, and update P&L."""
    try:
        owner_solders = keypair.pubkey()
        owner_solana = to_solana_pubkey(owner_solders)
        mint_solana = to_solana_pubkey(Pubkey.from_string(mint))

        # Get ATA and balance (solana-py types for SPL helpers)
        token_account = spl_get_ata(owner_solana, mint_solana)

        try:
            response = rpc.get_token_account_balance(token_account)
            if not response or not hasattr(response, 'value') or not response.value:
                logging.warning(f"No token balance found for {mint}")
                await send_telegram_alert(f"⚠️ No token balance found for {mint}")
                return False
            balance = int(response.value.amount)
        except Exception as e:
            logging.error(f"Failed to get token balance for {mint}: {e}")
            await send_telegram_alert(f"⚠️ Failed to get balance for {mint}: {e}")
            return False

        amount = int(balance * percent / 100)
        if amount == 0:
            await send_telegram_alert(f"⚠️ Zero balance to sell for {mint}")
            return False

        # Jupiter path
        logging.info(f"[Sell] Attempting Jupiter sell for {mint[:8]}...")
        jupiter_sig = await execute_jupiter_sell(mint, amount)

        tx_sig = None
        if jupiter_sig:
            tx_sig = jupiter_sig
            await send_telegram_alert(
                f"✅ Sold {percent}% of {mint} via Jupiter\n"
                f"TX: https://solscan.io/tx/{jupiter_sig}"
            )
        else:
            # Raydium fallback
            logging.info(f"[Sell] Jupiter failed, trying Raydium for {mint[:8]}...")
            input_mint = mint
            output_mint = "So11111111111111111111111111111111111111112"  # SOL

            pool = raydium.find_pool(input_mint, output_mint)
            if not pool:
                pool = raydium.find_pool(output_mint, input_mint)
                if not pool:
                    await send_telegram_alert(f"⚠️ No pool for {mint}. Cannot sell.")
                    log_skipped_token(mint, "No pool for sell")
                    return False

            tx = raydium.build_swap_transaction(keypair, input_mint, output_mint, amount)
            if not tx:
                await send_telegram_alert(f"❌ Failed to build sell TX for {mint}")
                return False

            sig = raydium.send_transaction(tx, keypair)
            if not sig:
                await send_telegram_alert(f"❌ Failed to send sell tx for {mint}")
                return False

            tx_sig = sig
            await send_telegram_alert(
                f"✅ Sold {percent}% of {mint} via Raydium\n"
                f"TX: https://solscan.io/tx/{sig}"
            )

        # --- P&L accumulation block ---
        increment_stat("sells_executed", 1)
        log_trade(mint, f"SELL {percent}%", 0, amount)

        # Fetch actual SOL delta from the chain (includes fees)
        proceeds_sol = await get_sol_delta_from_tx(tx_sig)
        if proceeds_sol is None:
            logging.warning("P&L: Could not get SOL delta; skipping P&L update for this sell")
            return True  # Sale succeeded; just no P&L update

        # Allocate cost from remaining_cost_sol
        if mint in OPEN_POSITIONS:
            allocated_cost = _allocate_cost(OPEN_POSITIONS[mint], percent)
        else:
            allocated_cost = 0.0

        pnl = proceeds_sol - allocated_cost
        daily_stats["profit_sol"] += pnl

        # Optional: Telegram summary of this sell's P&L
        await send_telegram_alert(
            f"📒 P&L Update for {mint[:8]}:\n"
            f"Proceeds: {proceeds_sol:.6f} SOL\n"
            f"Allocated Cost: {allocated_cost:.6f} SOL\n"
            f"Δ P&L: {pnl:+.6f} SOL\n"
            f"Daily P&L: {daily_stats['profit_sol']:+.6f} SOL"
        )

        return True

    except Exception as e:
        await send_telegram_alert(f"❌ Sell failed for {mint}: {e}")
        log_skipped_token(mint, f"Sell failed: {e}")
        return False

# -----------------------------------------------------------------------------
# Pricing & Auto-sell
# -----------------------------------------------------------------------------
async def get_token_price_usd(mint: str) -> Optional[float]:
    """Get current token price in USD from Jupiter Price API."""
    try:
        url = "https://price.jup.ag/v6/price"
        params = {"ids": mint}
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(url, params=params)
            if response.status_code == 200:
                data = response.json()
                if "data" in data and mint in data["data"]:
                    price = data["data"][mint]["price"]
                    logging.debug(f"[Price] {mint[:8]}... = ${price:.6f}")
                    return float(price)
        return None
    except Exception as e:
        logging.debug(f"[Price] Error getting price for {mint}: {e}")
        return None

async def wait_and_auto_sell(mint: str):
    """Monitor position and auto-sell at REAL profit targets with risk management."""
    try:
        if mint not in OPEN_POSITIONS:
            logging.warning(f"No position found for {mint}")
            return

        position = OPEN_POSITIONS[mint]
        buy_amount_sol = position["buy_amount_sol"]

        # Wait for token to settle and get initial price
        await asyncio.sleep(10)

        # Get entry price with quick retries before timer fallback
        entry_price = None
        for _ in range(3):
            entry_price = await get_token_price_usd(mint)
            if entry_price:
                break
            await asyncio.sleep(0.7)

        if not entry_price:
            logging.warning(f"Could not get entry price for {mint} after retries, using timer-based fallback")
            await wait_and_auto_sell_timer_based(mint)
            return

        # Initialize tracking variables
        position["entry_price"] = entry_price
        position["highest_price"] = entry_price
        position["token_amount"] = position.get("expected_token_amount", 0)

        await send_telegram_alert(
            f"📊 Monitoring {mint[:8]}...\n"
            f"Entry: ${entry_price:.6f}\n"
            f"Targets: {TAKE_PROFIT_1}x/${entry_price*TAKE_PROFIT_1:.6f}, "
            f"{TAKE_PROFIT_2}x/${entry_price*TAKE_PROFIT_2:.6f}, "
            f"{TAKE_PROFIT_3}x/${entry_price*TAKE_PROFIT_3:.6f}\n"
            f"Stop Loss: -{STOP_LOSS_PERCENT:.0f}% | Trailing: {TRAILING_STOP_PERCENT:.0f}%"
        )

        # Monitor loop
        start_time = time.time()
        last_price_check = 0
        max_sell_attempts = 3
        sell_attempts = {"profit1": 0, "profit2": 0, "profit3": 0, "stop_loss": 0}

        while time.time() - start_time < MAX_HOLD_TIME_SEC:
            try:
                if time.time() - last_price_check < PRICE_CHECK_INTERVAL_SEC:
                    await asyncio.sleep(1)
                    continue

                last_price_check = time.time()
                current_price = await get_token_price_usd(mint)

                if not current_price:
                    logging.debug(f"Could not get price for {mint}, skipping this check")
                    await asyncio.sleep(PRICE_CHECK_INTERVAL_SEC)
                    continue

                # Profit metrics
                profit_multiplier = current_price / entry_price
                profit_percent = (profit_multiplier - 1) * 100

                # Update high for trailing stop
                if current_price > position["highest_price"]:
                    position["highest_price"] = current_price
                    logging.info(f"[{mint[:8]}] New high: ${current_price:.6f} ({profit_multiplier:.2f}x)")

                # Trailing stop (after at least one partial sell)
                drop_from_high = (position["highest_price"] - current_price) / position["highest_price"] * 100
                if drop_from_high >= TRAILING_STOP_PERCENT and len(position["sold_stages"]) > 0:
                    logging.info(f"[{mint[:8]}] Trailing stop triggered! Down {drop_from_high:.1f}% from peak")
                    if await sell_token(mint, 100):  # Sell all remaining
                        await send_telegram_alert(
                            f"⛔ Trailing stop triggered for {mint[:8]}!\n"
                            f"Price dropped {drop_from_high:.1f}% from peak ${position['highest_price']:.6f}"
                        )
                        break

                # Stop loss
                if profit_percent <= -STOP_LOSS_PERCENT and sell_attempts["stop_loss"] < max_sell_attempts:
                    sell_attempts["stop_loss"] += 1
                    logging.info(f"[{mint[:8]}] Stop loss triggered at {profit_percent:.1f}%")
                    if await sell_token(mint, 100):
                        await send_telegram_alert(
                            f"🛑 Stop loss triggered for {mint[:8]} at {profit_percent:.1f}%"
                        )
                        break

                # Profit targets
                if profit_multiplier >= TAKE_PROFIT_1 and "profit1" not in position["sold_stages"] and sell_attempts["profit1"] < max_sell_attempts:
                    sell_attempts["profit1"] += 1
                    if await sell_token(mint, SELL_PERCENT_1):
                        position["sold_stages"].add("profit1")
                        await send_telegram_alert(
                            f"💰 Hit {TAKE_PROFIT_1}x for {mint[:8]} — sold {SELL_PERCENT_1}%"
                        )

                if profit_multiplier >= TAKE_PROFIT_2 and "profit2" not in position["sold_stages"] and sell_attempts["profit2"] < max_sell_attempts:
                    sell_attempts["profit2"] += 1
                    if await sell_token(mint, SELL_PERCENT_2):
                        position["sold_stages"].add("profit2")
                        await send_telegram_alert(
                            f"🚀 Hit {TAKE_PROFIT_2}x for {mint[:8]} — sold {SELL_PERCENT_2}%"
                        )

                if profit_multiplier >= TAKE_PROFIT_3 and "profit3" not in position["sold_stages"] and sell_attempts["profit3"] < max_sell_attempts:
                    sell_attempts["profit3"] += 1
                    if await sell_token(mint, SELL_PERCENT_3):
                        position["sold_stages"].add("profit3")
                        await send_telegram_alert(
                            f"🌙 Hit {TAKE_PROFIT_3}x for {mint[:8]} — sold final {SELL_PERCENT_3}%"
                        )
                        break  # fully out

                # Periodic log
                if int((time.time() - start_time) % 60) == 0:
                    logging.info(
                        f"[{mint[:8]}] Price ${current_price:.6f} ({profit_multiplier:.2f}x) | "
                        f"High ${position['highest_price']:.6f} | Sold {position['sold_stages']}"
                    )

                # Done if all sold stages done
                if len(position["sold_stages"]) >= 3:
                    break

            except Exception as e:
                logging.error(f"Error monitoring {mint}: {e}")
                await asyncio.sleep(PRICE_CHECK_INTERVAL_SEC)

        # Time limit reached
        if time.time() - start_time >= MAX_HOLD_TIME_SEC:
            logging.info(f"[{mint[:8]}] Max hold time reached, force selling")
            if await sell_token(mint, 100):
                current_price = await get_token_price_usd(mint) or entry_price
                profit_percent = ((current_price / entry_price) - 1) * 100
                await send_telegram_alert(
                    f"⏰ Max hold time reached for {mint[:8]}\n"
                    f"Final P&L: {profit_percent:+.1f}%"
                )

        # Cleanup
        if mint in OPEN_POSITIONS:
            del OPEN_POSITIONS[mint]

    except Exception as e:
        logging.error(f"Auto-sell error for {mint}: {e}")
        await send_telegram_alert(f"⚠️ Auto-sell error for {mint}: {e}")

async def wait_and_auto_sell_timer_based(mint: str):
    """FALLBACK: Original timer-based selling if price feed fails."""
    try:
        if mint not in OPEN_POSITIONS:
            return

        position = OPEN_POSITIONS[mint]
        start_time = time.time()
        max_duration = 600
        max_sell_attempts = 3
        sell_attempts = {"2x": 0, "5x": 0, "10x": 0}

        while time.time() - start_time < max_duration:
            try:
                elapsed = time.time() - start_time

                if elapsed > 30 and "2x" not in position["sold_stages"] and sell_attempts["2x"] < max_sell_attempts:
                    sell_attempts["2x"] += 1
                    if await sell_token(mint, AUTO_SELL_PERCENT_2X):
                        position["sold_stages"].add("2x")
                        await send_telegram_alert(f"📈 Sold {AUTO_SELL_PERCENT_2X}% at 30s timer for {mint}")
                    elif sell_attempts["2x"] >= max_sell_attempts:
                        position["sold_stages"].add("2x")

                if elapsed > 120 and "5x" not in position["sold_stages"] and sell_attempts["5x"] < max_sell_attempts:
                    sell_attempts["5x"] += 1
                    if await sell_token(mint, AUTO_SELL_PERCENT_5X):
                        position["sold_stages"].add("5x")
                        await send_telegram_alert(f"🚀 Sold {AUTO_SELL_PERCENT_5X}% at 2min timer for {mint}")
                    elif sell_attempts["5x"] >= max_sell_attempts:
                        position["sold_stages"].add("5x")

                if elapsed > 300 and "10x" not in position["sold_stages"] and sell_attempts["10x"] < max_sell_attempts:
                    sell_attempts["10x"] += 1
                    if await sell_token(mint, AUTO_SELL_PERCENT_10X):
                        position["sold_stages"].add("10x")
                        await send_telegram_alert(f"🌙 Sold final {AUTO_SELL_PERCENT_10X}% at 5min timer for {mint}")
                        break
                    elif sell_attempts["10x"] >= max_sell_attempts:
                        position["sold_stages"].add("10x")
                        break

                if len(position["sold_stages"]) >= 3:
                    break

                await asyncio.sleep(10)

            except Exception as e:
                logging.error(f"Timer-based monitoring error for {mint}: {e}")
                await asyncio.sleep(10)

        if mint in OPEN_POSITIONS:
            del OPEN_POSITIONS[mint]

    except Exception as e:
        logging.error(f"Timer-based auto-sell error for {mint}: {e}")
