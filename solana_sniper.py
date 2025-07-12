# solana_sniper.py
import os
import json
import base64
import asyncio
import httpx
from solana.rpc.api import Client
from solana.keypair import Keypair
from solana.rpc.types import TxOpts
from solana.transaction import Transaction
from solders.transaction import VersionedTransaction
from dotenv import load_dotenv
from utils import send_telegram_alert, log_trade_to_csv

load_dotenv()

# 🔐 Load wallet
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
SOLANA_PRIVATE_KEY = json.loads(os.getenv("SOLANA_PRIVATE_KEY"))
client = Client(SOLANA_RPC)
keypair = Keypair.from_secret_key(bytes(SOLANA_PRIVATE_KEY))
wallet_address = str(keypair.public_key)

JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL = "https://quote-api.jup.ag/v6/swap"

# ✅ Get a quote from Jupiter
async def get_jupiter_quote(output_mint: str, amount_sol: float, slippage: float = 1.0):
    lamports = int(amount_sol * 1_000_000_000)
    params = {
        "inputMint": "So11111111111111111111111111111111111111112",  # SOL
        "outputMint": output_mint,
        "amount": lamports,
        "slippage": slippage
    }
    async with httpx.AsyncClient() as session:
        res = await session.get(JUPITER_QUOTE_URL, params=params)
        data = res.json()
        routes = data.get("data", [])
        return routes[0] if routes else None

# 🧠 Build swap transaction
async def build_jupiter_swap_tx(route):
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

# 🚀 Sign and send transaction
def sign_and_send_tx(raw_tx: bytes):
    try:
        tx = VersionedTransaction.deserialize(raw_tx)
        tx.sign([keypair])
        signature = client.send_raw_transaction(tx.serialize(), opts=TxOpts(skip_preflight=True))
        print(f"[+] TX sent: {signature['result']}")
        return signature['result']
    except Exception as e:
        print(f"[‼️] TX Error: {e}")
        return False

# 🪙 Buy token with SOL
async def buy_token(token_address: str, amount_sol: float = 0.01):
    try:
        route = await get_jupiter_quote(token_address, amount_sol)
        if not route:
            await send_telegram_alert(f"❌ No Jupiter route found for token {token_address}")
            return

        if route['outAmount'] < 1:
            await send_telegram_alert(f"❌ Output too low for token {token_address}, skipping")
            return

        raw_tx = await build_jupiter_swap_tx(route)
        if not raw_tx:
            await send_telegram_alert(f"❌ Could not build transaction for token {token_address}")
            return

        signature = sign_and_send_tx(raw_tx)
        if signature:
            await send_telegram_alert(f"✅ Buy TX sent: {signature}\nToken: {token_address}")
            log_trade_to_csv(token_address, "buy", amount_sol, route['outAmount'] / 1e9)
        else:
            await send_telegram_alert(f"‼️ Failed to send buy TX for {token_address}")

    except Exception as e:
        print(f"[!] Sniping failed: {e}")
        await send_telegram_alert(f"[!] Sniping failed: {e}")
