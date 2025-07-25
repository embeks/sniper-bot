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

# ENV CONFIG
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

# TELEGRAM
async def send_telegram_alert(message: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        async with httpx.AsyncClient() as client:
            await client.post(url, json=payload)
    except Exception:
        pass

# TRADE LOGGING
def log_trade(token, action, sol_in, token_out):
    with open("trade_log.csv", "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([datetime.utcnow().isoformat(), token, action, sol_in, token_out])

def log_skipped_token(mint: str, reason: str):
    with open("skipped_tokens.csv", "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([datetime.utcnow().isoformat(), mint, reason])

# ✅ DEBUGGING RAYDIUM LP CHECK
async def get_liquidity_and_ownership(mint: str):
    try:
        # Raydium pool address logic (simple PDA derivation placeholder for now)
        pool_addr = Pubkey.find_program_address([b"amm", bytes(Pubkey.from_string(mint))], Pubkey.from_string("RVKd61ztZW9GdKzGwcz74hzw1Fz5sGg5E7gKD9cTAiX"))[0]
        print(f"[LP CHECK] Derived Raydium pool address: {pool_addr}")

        # Fetch raw account data
        resp = rpc.get_account_info(pool_addr)
        if not resp or not resp.get("result") or not resp["result"].get("value"):
            await send_telegram_alert(f"❌ LP pool not found for {mint} — {pool_addr}")
            return None

        raw_data = resp["result"]["value"]["data"][0]  # base64
        print(f"[LP RAW] {raw_data[:40]}...")  # print first 40 chars

        # Dummy LP for now — just confirms call works
        return {
            "liquidity": -1,  # intentionally obvious dummy
            "renounced": False,
            "lp_locked": False
        }
    except Exception as e:
        await send_telegram_alert(f"⚠️ get_liquidity_and_ownership error: {e}")
        return None

# APPROVE
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

# BUY
async def buy_token(mint: str):
    input_mint = Pubkey.from_string("So11111111111111111111111111111111111111112")
    output_mint = Pubkey.from_string(mint)
    amount = int(BUY_AMOUNT_SOL * 1e9)

    try:
        route = await jupiter.get_quote(input_mint, output_mint, amount)
        if not route:
            await send_telegram_alert(f"⚠️ Jupiter quote failed for {mint}, trying Raydium fallback")
            route = await jupiter.get_quote(input_mint, output_mint, amount, only_direct_routes=True)

        if not route:
            await send_telegram_alert(f"❌ No valid quote for {mint} (Jupiter & Raydium failed)")
            log_skipped_token(mint, "No valid quote")
            return False

        swap_tx_base64 = await jupiter.get_swap_transaction(route)
        if not swap_tx_base64:
            await send_telegram_alert(f"❌ Failed to fetch swap transaction for {mint}")
            log_skipped_token(mint, "Swap fetch failed")
            return False

        await approve_token_if_needed(mint)
        tx = jupiter.build_swap_transaction(swap_tx_base64, keypair)
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

# SELL
async def sell_token(mint: str, percent: float = 100.0):
    input_mint = Pubkey.from_string(mint)
    output_mint = Pubkey.from_string("So11111111111111111111111111111111111111112")
    amount = int(BUY_AMOUNT_SOL * 1e9 * percent / 100)

    try:
        route = await jupiter.get_quote(input_mint, output_mint, amount)
        if not route:
            route = await jupiter.get_quote(input_mint, output_mint, amount, only_direct_routes=True)

        if not route:
            await send_telegram_alert(f"❌ No sell quote for {mint}")
            log_skipped_token(mint, "No sell quote")
            return False

        swap_tx_base64 = await jupiter.get_swap_transaction(route)
        if not swap_tx_base64:
            await send_telegram_alert(f"❌ Sell swap fetch failed for {mint}")
            log_skipped_token(mint, "Sell swap fetch failed")
            return False

        tx = jupiter.build_swap_transaction(swap_tx_base64, keypair)
        sig = rpc.send_raw_transaction(tx)
        await send_telegram_alert(f"✅ Sell {percent}% sent: https://solscan.io/tx/{sig}")
        log_trade(mint, f"SELL {percent}%", 0, route.get("outAmount", 0) / 1e9)
        return True
    except Exception as e:
        await send_telegram_alert(f"❌ Sell failed for {mint}: {e}")
        log_skipped_token(mint, f"Sell failed: {e}")
        return False

# AUTO SELL
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

# MINT CHECK
def is_valid_mint(keys):
    TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
    return any(k.get("pubkey") == TOKEN_PROGRAM_ID for k in keys if isinstance(k, dict))

# TRENDING
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

# TELEGRAM TEXT
def get_wallet_status_message():
    return f"🟢 Bot is running: `{is_bot_running()}`\nWallet: `{wallet_pubkey}`"

def get_wallet_summary():
    return f"💼 Wallet: `{wallet_pubkey}`"
