from price_utils import get_token_price, get_token_liquidity
from utils import (
    send_telegram_alert,
    is_contract_verified,
    has_blacklist_or_mint_functions,
    is_lp_locked_or_burned
)
import os
import json
import time
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

# 🧠 Convert to usable keypair
keypair = Keypair.from_secret_key(bytes(solana_private_key))
wallet_public_key = keypair.public_key

# 🔧 Setup RPC client
client = Client("https://api.mainnet-beta.solana.com")

# 📉 RUG PROTECTION
previous_price = {}

def auto_sell_if_profit(token_address, entry_price, wallet, take_profit=1.5, timeout=300):
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            current_price = get_token_price(token_address)
            if current_price and current_price >= entry_price * take_profit:
                send_telegram_alert(f"[✅] Profit target hit — Price: {current_price:.4f}")
                sell_token(token_address, wallet)
                return

            # 🧨 Rug Detection
            if token_address in previous_price:
                drop = (previous_price[token_address] - current_price) / previous_price[token_address]
                if drop >= 0.25:
                    send_telegram_alert(f"[🚨] Rug alert! Price dropped by 25+% — {current_price:.4f}")
                    sell_token(token_address, wallet)
                    return
            previous_price[token_address] = current_price

        except Exception as e:
            print(f"[⚠️] Error checking price: {e}")
        time.sleep(5)
    send_telegram_alert("[⛔] Timeout hit — No profit exit.")

def sell_token(token_address, wallet):
    try:
        token_pubkey = PublicKey(token_address)
        tx = Transaction()
        tx.add(
            transfer(
                TransferParams(
                    from_pubkey=wallet.public_key,
                    to_pubkey=token_pubkey,
                    lamports=500_000  # Example: sell 0.0005 SOL
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

def buy_token(token_address, sol_amount=0.01, max_slippage=0.15):
    try:
        wallet = keypair
        token_pubkey = PublicKey(token_address)

        entry_price = get_token_price(token_address)
        if not entry_price:
            send_telegram_alert("[⛔] Entry price unavailable — aborting snipe")
            return

        # ✅ Slippage logic (check liquidity)
        liquidity = get_token_liquidity(token_address)
        if liquidity == 0:
            send_telegram_alert("[❌] Liquidity is zero. Skipping token.")
            return

        # 🔒 Protection Checks
        if not is_contract_verified(token_address):
            send_telegram_alert("[⛔] Token not verified — skipping")
            return
        if has_blacklist_or_mint_functions(token_address):
            send_telegram_alert("[⛔] Suspicious token functions — skipping")
            return
        if not is_lp_locked_or_burned(token_address):
            send_telegram_alert("[⛔] LP not locked or burned — skipping")
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

# 🧠 Mempool Monitoring (placeholder logic)
def mempool_monitor():
    print("[👁️] Mempool listener running...")
    while True:
        try:
            dummy_token_address = "Dummy111111111111111111111111111111111111111"
            buy_token(dummy_token_address, sol_amount=0.01)
        except Exception as e:
            print(f"[!] Mempool error: {e}")
        time.sleep(60)

if __name__ == "__main__":
    send_telegram_alert("✅ Sniper bot launched — monitoring mempool")
    mempool_monitor()
