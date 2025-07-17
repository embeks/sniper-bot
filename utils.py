# =========================
# utils.py
# =========================
import os
import json
import time
import httpx
from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.api import Client
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TokenAccountOpts

load_dotenv()

# 🔧 Environment Variables
SOLANA_PRIVATE_KEY = json.loads(os.getenv("SOLANA_PRIVATE_KEY"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")
RPC_URL = os.getenv("RPC_URL")
RPC_URL_TRITON = os.getenv("RPC_URL_TRITON")

# 🔐 Wallet Setup
keypair = Keypair.from_bytes(bytes(SOLANA_PRIVATE_KEY))
WALLET_PUBKEY = keypair.pubkey()
WALLET_ADDRESS = str(WALLET_PUBKEY)
client = Client(RPC_URL)

# ✅ Async RPC Client Getter
def get_rpc_client(use_triton=False) -> AsyncClient:
    return AsyncClient(RPC_URL_TRITON if use_triton else RPC_URL)

# 📤 Telegram Alerts
async def send_telegram_alert(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[⚠️] Telegram not configured")
        return
    try:
        async with httpx.AsyncClient() as session:
            await session.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": message}
            )
    except Exception as e:
        print(f"[‼️] Telegram alert failed: {e}")

# 🧪 Token Safety Filter
async def check_token_safety(token_address):
    try:
        async with httpx.AsyncClient() as session:
            url = f"https://public-api.birdeye.so/public/token/{token_address}/info"
            headers = {"X-API-KEY": BIRDEYE_API_KEY}
            res = await session.get(url, headers=headers)
            data = res.json().get("data", {})

            liquidity = data.get("liquidity", 0)
            buy_tax = data.get("buyTax", 0)
            sell_tax = data.get("sellTax", 0)
            holders = data.get("holders", 0)

            if liquidity < 10000:
                return "❌ Rug Risk: Low Liquidity"
            if buy_tax > 15 or sell_tax > 15:
                return f"⚠️ Honeypot Risk: High Tax ({buy_tax}% / {sell_tax}%)"
            if holders < 20:
                return "⚠️ Low Holders"

            return "✅ Passed basic safety"
    except Exception as e:
        return f"[!] Safety check error: {e}"

# 🧠 Token Meta
async def get_token_data(token_address):
    try:
        async with httpx.AsyncClient() as session:
            url = f"https://public-api.birdeye.so/public/token/{token_address}/info"
            headers = {"X-API-KEY": BIRDEYE_API_KEY}
            res = await session.get(url, headers=headers)
            return res.json().get("data", {})
    except Exception as e:
        print(f"[!] Token data fetch failed: {e}")
        return {}

# 📈 Token Price
async def get_token_price(token_address):
    try:
        async with httpx.AsyncClient() as session:
            url = f"https://public-api.birdeye.so/defi/price?address={token_address}"
            headers = {"X-API-KEY": BIRDEYE_API_KEY}
            res = await session.get(url, headers=headers)
            data = res.json().get("data", {})
            return float(data.get("value", 0))
    except Exception as e:
        print(f"[!] Price fetch failed: {e}")
        return None

# 🚫 Blacklist, Authority, LP Lock Checks
async def is_blacklisted(token_address):
    BLACKLISTED_CREATORS = {"Fg6PaFpoGXkYsidMpWxTWqYw84fi5GZzvynV2GF3u4gN"}
    BLACKLISTED_WALLETS = {"So11111111111111111111111111111111111111112"}
    try:
        data = await get_token_data(token_address)
        creator = data.get("creatorAddress", "")
        owner = data.get("ownerAddress", "")
        return creator in BLACKLISTED_CREATORS or owner in BLACKLISTED_WALLETS
    except Exception as e:
        print(f"[!] Blacklist check failed: {e}")
        return True

async def has_blacklist_or_mint_functions(token_address):
    try:
        data = await get_token_data(token_address)
        return data.get("hasBlacklist", False) or data.get("hasMintAuthority", True)
    except Exception as e:
        print(f"[!] Authority check error: {e}")
        return True

async def is_lp_locked_or_burned(token_address):
    try:
        data = await get_token_data(token_address)
        return data.get("lpIsBurned", False) or data.get("lpLocked", False)
    except Exception as e:
        print(f"[!] LP lock check failed: {e}")
        return False

# 🐋 Whale Protection
async def has_whales(token_address):
    try:
        async with httpx.AsyncClient() as session:
            url = f"https://public-api.birdeye.so/public/token/{token_address}/holders"
            headers = {"X-API-KEY": BIRDEYE_API_KEY}
            res = await session.get(url, headers=headers)
            holders = res.json().get("data", [])
            for h in holders:
                pct = float(h.get("percent", 0))
                if pct > 30:
                    return True
            return False
    except Exception as e:
        print(f"[!] Whale check failed: {e}")
        return True

# 🧾 Token Balance
async def get_token_balance(token_mint):
    try:
        opts = TokenAccountOpts(
            mint=token_mint,
            program_id="TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
        )
        accounts = client.get_token_accounts_by_owner(WALLET_PUBKEY, opts)
        results = accounts.get("result", {}).get("value", [])
        for acc in results:
            amount = int(acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
            return amount
        return 0
    except Exception as e:
        print(f"[!] Balance fetch failed: {e}")
        return 0

# ⚠️ Rug Detection
def detect_rug_conditions(token_data):
    try:
        return (
            token_data.get("liquidity", 0) < 1000 or
            token_data.get("volume24h", 0) < 100 or
            token_data.get("sellTax", 0) > 25
        )
    except Exception as e:
        print(f"[!] Rug detection error: {e}")
        return False

# 📉 Trade Log Writer
def log_trade_to_csv(token_address, action, amount, price):
    try:
        with open("trade_log.csv", "a") as f:
            f.write(f"{time.time()},{token_address},{action},{amount},{price}\n")
    except Exception as e:
        print(f"[‼️] CSV log error: {e}")

# 🧪 Raydium Fallback Stub
async def buy_on_raydium(client: AsyncClient, wallet, mint_address: str, amount: float):
    print(f"[⚡] Direct Raydium buy: {mint_address} with {amount} SOL")
    return True
