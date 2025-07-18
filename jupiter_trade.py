# =========================
# jupiter_trade.py (Final Upgraded with Raydium Fallback)
# =========================

import os
import json
import base64
import httpx
import asyncio
from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solana.rpc.api import Client
from solana.rpc.types import TxOpts

from utils import (
    send_telegram_alert,
    log_trade_to_csv,
    get_rpc_client,
    buy_on_raydium
)

# 🔐 Load environment
load_dotenv()
SOLANA_RPC = os.getenv("RPC_URL")
SOLANA_PRIVATE_KEY = json.loads(os.getenv("SOLANA_PRIVATE_KEY"))

# 🔧 Setup
client = Client(SOLANA_RPC)
keypair = Keypair.from_bytes(bytes(SOLANA_PRIVATE_KEY))
wallet_address = str(keypair.pubkey())

# 🌐 Jupiter Endpoints
JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL = "https://quote-api.jup.ag/v6/swap"
JUPITER_TOKEN_LIST_URL = "https://cache.jup.ag/tokens"

# ✅ Check if token is supported by Jupiter
async def is_token_supported_by_jupiter(mint: str) -> bool:
    try:
        async with httpx.AsyncClient() as session:
            res = await session.get(JUPITER_TOKEN_LIST_URL)
            tokens = res.json()
            return any(token["address"] == mint for token in tokens)
    except Exception as e:
        print(f"[!] Jupiter token list fetch error: {e}")
        return False

# ✅ Get best route quote from Jupiter
async def get_jupiter_quote(output_mint: str, amount_sol: float, slippage: float = 5.0):
    try:
        lamports = int(amount_sol * 1_000_000_000)
        params = {
            "inputMint": "So11111111111111111111111111111111111111112",
            "outputMint": output_mint,
            "amount": lamports,
            "slippageBps": int(slippage * 100)
        }
        async with httpx.AsyncClient() as session:
            res = await session.get(JUPITER_QUOTE_URL, params=params)
            data = res.json()
            return data.get("data", [None])[0]
    except Exception as e:
        print(f"[!] Jupiter quote error: {e}")
        return None

# 🧠 Build swap transaction
async def build_jupiter_swap_tx(route):
    try:
        payload = {
            "route": route,
            "userPublicKey": wallet_address,
            "wrapUnwrapSOL": True,
            "feeAccount": None,
            "computeUnitPriceMicroLamports": 5000
        }
        async with httpx.AsyncClient() as session:
            res = await session.post(JUPITER_SWAP_URL, json=payload)
            tx_data = res.json().get("swapTransaction")
            return base64.b64decode(tx_data) if tx_data else None
    except Exception as e:
        print(f"[!] Build TX error: {e}")
        return None

# 🚀 Sign and send transaction
def sign_and_send_tx(raw_tx: bytes):
    try:
        tx = VersionedTransaction.deserialize(raw_tx)
        tx.sign([keypair])
        signature = client.send_raw_transaction(tx.serialize(), opts=TxOpts(skip_preflight=True))
        return signature.get('result')
    except Exception as e:
        print(f"[‼️] TX signing error: {e}")
        return None

# 🪙 Buy token with SOL (with Raydium Fallback)
async def buy_token(token_address: str, amount_sol: float = 0.03):
    try:
        await send_telegram_alert(f"🟡 Trying to snipe {token_address} with {amount_sol} SOL")

        await send_telegram_alert("🔍 Step 1: Checking if token is supported by Jupiter...")
        supported = await is_token_supported_by_jupiter(token_address)
        if not supported:
            await send_telegram_alert(f"❌ Token {token_address} not supported by Jupiter. Trying Raydium fallback...")
            client_alt = await get_rpc_client(use_triton=True)
            success = await buy_on_raydium(client_alt, keypair, token_address, amount_sol)
            if success:
                await send_telegram_alert(f"✅ Raydium fallback buy succeeded for {token_address}")
                log_trade_to_csv(token_address, "buy-raydium", amount_sol, 0)
            else:
                await send_telegram_alert(f"❌ Raydium fallback failed for {token_address}")
            return

        await asyncio.sleep(0.2)
        await send_telegram_alert("🔍 Step 2: Fetching Jupiter route quote...")
        route = await get_jupiter_quote(token_address, amount_sol)
        if not route:
            await send_telegram_alert(f"❌ No Jupiter route found for {token_address}. Trying Raydium fallback...")
            client_alt = await get_rpc_client(use_triton=True)
            success = await buy_on_raydium(client_alt, keypair, token_address, amount_sol)
            if success:
                await send_telegram_alert(f"✅ Raydium fallback buy succeeded for {token_address}")
                log_trade_to_csv(token_address, "buy-raydium", amount_sol, 0)
            else:
                await send_telegram_alert(f"❌ Raydium fallback failed for {token_address}")
            return

        if route.get('outAmount', 0) < 1:
            await send_telegram_alert(f"❌ Output too low for {token_address}, skipping")
            return

        await asyncio.sleep(0.2)
        await send_telegram_alert("🔍 Step 3: Building transaction...")
        raw_tx = await build_jupiter_swap_tx(route)
        if not raw_tx:
            await send_telegram_alert(f"❌ Could not build transaction for {token_address}")
            return

        await asyncio.sleep(0.2)
        await send_telegram_alert("🚀 Step 4: Sending transaction to blockchain...")
        signature = sign_and_send_tx(raw_tx)
        if signature:
            await send_telegram_alert(f"✅ Buy TX sent for {token_address}\n🔗 https://solscan.io/tx/{signature}")
            log_trade_to_csv(token_address, "buy", amount_sol, route['outAmount'] / 1e9)
        else:
            await send_telegram_alert(f"‼️ TX failed for {token_address}")

    except Exception as e:
        print(f"[!] Live buy failed: {e}")
        await send_telegram_alert(f"[!] Buy error: {e}")

# 💰 Sell token for SOL
async def sell_token(token_address: str, amount_token: int):
    try:
        await send_telegram_alert(f"💸 Attempting to sell {amount_token} of {token_address}")

        route = await get_jupiter_quote(
            output_mint="So11111111111111111111111111111111111111112",
            amount_sol=amount_token / 1e9,
            slippage=5.0
        )
        if not route:
            await send_telegram_alert(f"❌ No sell route found for {token_address}")
            return

        await asyncio.sleep(0.2)
        raw_tx = await build_jupiter_swap_tx(route)
        if not raw_tx:
            await send_telegram_alert(f"❌ Failed to build sell TX for {token_address}")
            return

        signature = sign_and_send_tx(raw_tx)
        if signature:
            await send_telegram_alert(f"✅ Sell TX sent for {token_address}\n🔗 https://solscan.io/tx/{signature}")
            log_trade_to_csv(token_address, "sell", amount_token / 1e9, route['outAmount'] / 1e9)
        else:
            await send_telegram_alert(f"‼️ Sell TX failed for {token_address}")

    except Exception as e:
        await send_telegram_alert(f"[‼️] Sell error for {token_address}: {e}")
        print(f"[‼️] Sell error for {token_address}: {e}")
