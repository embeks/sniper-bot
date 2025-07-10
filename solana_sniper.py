import os
import json
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

def buy_token(token_address, sol_amount=0.01):
    try:
        wallet = keypair
        token_pubkey = PublicKey(token_address)

        # 🧾 Get wallet balance before
        before_balance = client.get_balance(wallet.public_key)["result"]["value"] / 1_000_000_000
        print(f"💰 Balance before buy: {before_balance:.4f} SOL")

        # 💸 Create transaction
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

        # 🚀 Send transaction
        resp = client.send_transaction(
            tx,
            wallet,
            opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed)
        )

        # ⏱ Wait a moment for network confirmation (optional safety buffer)
        import time
        time.sleep(2)

        # 🧾 Get wallet balance after
        after_balance = client.get_balance(wallet.public_key)["result"]["value"] / 1_000_000_000
        print(f"✅ Buy successful — TX: {resp['result']}")
        print(f"💰 Balance after buy: {after_balance:.4f} SOL")

    except Exception as e:
        print(f"[!] Sniping failed: {e}")
