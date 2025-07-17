import asyncio
import os
import json
import base64
import time
import httpx
from dotenv import load_dotenv

from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solana.rpc.api import Client
from solana.rpc.types import TxOpts

from utils import (
    send_telegram_alert, log_trade_to_csv,
    is_blacklisted, is_lp_locked_or_burned,
    check_token_safety, has_blacklist_or_mint_functions,
    buy_on_raydium, get_rpc_client,
    get_token_price, detect_rug_conditions
)
from mempool_listener import mempool_listener_jupiter, mempool_listener_raydium

# ============================== 🔧 Config ==============================
load_dotenv()

SOLANA_RPC = os.getenv("RPC_URL")
SOLANA_PRIVATE_KEY = json.loads(os.getenv("SOLANA_PRIVATE_KEY"))
BUY_AMOUNT_SOL = float(os.getenv("BUY_AMOUNT_SOL", "0.03"))

JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL = "https://quote-api.jup.ag/v6/swap"
JUPITER_TOKEN_LIST_URL = "https://cache.jup.ag/tokens"

client = Client(SOLANA_RPC)
keypair = Keypair.from_bytes(bytes(SOLANA_PRIVATE_KEY))
wallet_address = str(keypair.pubkey())

# ============================== 📦 Memory ==============================
open_positions = {}  # token_address -> {"buy_price": float, "buy_time": time.time()}

# ============================== 🔄 Buy Logic ==============================

async def is_token_supported_by_jupiter(mint: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=10) as session:
            res = await session.get(JUPITER_TOKEN_LIST_URL)
            tokens = res.json()
            return any(token["address"] == mint for token in tokens)
    except Exception as e:
        print(f"[!] Jupiter token list fetch error: {e}")
        return False

async def get_jupiter_quote(output_mint: str, amount_sol: float, slippage: float = 5.0):
    try:
        lamports = int(amount_sol * 1_000_000_000)
        params = {
            "inputMint": "So11111111111111111111111111111111111111112",
            "outputMint": output_mint,
            "amount": lamports,
            "slippageBps": int(slippage * 100)
        }
        async with httpx.AsyncClient(timeout=10) as session:
            res = await session.get(JUPITER_QUOTE_URL, params=params)
            data = res.json()
            return data.get("data", [None])[0]
    except Exception as e:
        print(f"[!] Jupiter quote error: {e}")
        return None

async def build_jupiter_swap_tx(route):
    try:
        payload = {
            "route": route,
            "userPublicKey": wallet_address,
            "wrapUnwrapSOL": True,
            "feeAccount": None,
            "computeUnitPriceMicroLamports": 5000
        }
        async with httpx.AsyncClient(timeout=10) as session:
            res = await session.post(JUPITER_SWAP_URL, json=payload)
            data = res.json()
            tx_data = data.get("swapTransaction")
            return base64.b64decode(tx_data) if tx_data else None
    except Exception as e:
        print(f"[!] Build TX error: {e}")
        return None

def sign_and_send_tx(raw_tx: bytes):
    try:
        tx = VersionedTransaction.deserialize(raw_tx)
        tx.sign([keypair])
        sig = client.send_raw_transaction(tx.serialize(), opts=TxOpts(skip_preflight=True))
        return sig.get('result')
    except Exception as e:
        print(f"[‼️] TX signing error: {e}")
        return None

def confirm_tx(signature: str, max_wait: int = 20):
    for _ in range(max_wait):
        res = client.get_confirmed_transaction(signature)
        if res.value:
            return True
        asyncio.run(asyncio.sleep(1))
    return False

async def get_sol_balance():
    try:
        balance = client.get_balance(keypair.pubkey()).value
        return balance / 1e9
    except Exception as e:
        print(f"[!] Failed to fetch SOL balance: {e}")
        return 0

async def buy_token(token_address: str, amount_sol: float = BUY_AMOUNT_SOL):
    try:
        balance = await get_sol_balance()
        if balance < amount_sol:
            await send_telegram_alert(f"❌ Not enough SOL to snipe. Balance: {balance:.2f} SOL")
            return

        await send_telegram_alert(f"🟡 Trying to snipe {token_address} with {amount_sol} SOL")

        if await is_blacklisted(token_address):
            await send_telegram_alert(f"❌ Blacklisted: {token_address}")
            return

        if not await is_lp_locked_or_burned(token_address):
            await send_telegram_alert(f"❌ LP not locked or burned: {token_address}")
            return

        if await has_blacklist_or_mint_functions(token_address):
            await send_telegram_alert(f"⚠️ Token has blacklist/mint authority")
            return

        safety = await check_token_safety(token_address)
        await send_telegram_alert(f"🛡️ Safety: {safety}")
        if "❌" in safety:
            return

        supported = await is_token_supported_by_jupiter(token_address)
        if not supported:
            await send_telegram_alert(f"❌ Not supported by Jupiter")
            return

        route = await get_jupiter_quote(token_address, amount_sol)
        if not route:
            await send_telegram_alert(f"❌ No route — fallback to Raydium")
            rpc = get_rpc_client(use_triton=True)
            await buy_on_raydium(rpc, keypair, token_address, amount_sol)
            return

        if route.get('outAmount', 0) < 1:
            await send_telegram_alert(f"❌ OutAmount too low")
            return

        raw_tx = await build_jupiter_swap_tx(route)
        if not raw_tx:
            await send_telegram_alert(f"❌ TX build failed — fallback to Raydium")
            rpc = get_rpc_client(use_triton=True)
            await buy_on_raydium(rpc, keypair, token_address, amount_sol)
            return

        signature = sign_and_send_tx(raw_tx)
        if signature:
            await send_telegram_alert(f"✅ TX sent: https://solscan.io/tx/{signature}")
            confirmed = confirm_tx(signature)
            if confirmed:
                price = await get_token_price(token_address)
                await send_telegram_alert(f"✅ Buy confirmed. Price: {price:.9f} SOL")
                open_positions[token_address] = {"buy_price": price, "buy_time": time.time()}
                log_trade_to_csv(token_address, "buy", amount_sol, price)
            else:
                await send_telegram_alert(f"⚠️ TX not confirmed")
        else:
            await send_telegram_alert(f"‼️ TX failed")
    except Exception as e:
        print(f"[!] Sniping failed: {e}")
        await send_telegram_alert(f"[!] Sniping error: {e}")

# ============================== 💰 Sell Logic ==============================

async def sell_token(token_address: str, amount_token: int = 0):
    try:
        price = await get_token_price(token_address)
        entry = open_positions.get(token_address)

        if not entry:
            await send_telegram_alert(f"❌ No entry found for {token_address}")
            return

        buy_price = entry["buy_price"]
        buy_time = entry["buy_time"]
        profit = price / buy_price if buy_price > 0 else 0
        age = time.time() - buy_time

        if detect_rug_conditions(await get_token_data(token_address)):
            await send_telegram_alert(f"🚨 Rug detected on {token_address}! Selling...")
        elif profit >= 10:
            await send_telegram_alert(f"💰 10x profit! Selling {token_address}")
        elif profit >= 5:
            await send_telegram_alert(f"💸 5x profit. Selling {token_address}")
        elif profit >= 2:
            await send_telegram_alert(f"📈 2x reached. Selling {token_address}")
        elif age > 300:
            await send_telegram_alert(f"⌛ Timeout hit. Selling {token_address}")
        else:
            return  # Not ready to sell yet

        # Placeholder: implement actual sell TX
        log_trade_to_csv(token_address, "sell", amount_token, price)
        await send_telegram_alert(f"✅ Sold {token_address} at {price:.9f} SOL")

    except Exception as e:
        print(f"[!] Sell error: {e}")
        await send_telegram_alert(f"[!] Sell error: {e}")

# ============================== 🚀 Start ==============================

async def start_sniper():
    await send_telegram_alert("✅ Starting sniper bot with dual sockets (Jupiter + Raydium)...")
    await asyncio.gather(
        mempool_listener_jupiter(),
        mempool_listener_raydium()
    )
