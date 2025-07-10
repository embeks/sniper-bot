from price_utils import get_token_price, get_token_liquidity
from utils import send_telegram_alert
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

keypair = Keypair.from_secret_key(bytes(solana_private_key))
wallet_public_key = keypair.public_key
client = Client("https://api.mainnet-beta.solana.com")


def sell_partial(token_address, wallet, sol_amount):
    try:
        token_pubkey = PublicKey(token_address)
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
            tx,
            wallet,
            opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed)
        )
        print(f"[💸] Partial Sell Sent — TX: {resp['result']}")
        send_telegram_alert(f"📤 Partial sell executed\nTX: {resp['result']}")
    except Exception as e:
        print(f"[‼️] Partial sell failed: {e}")


def auto_sell_if_profit(token_address, entry_price, wallet, take_profits=[2, 5, 10], timeout=300):
    print(f"[⏳] Monitoring {token_address} for profit or rug protection...")
    start_time = time.time()
    last_liquidity = get_token_liquidity(token_address)
    sold_levels = set()

    while time.time() - start_time < timeout:
        try:
            current_price = get_token_price(token_address)
            current_liq = get_token_liquidity(token_address)

            # 🪓 Rug detection
            if current_liq < last_liquidity * 0.75:
                sell_partial(token_address, wallet, 0.99)
                send_telegram_alert(f"⚠️ Rug detected! Liquidity dropped by >25%\nAuto-exited.")
                return

            # 📈 Take-profit logic
            for level in take_profits:
                if current_price >= entry_price * level and level not in sold_levels:
                    percentage = {2: 0.5, 5: 0.25, 10: 0.25}.get(level, 0.1)
                    sell_partial(token_address, wallet, percentage)
                    send_telegram_alert(f"✅ {level}x profit reached!\nAuto-sold {int(percentage * 100)}%")
                    sold_levels.add(level)

            time.sleep(5)

        except Exception as e:
            print(f"[⚠️] Monitor Error: {e}")

    print(f"[⛔] Timeout hit — no profit targets reached.")
    send_telegram_alert("⏰ Timeout — trade closed without hitting any TP levels.")


def buy_token(token_address, sol_amount=0.01):
    try:
        wallet = keypair
        token_pubkey = PublicKey(token_address)

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
            tx,
            wallet,
            opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed)
        )

        time.sleep(2)

        after_balance = client.get_balance(wallet.public_key)["result"]["value"] / 1_000_000_000
        print(f"✅ Buy successful — TX: {resp['result']}")
        print(f"💰 Balance after buy: {after_balance:.4f} SOL")
        send_telegram_alert(f"🟢 Sniped Token: {token_address}\nTX: {resp['result']}")

        # Monitor for profit or rug triggers
        entry_price = get_token_price(token_address)
        auto_sell_if_profit(token_address, entry_price, wallet)

    except Exception as e:
        print(f"[!] Sniping failed: {e}")
        send_telegram_alert(f"❌ Buy failed for {token_address}\nReason: {e}")
