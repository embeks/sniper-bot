import os
import json
import httpx
from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.api import Client
from solana.rpc.types import TokenAccountOpts

load_dotenv()

# 🔧 Environment + Config
SOLANA_PRIVATE_KEY = json.loads(os.getenv("SOLANA_PRIVATE_KEY"))
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")

# 🔐 Wallet
keypair = Keypair.from_bytes(bytes(SOLANA_PRIVATE_KEY))
WALLET_ADDRESS = str(keypair.pubkey())
client = Client(SOLANA_RPC)

# 📤 Send Telegram Alert (Async)
async def send_telegram_alert(message):
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

# 🔍 Honeypot Filter
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

# 🚫 Blacklist or Mint Authority Present
async def has_blacklist_or_mint_functions(token_address):
    try:
        async with httpx.AsyncClient() as session:
            url = f"https://public-api.birdeye.so/public/token/{token_address}/info"
            headers = {"X-API-KEY": BIRDEYE_API_KEY}
            res = await session.get(url, headers=headers)
            data = res.json().get("data", {})
            return data.get("hasBlacklist", False) or data.get("hasMintAuthority", True)
    except Exception:
        return True  # Assume dangerous if failed

# 🔒 LP Locked or Burned
async def is_lp_locked_or_burned(token_address):
    try:
        async with httpx.AsyncClient() as session:
            url = f"https://public-api.birdeye.so/public/token/{token_address}/info"
            headers = {"X-API-KEY": BIRDEYE_API_KEY}
            res = await session.get(url, headers=headers)
            data = res.json().get("data", {})
            return data.get("lpIsBurned", False) or data.get("lpLocked", False)
    except Exception:
        return False

# 📈 Get Token Price
async def get_token_price(token_address):
    try:
        async with httpx.AsyncClient() as session:
            url = f"https://public-api.birdeye.so/defi/price?address={token_address}"
            headers = {"X-API-KEY": BIRDEYE_API_KEY}
            res = await session.get(url, headers=headers)

            if res.status_code != 200:
                print(f"[!] Birdeye response error: {res.status_code}")
                return None

            data = res.json().get("data", {})
            return float(data.get("value", 0))
    except Exception as e:
        print(f"[!] Price fetch failed: {e}")
        return None

# 🧾 Token Balance
async def get_token_balance(token_mint):
    try:
        opts = TokenAccountOpts(
            mint=token_mint,
            program_id="TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
        )
        accounts = client.get_token_accounts_by_owner(WALLET_ADDRESS, opts)
        for acc in accounts["result"]["value"]:
            amount = int(acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
            return amount
        return 0
    except Exception as e:
        print(f"[!] Balance fetch failed: {e}")
        return 0

# ⚠️ Rug Condition Logic
def detect_rug_conditions(token_data):
    try:
        return (
            token_data["liquidity"] < 1000 or
            token_data["volume24h"] < 100 or
            token_data["sellTax"] > 25
        )
    except Exception:
        return False

# 📉 Trade Logger
def log_trade_to_csv(token_address, action, amount, price):
    from time import time
    with open("trade_log.csv", "a") as f:
        f.write(f"{time()},{token_address},{action},{amount},{price}\n")
