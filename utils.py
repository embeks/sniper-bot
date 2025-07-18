# =========================
# utils.py (FINAL UPGRADE)
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
from solana.transaction import Transaction
from spl.token.instructions import approve
from solana.rpc.commitment import Confirmed

load_dotenv()

# 🔧 Environment
SOLANA_PRIVATE_KEY = json.loads(os.getenv("SOLANA_PRIVATE_KEY"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")
RPC_URL = os.getenv("RPC_URL")
RPC_URL_TRITON = os.getenv("RPC_URL_TRITON")

# 🔐 Wallet
keypair = Keypair.from_bytes(bytes(SOLANA_PRIVATE_KEY))
WALLET_PUBKEY = keypair.pubkey()
WALLET_ADDRESS = str(WALLET_PUBKEY)
client = Client(RPC_URL)

# ✅ RPC client
def get_rpc_client(use_triton=False) -> AsyncClient:
    return AsyncClient(RPC_URL_TRITON if use_triton else RPC_URL)

# 📤 Telegram Alerts
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

# 🧪 Token Safety Checks
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
            transfer_fee = data.get("transferFeeBasisPoints", 0)
            has_blacklist = data.get("hasBlacklist", False)
            has_mint = data.get("hasMintAuthority", True)
            renounced = data.get("isOwnershipRenounced", False)
            multisig = data.get("isMultisig", False)
            lp_locked = data.get("lpLocked", False)
            lp_lock_time = data.get("lpLockDuration", 0)  # New field (if supported)

            if liquidity < 10000:
                return "❌ Rug Risk: Low Liquidity"
            if buy_tax > 15 or sell_tax > 15:
                return f"⚠️ Honeypot Risk: High Tax ({buy_tax}% / {sell_tax}%)"
            if holders < 20:
                return "⚠️ Low Holders"
            if transfer_fee > 100:
                return "❌ High transfer fee detected"
            if has_blacklist:
                return "❌ Blacklist function detected"
            if has_mint:
                return "❌ Mint authority not revoked"
            if not (renounced or multisig):
                return "❌ Token not renounced or multisig-controlled"
            if not lp_locked or lp_lock_time < 15552000:  # ~6 months
                return "❌ LP not locked long enough"

            return "✅ Passed all safety checks"
    except Exception as e:
        return f"[!] Safety check error: {e}"

# 📉 Logger + Enhanced CSV

def log_trade_to_csv(token_address, action, amount, price):
    try:
        pnl = "N/A"
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        with open("trade_log.csv", "a") as f:
            f.write(f"{timestamp},{token_address},{action},{amount},{price},{pnl}\n")
    except Exception as e:
        print(f"[‼️] CSV log error: {e}")

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
