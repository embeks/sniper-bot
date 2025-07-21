# =============================
# utils.py — Final (Real Buy/Sell, PnL, Partial Sells)
# =============================

import os
import json
import httpx
import asyncio
import csv
from jupiter_aggregator import JupiterAggregatorClient
from datetime import datetime
from dotenv import load_dotenv
from solana.rpc.api import Client
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.instruction import Instruction
from solders.transaction import Transaction
from solders.message import MessageV0
from solders.hash import Hash
from solders.signature import Signature
from solders.account_meta import AccountMeta
from solders.rpc.requests import GetLatestBlockhash

from telegram.ext import Application, CommandHandler

load_dotenv()

# 🔐 ENV + Wallet
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
RPC_URL = os.getenv("RPC_URL")
SOLANA_PRIVATE_KEY = json.loads(os.getenv("SOLANA_PRIVATE_KEY"))
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")
BUY_AMOUNT_SOL = float(os.getenv("BUY_AMOUNT_SOL", 0.03))

# 💰 Constants
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
BLACKLISTED_TOKENS = ["BADTOKEN1", "BADTOKEN2"]
SELL_MULTIPLIERS = [2, 5, 10]
SELL_TIMEOUT_SEC = int(os.getenv("SELL_TIMEOUT_SEC", 300))
RUG_LP_THRESHOLD = float(os.getenv("RUG_LP_THRESHOLD", 0.75))

# 💪 Wallet Setup
keypair = Keypair.from_bytes(bytes(SOLANA_PRIVATE_KEY))
wallet_pubkey = str(keypair.pubkey())
rpc = Client(RPC_URL)

# 📬 Telegram Alerts
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

# 📈 Token Price
async def get_token_price(token_mint):
    try:
        url = f"https://public-api.birdeye.so/public/price?address={token_mint}"
        headers = {"x-chain": "solana", "X-API-KEY": BIRDEYE_API_KEY}
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers=headers)
            return r.json().get("data", {}).get("value")
    except:
        return None

# 🔎 Token Safety Data
async def get_token_data(mint):
    try:
        url = f"https://public-api.birdeye.so/public/token/{mint}"
        headers = {"x-chain": "solana", "X-API-KEY": BIRDEYE_API_KEY}
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers=headers)
            d = r.json().get("data", {})
            return {
                "liquidity": d.get("liquidity", 0),
                "holders": d.get("holder_count", 0),
                "renounced": d.get("is_renounced", False),
                "lp_locked": d.get("is_lp_locked", False)
            }
    except:
        return {}

# 🔁 Buy Token
async def buy_token(mint: str):
    try:
        from solders.pubkey import Pubkey
        from jupiter_aggregator import JupiterAggregatorClient  # make sure it's installed
        jupiter = JupiterAggregatorClient(RPC_URL)

        input_mint = Pubkey.from_string("So11111111111111111111111111111111111111112")  # SOL
        output_mint = Pubkey.from_string(mint)

        quote = jupiter.get_quote(input_mint, output_mint, int(BUY_AMOUNT_SOL * 1e9))
        if not quote:
            await send_telegram_alert(f"❌ No quote found for {mint}")
            return False

        tx = jupiter.build_swap_transaction(
            quote["swapTransaction"],
            keypair,
        )

        sig = rpc.send_raw_transaction(tx)
        await send_telegram_alert(f"✅ Buy tx sent: https://solscan.io/tx/{sig}")
        log_trade(mint, "BUY", BUY_AMOUNT_SOL, 0)
        return True

    except Exception as e:
        await send_telegram_alert(f"❌ Buy failed for {mint}: {e}")
        return False
# 💸 Sell Token
async def sell_token(mint: str, percent: float = 100.0):
    try:
        from solders.pubkey import Pubkey
        from jupiter_aggregator import JupiterAggregatorClient
        jupiter = JupiterAggregatorClient(RPC_URL)

        input_mint = Pubkey.from_string(mint)
        output_mint = Pubkey.from_string("So11111111111111111111111111111111111111112")  # SOL

        token_balance = rpc.get_token_account_balance(wallet_pubkey, input_mint)
        amount = int((token_balance * percent / 100.0))

        quote = jupiter.get_quote(input_mint, output_mint, amount)
        if not quote:
            await send_telegram_alert(f"❌ No sell quote found for {mint}")
            return False

        tx = jupiter.build_swap_transaction(
            quote["swapTransaction"],
            keypair,
        )

        sig = rpc.send_raw_transaction(tx)
        await send_telegram_alert(f"✅ Sell {percent}% sent: https://solscan.io/tx/{sig}")
        log_trade(mint, f"SELL {percent}%", 0, amount / 1e9)
        return True

    except Exception as e:
        await send_telegram_alert(f"❌ Sell failed for {mint}: {e}")
        return False
# ⚠️ Volume Spike
async def is_volume_spike(mint, threshold=5.0):
    try:
        url = f"https://public-api.birdeye.so/public/token/{mint}/chart?time=1m"
        headers = {"x-chain": "solana", "X-API-KEY": BIRDEYE_API_KEY}
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers=headers)
            data = r.json().get("data", [])
            if len(data) < 2:
                return False
            open_vol = data[-2].get("volume", 1)
            close_vol = data[-1].get("volume", 1)
            return (close_vol / open_vol) >= threshold
    except:
        return False

# 🚀 Auto-Partial Selling
async def partial_sell(mint):
    for i, multiplier in enumerate(SELL_MULTIPLIERS):
        await asyncio.sleep(multiplier * 60)  # simulate wait
        await sell_token(mint, 0.5 if i == 0 else 0.25)

# 🧬 Main Sniping Logic
async def snipe_token(mint: str) -> bool:
    try:
        if mint in BLACKLISTED_TOKENS:
            await send_telegram_alert(f"🚫 Skipping blacklisted token: {mint}")
            return False

        if not os.path.exists("sniped_tokens.txt"):
            open("sniped_tokens.txt", "w").close()

        with open("sniped_tokens.txt", "r") as f:
            if mint in f.read():
                return False

        with open("sniped_tokens.txt", "a") as f:
            f.write(mint + "\n")

        token_data = await get_token_data(mint)
        if token_data["liquidity"] < 1000 or not token_data["lp_locked"]:
            await send_telegram_alert(f"🛑 Safety check failed for {mint}")
            return False

        await buy_token(mint)
        await asyncio.sleep(5)
        await partial_sell(mint)
        return True

    except Exception as e:
        await send_telegram_alert(f"[‼️] Snipe failed for {mint}: {e}")
        return False

# ✅ Is Valid Mint
def is_valid_mint(keys):
    for k in keys:
        if isinstance(k, dict):
            if k.get("pubkey") == TOKEN_PROGRAM_ID:
                return True
    return False

# =========================
# 🤖 Telegram Command Bot
# =========================

async def status(update, context):
    await update.message.reply_text(f"🟢 Bot is running.\nWallet: `{wallet_pubkey}`")

async def holdings(update, context):
    try:
        with open("sniped_tokens.txt", "r") as f:
            tokens = f.read().splitlines()
        reply = "📦 Current sniped tokens:\n" + "\n".join(tokens[-10:]) if tokens else "📦 No sniped tokens yet."
        await update.message.reply_text(reply)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")

async def logs(update, context):
    try:
        with open("trade_log.csv", "r") as f:
            lines = f.readlines()[-10:]
        await update.message.reply_text("📝 Last trades:\n" + "".join(lines) if lines else "📝 No trades logged yet.")
    except:
        await update.message.reply_text("📝 No logs found.")

async def wallet(update, context):
    await update.message.reply_text(f"💼 Wallet: `{wallet_pubkey}`")

async def reset(update, context):
    open("sniped_tokens.txt", "w").close()
    await update.message.reply_text("♻️ Sniped token list reset.")

async def start_command_bot():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("holdings", holdings))
    app.add_handler(CommandHandler("logs", logs))
    app.add_handler(CommandHandler("wallet", wallet))
    app.add_handler(CommandHandler("reset", reset))
    print("🤖 Telegram command bot ready.")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
