from price_utils import get_token_price, get_token_liquidity
from utils import (
    send_telegram_alert,
    is_contract_verified,
    has_blacklist_or_mint_functions,
    is_lp_locked_or_burned,
    check_token_safety
)
import os
import json
import time
import asyncio
import websockets
import requests
from solana.publickey import PublicKey
from solana.rpc.api import Client
from solana.transaction import Transaction
from solana.system_program import TransferParams, transfer
from solana.keypair import Keypair
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts

# 🔐 Load environment variables
solana_key_str = os.getenv("SOLANA_PRIVATE_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SOLANA_MEMPOOL_WS = os.getenv("SOLANA_MEMPOOL_WS")

# 🧠 Setup wallet
if solana_key_str:
    solana_private_key = json.loads(solana_key_str)
else:
    raise Exception("❌ SOLANA_PRIVATE_KEY not set in environment!")
keypair = Keypair.from_secret_key(bytes(solana_private_key))
wallet_public_key = keypair.public_key

# 🔧 RPC client
client = Client("https://api.mainnet-beta.solana.com")

# 🛡 Honeypot check
def is_token_safe(token_address):
    safety_status = check_token_safety(token_address)
    send_telegram_alert(f"[🧠] Safety Check Result: {safety_status}")
    return safety_status.startswith("✅")

# 🚀 Buy token
def buy_token(token_address, sol_amount=0.01, max_slippage=0.15):
    try:
        wallet = keypair
        token_pubkey = PublicKey(token_address)

        entry_price = get_token_price(token_address)
        if not entry_price:
            send_telegram_alert("[⛔] Entry price unavailable — aborting snipe")
            return

        liquidity = get_token_liquidity(token_address)
        if liquidity == 0:
            send_telegram_alert("[❌] Liquidity is zero. Skipping token.")
            return

        if not is_token_safe(token_address):
            send_telegram_alert("[⚠️] Token failed honeypot/safety check")
            return

        before_balance = client.get_balance(wallet.public_key)["result"]["value"] / 1_000_000_000
        print(f"💰 Balance before buy: {before_balance:.4f} SOL")

        tx = Transaction()
        tx.add(
            transfer(
                TransferParams(
                    from_pubkey=wallet.public_key,
                    to_pubkey=token_pubkey,
                    lamports=int(sol_amount * 1_000_000_000)
                )
            )
        )

        resp = client.send_transaction(
            tx, wallet, opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed)
        )

        time.sleep(2)
        after_balance = client.get_balance(wallet.public_key)["result"]["value"] / 1_000_000_000
        send_telegram_alert(f"✅ Buy TX sent — {resp['result']}\n💰 New balance: {after_balance:.4f} SOL")

        auto_sell_if_profit(token_address, entry_price, wallet)

    except Exception as e:
        print(f"[!] Sniping failed: {e}")
        send_telegram_alert(f"[!] Sniping failed: {e}")

# 📈 Auto-sell monitor

def auto_sell_if_profit(token_address, entry_price, wallet, take_profit=1.5, timeout=300):
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            current_price = get_token_price(token_address)
            if current_price and current_price >= entry_price * take_profit:
                send_telegram_alert(f"[✅] Profit target hit — Price: {current_price:.4f}")
                sell_token(token_address, wallet)
                return
        except Exception as e:
            print(f"[⚠️] Error checking price: {e}")
        time.sleep(5)
    send_telegram_alert("[⛔] Timeout hit — No profit exit.")

# 💸 Sell token

def sell_token(token_address, wallet):
    try:
        token_pubkey = PublicKey(token_address)
        tx = Transaction()
        tx.add(
            transfer(
                TransferParams(
                    from_pubkey=wallet.public_key,
                    to_pubkey=token_pubkey,
                    lamports=500_000
                )
            )
        )
        resp = client.send_transaction(
            tx, wallet, opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed)
        )
        send_telegram_alert(f"[💰] Sell TX sent — {resp['result']}")
    except Exception as e:
        print(f"[‼️] Sell failed: {e}")
        send_telegram_alert(f"[‼️] Sell failed: {e}")

# 🔍 Mempool listener

async def listen_mempool():
    async with websockets.connect(SOLANA_MEMPOOL_WS) as ws:
        await ws.send(json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsSubscribe",
            "params": [
                {"mentions": ["4F5eMW7faAaLfsn5jXDzUsXXvyrRvwBxAV6jDFitZZGX", "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB"]},
                {"commitment": "confirmed"}
            ]
        }))
        print("🟢 Mempool listener active...")
        while True:
            try:
                response = await ws.recv()
                data = json.loads(response)
                log_info = data.get("params", {}).get("result", {})
                signature = log_info.get("signature", "N/A")
                msg = (
                    f"🔔 Mempool Event Detected!\n"
                    f"Program: Raydium/Jupiter\n"
                    f"Tx Signature: {signature}"
                )
                send_telegram_alert(msg)
                print(f"[+] Alert sent for: {signature}")
                # Insert your logic here (e.g., detect token address from logs and call buy_token)
            except Exception as e:
                print(f"[!] Mempool error: {e}")
                await asyncio.sleep(5)

# 🚀 Launch

if __name__ == "__main__":
    send_telegram_alert("✅ Sniper bot launched — monitoring mempool")
    asyncio.run(listen_mempool())
