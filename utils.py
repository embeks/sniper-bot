# utils.py — ELITE VERSION

import os
import json
import csv
import logging
from datetime import datetime

from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.api import Client

from jupiter_aggregator import JupiterAggregatorClient

load_dotenv()

# === ENV VARS ===
RPC_URL = os.getenv("RPC_URL")
SOLANA_PRIVATE_KEY = json.loads(os.getenv("SOLANA_PRIVATE_KEY"))
BUY_AMOUNT_SOL = float(os.getenv("BUY_AMOUNT_SOL", 0.01))
SLIPPAGE_BPS = int(os.getenv("SLIPPAGE_BPS", 100))

# === GLOBALS ===
client = Client(RPC_URL)
keypair = Keypair.from_bytes(bytes(SOLANA_PRIVATE_KEY))
wallet_pubkey = keypair.pubkey()
aggregator = JupiterAggregatorClient(RPC_URL)

# === TELEGRAM ===
from telegram import Bot
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
bot = Bot(token=TELEGRAM_TOKEN)

# === STATE ===
bot_status = {"running": True}
seen_tokens = set()
task_registry = []

# === UTILS ===
def is_valid_mint(account_data):
    for acc in account_data:
        if acc.get("owner") != "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA":
            return False
    return True

async def send_telegram_alert(msg):
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
    except Exception as e:
        logging.error(f"[TELEGRAM ERROR] {e}")

async def log_skipped_token(mint, reason):
    try:
        with open("skipped_tokens.txt", "a") as f:
            f.write(f"{mint} — {reason}\n")
    except Exception as e:
        logging.error(f"[SKIP LOG ERROR] {e}")

async def buy_token(mint):
    try:
        await send_telegram_alert(f"🚀 Buying token: {mint}")

        quote = await aggregator.get_quote(
            input_mint=Pubkey.from_string("So11111111111111111111111111111111111111112"),
            output_mint=Pubkey.from_string(mint),
            amount=int(BUY_AMOUNT_SOL * 1e9),
            slippage_bps=SLIPPAGE_BPS,
            user_pubkey=wallet_pubkey
        )

        if not quote:
            await send_telegram_alert(f"❌ Failed to get quote for {mint}")
            return False

        tx_bytes = await aggregator.get_swap_transaction(quote, keypair)
        if not tx_bytes:
            await send_telegram_alert(f"❌ Failed to build swap for {mint}")
            return False

        sig = await aggregator.send_transaction(tx_bytes, keypair)
        if not sig:
            await send_telegram_alert(f"❌ Failed to send buy TX for {mint}")
            return False

        await send_telegram_alert(f"✅ Buy TX sent: https://solscan.io/tx/{sig}")
        log_trade(mint, "BUY", sig)
        return True

    except Exception as e:
        await send_telegram_alert(f"❌ Buy error for {mint}: {e}")
        logging.exception(f"[BUY ERROR] {e}")
        return False

async def wait_and_auto_sell(mint):
    try:
        await asyncio.sleep(60)  # Placeholder for real strategy

        quote = await aggregator.get_quote(
            input_mint=Pubkey.from_string(mint),
            output_mint=Pubkey.from_string("So11111111111111111111111111111111111111112"),
            amount=0,  # You should fetch actual token balance
            slippage_bps=SLIPPAGE_BPS,
            user_pubkey=wallet_pubkey
        )

        if not quote:
            await send_telegram_alert(f"❌ Sell quote failed for {mint}")
            return

        tx_bytes = await aggregator.get_swap_transaction(quote, keypair)
        if not tx_bytes:
            await send_telegram_alert(f"❌ Failed to build sell swap for {mint}")
            return

        sig = await aggregator.send_transaction(tx_bytes, keypair)
        if not sig:
            await send_telegram_alert(f"❌ Failed to send sell tx for {mint}")
        else:
            await send_telegram_alert(f"✅ Sell TX sent: https://solscan.io/tx/{sig}")
            log_trade(mint, "SELL", sig)

    except Exception as e:
        await send_telegram_alert(f"❌ Sell failed for {mint}: {e}")
        logging.exception(f"[SELL ERROR] {e}")

async def get_trending_mints():
    return []  # Stub

def is_bot_running():
    return bot_status["running"]

def log_trade(mint, action, txid):
    try:
        with open("trades.csv", "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([datetime.utcnow().isoformat(), mint, action, txid])
    except Exception as e:
        logging.error(f"[TRADE LOG ERROR] {e}")
