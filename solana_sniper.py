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
import requests
import websockets
from solana.publickey import PublicKey
from solana.rpc.api import Client
from solana.transaction import Transaction
from solana.system_program import TransferParams, transfer
from solana.keypair import Keypair
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts

# 🔐 Load Solana private key from environment
solana_key_str = os.getenv("SOLANA_PRIVATE_KEY")
if solana_key_str:
    solana_private_key = json.loads(solana_key_str)
else:
    raise Exception("❌ SOLANA_PRIVATE_KEY not set in environment!")

keypair = Keypair.from_secret_key(bytes(solana_private_key))
wallet_public_key = keypair.public_key
client = Client("https://api.mainnet-beta.solana.com")

# --- Trade Tracker ---
tracked_tokens = {}
PARTIAL_SELLS = {"2x": 0.33, "5x": 0.33, "10x": 1.0}
TIMEOUT_SELL_SECONDS = 300
RUG_THRESHOLD = 0.75


def sell_token(token_address, wallet, percentage=1.0):
    try:
        token_pubkey = PublicKey(token_address)
        lamports = int(client.get_balance(wallet.public_key)['result']['value'] * percentage)
        tx = Transaction()
        tx.add(transfer(TransferParams(from_pubkey=wallet.public_key, to_pubkey=token_pubkey, lamports=lamports)))
        resp = client.send_transaction(tx, wallet, opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed))
        send_telegram_alert(f"[💰] Sell TX sent — {resp['result']}")
    except Exception as e:
        send_telegram_alert(f"[‼️] Sell failed: {e}")


def track_token(token_address, buy_price, initial_liquidity):
    tracked_tokens[token_address] = {
        "buy_price": buy_price,
        "initial_liquidity": initial_liquidity,
        "buy_time": time.time(),
        "sells": set()
    }
    send_telegram_alert(f"✅ Tracking {token_address}\nBuy: ${buy_price:.6f}")


def monitor_tokens():
    for token_address, data in list(tracked_tokens.items()):
        price = get_token_price(token_address)
        liquidity = get_token_liquidity(token_address)
        if not price or not liquidity:
            continue

        for label, multiplier in [("2x", 2), ("5x", 5), ("10x", 10)]:
            if label not in data["sells"] and price >= data["buy_price"] * multiplier:
                sell_token(token_address, keypair, PARTIAL_SELLS[label])
                send_telegram_alert(f"💰 {label} profit hit. Sold {int(PARTIAL_SELLS[label]*100)}%")
                data["sells"].add(label)

        if time.time() - data["buy_time"] > TIMEOUT_SELL_SECONDS and "timeout" not in data["sells"]:
            sell_token(token_address, keypair)
            send_telegram_alert(f"⏰ Timeout hit. Sold all for {token_address}")
            data["sells"].add("timeout")

        if liquidity < data["initial_liquidity"] * RUG_THRESHOLD and "rug" not in data["sells"]:
            sell_token(token_address, keypair)
            send_telegram_alert(f"🚨 RUG WARNING: Liquidity dropped for {token_address}")
            data["sells"].add("rug")


def buy_token(token_address, sol_amount=0.01):
    try:
        wallet = keypair
        token_pubkey = PublicKey(token_address)

        safety = check_token_safety(token_address)
        send_telegram_alert(f"🔎 Token Safety:
{safety}")
        if "❌" in safety or "⚠️" in safety:
            return

        liquidity = get_token_liquidity(token_address)
        if liquidity < 1000:
            send_telegram_alert("❌ Insufficient liquidity — Skipping")
            return

        entry_price = get_token_price(token_address)
        if not entry_price:
            send_telegram_alert("❌ Price unavailable — Skipping")
            return

        before_balance = client.get_balance(wallet.public_key)['result']['value'] / 1e9

        tx = Transaction()
        tx.add(transfer(TransferParams(from_pubkey=wallet.public_key, to_pubkey=token_pubkey,
                                       lamports=int(sol_amount * 1e9))))
        resp = client.send_transaction(tx, wallet, opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed))

        after_balance = client.get_balance(wallet.public_key)['result']['value'] / 1e9

        send_telegram_alert(f"✅ Buy TX sent: {resp['result']}\n💰 Balance: {after_balance:.4f} SOL")

        track_token(token_address, entry_price, liquidity)

    except Exception as e:
        send_telegram_alert(f"[!] Buy failed: {e}")


# --- Mempool / TX Feed Monitor ---
async def listen_mempool():
    url = os.getenv("SOLANA_MEMPOOL_WS")
    raydium = "4F5eMW7faAaLfsn5jXDzUsXXvyrRvwBxAV6jDFitZZGX"
    jupiter = "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB"

    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
            "params": [{"mentions": [raydium, jupiter]}, {"commitment": "confirmed"}]
        }))
        print("🟢 Mempool monitoring active...")
        while True:
            try:
                resp = await ws.recv()
                data = json.loads(resp)
                signature = data.get("params", {}).get("result", {}).get("signature")
                send_telegram_alert(f"🔔 TX Detected: {signature}")
                # Placeholder: Replace with actual logic to get token
                dummy_token = "So11111111111111111111111111111111111111112"
                buy_token(dummy_token, sol_amount=0.01)
            except Exception as e:
                print(f"[!] Mempool error: {e}")
                await asyncio.sleep(3)


if __name__ == "__main__":
    send_telegram_alert("✅ Sniper Bot Online")
    asyncio.run(listen_mempool())
