import os
import asyncio
import logging
import csv
from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from telegram import Bot
from datetime import datetime

load_dotenv()

# === ENVIRONMENT ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")

# === TELEGRAM BOT ===
bot = Bot(token=TELEGRAM_TOKEN)

# === GLOBAL STATE ===
is_bot_running = False
task_registry = []
sniped_tokens = set()

# === WALLET ===
keypair = Keypair.from_bytes(bytes([int(k) for k in PRIVATE_KEY.strip('[]').split(',')]))
wallet_pubkey = keypair.pubkey()

# === TELEGRAM HELPERS ===
async def send_telegram_message(message):
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
    except Exception as e:
        logging.error(f"[TELEGRAM] Failed to send message: {e}")

def send_telegram_alert(message):
    asyncio.create_task(send_telegram_message(message))

# === TASK CONTROL ===
def add_task(task):
    task_registry.append(task)

def cancel_all_tasks():
    for task in task_registry:
        if not task.done():
            task.cancel()
    task_registry.clear()

# === SKIPPED TOKENS ===
def log_skipped_token(reason, mint):
    timestamp = datetime.utcnow().isoformat()
    log = f"[SKIPPED] {mint} | Reason: {reason} | {timestamp}"
    logging.info(log)
    send_telegram_alert(log)

# === CSV LOGGING ===
def log_trade_to_csv(mint, side, size_sol, size_token, price, pnl_sol, tx_sig):
    with open("trade_log.csv", "a", newline="") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow([
            datetime.utcnow().isoformat(),
            mint,
            side,
            size_sol,
            size_token,
            price,
            pnl_sol,
            tx_sig
        ])

# === UTILS ===
def parse_pubkey(pubkey_str):
    try:
        return Pubkey.from_string(pubkey_str)
    except Exception:
        return None

# === LP DATA ===
async def get_token_data(mint):
    # Placeholder for LP ownership and liquidity logic
    # This should be implemented with raw on-chain calls
    return {
        "liquidity": 1000000,
        "owner_count": 100,
        "is_blacklisted": False,
        "lp_locked": True,
        "ownership_renounced": True
    }

# === BOT STATE ===
def set_bot_running(state: bool):
    global is_bot_running
    is_bot_running = state

def get_bot_running():
    return is_bot_running

# === DYNAMIC BUY SIZE ===
def calculate_buy_amount_sol(liquidity):
    if liquidity < 1_000_000:
        return 0.01
    elif liquidity < 10_000_000:
        return 0.03
    else:
        return 0.05

# === STATUS COMMANDS ===
def get_current_holdings():
    # Placeholder for wallet token parsing
    return "No holdings yet."

def get_recent_logs():
    try:
        with open("trade_log.csv", "r") as f:
            lines = f.readlines()
            return "".join(lines[-10:])
    except:
        return "No trade logs available."

# === FORCE RESTART ===
def reset_sniped_tokens():
    global sniped_tokens
    sniped_tokens = set()
    return "Sniped token list cleared."

