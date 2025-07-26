import os
import json
import csv
import time
import base64
import asyncio
from datetime import datetime
from typing import List

from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solana.rpc.async_api import AsyncClient
from solana.transaction import Transaction

from jupiter_aggregator import JupiterAggregatorClient

from telegram import Bot

# === ENV ===
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
RPC_URL = os.getenv("RPC_URL")
PRIVATE_KEY = json.loads(os.getenv("SOLANA_PRIVATE_KEY"))

# === WALLET ===
keypair = Keypair.from_bytes(bytes(PRIVATE_KEY))
wallet_pubkey = keypair.pubkey()

# === TELEGRAM ===
bot = Bot(token=TELEGRAM_BOT_TOKEN)

# === GLOBAL ===
running_tasks = []
sniped_tokens = set()
jupiter = JupiterAggregatorClient(RPC_URL)

# === UTILS ===
def is_valid_mint(mint: str) -> bool:
    return len(mint) == 44 and not mint.startswith("So11111111111111111111111111111111111111112")

def log_skipped_token(mint: str, reason: str):
    timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    with open("skipped_tokens.log", "a") as f:
        f.write(f"{timestamp} | {mint} | {reason}\n")

def log_trade(mint: str, buy_price: float, sell_price: float, pnl: float):
    file_exists = os.path.isfile("trades.csv")
    with open("trades.csv", mode="a", newline="") as file:
        writer = csv.writer(file)
        if not file_exists:
            writer.writerow(["Timestamp", "Token Mint", "Buy Price", "Sell Price", "PnL"])
        writer.writerow([datetime.utcnow().isoformat(), mint, buy_price, sell_price, pnl])

def send_telegram_alert(message: str):
    try:
        asyncio.create_task(bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message))
    except Exception as e:
        print(f"Failed to send Telegram alert: {e}")

def send_telegram_message(text: str):
    try:
        asyncio.run(bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text))
    except Exception as e:
        print(f"[Telegram Error] {e}")

def is_bot_running() -> bool:
    return any(task for task in running_tasks if not task.done())

def track_task(task: asyncio.Task):
    running_tasks.append(task)
    task.add_done_callback(lambda t: running_tasks.remove(t))

def reset_tasks():
    for task in running_tasks:
        if not task.done():
            task.cancel()
    running_tasks.clear()

# === BUY/SELL ===
async def buy_token(input_mint: str, amount_sol: float):
    try:
        input_mint_pubkey = Pubkey.from_string(input_mint)
        sol_mint = Pubkey.from_string("So11111111111111111111111111111111111111112")

        amount = int(amount_sol * 1e9)  # convert SOL to lamports

        quote = await jupiter.get_quote(
            input_mint=sol_mint,
            output_mint=input_mint_pubkey,
            amount=amount,
            slippage_bps=100,
            user_public_key=wallet_pubkey
        )

        if not quote or "data" not in quote:
            send_telegram_alert("No valid quote returned.")
            return None

        swap_txn = await jupiter.get_swap_transaction(
            user_public_key=wallet_pubkey,
            quote=quote["data"],
            keypair=keypair
        )

        signature = await jupiter.send_transaction(swap_txn)
        send_telegram_alert(f"Buy transaction sent: {signature}")
        return signature

    except Exception as e:
        send_telegram_alert(f"Buy failed: {e}")
        return None

async def sell_token(input_mint: str, amount_token: int):
    try:
        input_mint_pubkey = Pubkey.from_string(input_mint)
        sol_mint = Pubkey.from_string("So11111111111111111111111111111111111111112")

        quote = await jupiter.get_quote(
            input_mint=input_mint_pubkey,
            output_mint=sol_mint,
            amount=amount_token,
            slippage_bps=100,
            user_public_key=wallet_pubkey
        )

        if not quote or "data" not in quote:
            send_telegram_alert("No valid sell quote returned.")
            return None

        swap_txn = await jupiter.get_swap_transaction(
            user_public_key=wallet_pubkey,
            quote=quote["data"],
            keypair=keypair
        )

        signature = await jupiter.send_transaction(swap_txn)
        send_telegram_alert(f"Sell transaction sent: {signature}")
        return signature

    except Exception as e:
        send_telegram_alert(f"Sell failed: {e}")
        return None

