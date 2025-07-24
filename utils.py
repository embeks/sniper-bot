# =============================
# utils.py — Elite Tools + Trending Feed + Pre-Approve + Auto-Sell + LP Check
# =============================

import os
import json
import httpx
import asyncio
import csv
from datetime import datetime
from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.api import Client
from solana.transaction import Transaction
from solana.system_program import TransferParams, transfer
from solana.rpc.types import TxOpts
from spl.token.instructions import approve, get_associated_token_address
from telegram.ext import Application, CommandHandler
from jupiter_aggregator import JupiterAggregatorClient

load_dotenv()

# 🔐 ENV + Wallet
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
RPC_URL = os.getenv("RPC_URL")
SOLANA_PRIVATE_KEY = json.loads(os.getenv("SOLANA_PRIVATE_KEY"))
BUY_AMOUNT_SOL = float(os.getenv("BUY_AMOUNT_SOL", 0.03))
SELL_TIMEOUT_SEC = int(os.getenv("SELL_TIMEOUT_SEC", 300))
RUG_LP_THRESHOLD = float(os.getenv("RUG_LP_THRESHOLD", 0.5))

# 💪 Wallet Setup
keypair = Keypair.from_bytes(bytes(SOLANA_PRIVATE_KEY))
wallet_pubkey = str(keypair.pubkey())
rpc = Client(RPC_URL)
jupiter = JupiterAggregatorClient(RPC_URL)

# 📩 Telegram Alerts
async def send_telegram_alert(message: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        async with httpx.AsyncClient() as client:
            await client.post(url, json=payload)
    except:
        pass

# 📊 Trade Logger
def log_trade(token, action, sol_in, token_out):
    with open("trade_log.csv", "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([datetime.utcnow().isoformat(), token, action, sol_in, token_out])

# ⚠️ Skipped Token Logger
def log_skipped_token(mint: str, reason: str):
    with open("skipped_tokens.csv", "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([datetime.utcnow().isoformat(), mint, reason])

# 🔁 Buy Token
async def buy_token(mint: str):
    try:
        input_mint = Pubkey.from_string("So11111111111111111111111111111111111111112")
        output_mint = Pubkey.from_string(mint)

        quote = await jupiter.get_quote(input_mint, output_mint, int(BUY_AMOUNT_SOL * 1e9))
        if not quote or "swapTransaction" not in quote:
            await send_telegram_alert(f"❌ No quote found for {mint}")
            log_skipped_token(mint, "No Jupiter quote")
            return False

        await approve_token_if_needed(mint)

        tx = jupiter.build_swap_transaction(quote["swapTransaction"], keypair)
        sig = rpc.send_raw_transaction(tx)
        await send_telegram_alert(f"✅ Buy tx sent: https://solscan.io/tx/{sig}")
        log_trade(mint, "BUY", BUY_AMOUNT_SOL, 0)
        return True

    except Exception as e:
        await send_telegram_alert(f"❌ Buy failed for {mint}: {e}")
        log_skipped_token(mint, f"Buy failed: {e}")
        return False

# 💸 Sell Token
async def sell_token(mint: str, percent: float = 100.0):
    try:
        input_mint = Pubkey.from_string(mint)
        output_mint = Pubkey.from_string("So11111111111111111111111111111111111111112")

        quote = await jupiter.get_quote(input_mint, output_mint, int(BUY_AMOUNT_SOL * 1e9 * percent / 100))
        if not quote or "swapTransaction" not in quote:
            await send_telegram_alert(f"❌ No sell quote found for {mint}")
            return False

        tx = jupiter.build_swap_transaction(quote["swapTransaction"], keypair)
        sig = rpc.send_raw_transaction(tx)
        await send_telegram_alert(f"✅ Sell {percent}% sent: https://solscan.io/tx/{sig}")
        log_trade(mint, f"SELL {percent}%", 0, quote.get("outAmount", 0) / 1e9)
        return True

    except Exception as e:
        await send_telegram_alert(f"❌ Sell failed for {mint}: {e}")
        return False

# 🔁 Auto-Sell Logic
async def wait_and_auto_sell(mint):
    try:
        await asyncio.sleep(1)
        await sell_token(mint, percent=50)
        await asyncio.sleep(2)
        await sell_token(mint, percent=25)
        await asyncio.sleep(2)
        await sell_token(mint, percent=25)
    except Exception as e:
        await send_telegram_alert(f"❌ Auto-sell error for {mint}: {e}")

# ✅ Is Valid Mint
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
def is_valid_mint(keys):
    for k in keys:
        if isinstance(k, dict):
            if k.get("pubkey") == TOKEN_PROGRAM_ID:
                return True
    return False

# ✅ Trending Scanner (DEXScreener)
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

# ✅ Pre-Approval Batching
async def approve_token_if_needed(mint):
    try:
        mint_pubkey = Pubkey.from_string(mint)
        ata = get_associated_token_address(keypair.pubkey(), mint_pubkey)
        tx = Transaction().add(approve(
            program_id=Pubkey.from_string(TOKEN_PROGRAM_ID),
            source=ata,
            delegate=keypair.pubkey(),
            owner=keypair.pubkey(),
            amount=9999999999
        ))
        rpc.send_transaction(tx, keypair, opts=TxOpts(skip_confirmation=True))
    except:
        pass

# ✅ Liquidity + Ownership Check
async def get_liquidity_and_ownership(mint):
    try:
        url = f"https://api.geckoterminal.com/api/v2/networks/solana/tokens/{mint}"
        async with httpx.AsyncClient() as client:
            res = await client.get(url)
            if res.status_code != 200:
                return None
            data = res.json()
            attributes = data.get("data", {}).get("attributes", {})
            liquidity = float(attributes.get("liquidity_usd", 0))
            renounced = attributes.get("ownership_renounced", False)
            lp_locked = attributes.get("lp_honeycheck", {}).get("lp_locked", False)
            return {"liquidity": liquidity, "renounced": renounced, "lp_locked": lp_locked}
    except:
        return None

# 🤖 Telegram Bot
async def status(update, context):
    await update.message.reply_text(f"🟢 Bot is running.\nWallet: `{wallet_pubkey}`")

async def wallet(update, context):
    await update.message.reply_text(f"💼 Wallet: `{wallet_pubkey}`")

async def reset(update, context):
    open("sniped_tokens.txt", "w").close()
    await update.message.reply_text("♻️ Sniped token list reset.")

async def start_command_bot():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("wallet", wallet))
    app.add_handler(CommandHandler("reset", reset))
    print("🤖 Telegram command bot ready.")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
