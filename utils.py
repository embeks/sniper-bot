# =============================
# utils.py — ELITE VERSION (Webhook-Only, No Polling, Full Telegram Control, Safety Filters)
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
from solana.rpc.types import TxOpts
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
    except:
        pass

def log_trade(token, action, sol_in, token_out):
    with open("trade_log.csv", "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([datetime.utcnow().isoformat(), token, action, sol_in, token_out])

def log_skipped_token(mint: str, reason: str):
    with open("skipped_tokens.csv", "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([datetime.utcnow().isoformat(), mint, reason])

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

    quote = await jupiter.get_quote(input_mint, output_mint, amount)
    if not quote or "swapTransaction" not in quote:
        await send_telegram_alert(f"⚠️ Jupiter quote failed for {mint}, trying Raydium fallback")
        quote = await jupiter.get_quote(input_mint, output_mint, amount, only_direct_routes=True)

    if not quote or "swapTransaction" not in quote:
        await send_telegram_alert(f"❌ No valid quote for {mint} (Jupiter & Raydium failed)")
        log_skipped_token(mint, "No valid quote")
        return False

    try:
        await approve_token_if_needed(mint)
        tx = jupiter.build_swap_transaction(quote["swapTransaction"], keypair)
        if not tx:
            raise Exception("Swap transaction build failed")

        sig = rpc.send_raw_transaction(tx)
        await send_telegram_alert(f"✅ Buy tx sent: https://solscan.io/tx/{sig}")
        log_trade(mint, "BUY", BUY_AMOUNT_SOL, 0)
        return True
    except Exception as e:
        await send_telegram_alert(f"❌ Buy failed for {mint}: {e}")
        log_skipped_token(mint, f"Buy failed: {e}")
        return False

async def sell_token(mint: str, percent: float = 100.0):
    input_mint = Pubkey.from_string(mint)
    output_mint = Pubkey.from_string("So11111111111111111111111111111111111111112")
    amount = int(BUY_AMOUNT_SOL * 1e9 * percent / 100)

    quote = await jupiter.get_quote(input_mint, output_mint, amount)
    if not quote or "swapTransaction" not in quote:
        quote = await jupiter.get_quote(input_mint, output_mint, amount, only_direct_routes=True)

    if not quote or "swapTransaction" not in quote:
        await send_telegram_alert(f"❌ No sell quote for {mint}")
        log_skipped_token(mint, "No sell quote")
        return False

    try:
        tx = jupiter.build_swap_transaction(quote["swapTransaction"], keypair)
        sig = rpc.send_raw_transaction(tx)
        await send_telegram_alert(f"✅ Sell {percent}% sent: https://solscan.io/tx/{sig}")
        log_trade(mint, f"SELL {percent}%", 0, quote.get("outAmount", 0) / 1e9)
        return True
    except Exception as e:
        await send_telegram_alert(f"❌ Sell failed for {mint}: {e}")
        log_skipped_token(mint, f"Sell failed: {e}")
        return False

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

TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
def is_valid_mint(keys):
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

async def get_liquidity_and_ownership(mint):
    try:
        url = f"https://api.geckoterminal.com/api/v2/networks/solana/tokens/{mint}"
        async with httpx.AsyncClient() as client:
            res = await client.get(url)
            if res.status_code != 200:
                return None
            attributes = res.json().get("data", {}).get("attributes", {})
            return {
                "liquidity": float(attributes.get("liquidity_usd", 0)),
                "renounced": attributes.get("ownership_renounced", False),
                "lp_locked": attributes.get("lp_honeycheck", {}).get("lp_locked", False)
            }
    except:
        return None

def get_wallet_status_message():
    return f"🟢 Bot is running: `{is_bot_running()}`\nWallet: `{wallet_pubkey}`"
