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

# 🧨 Send SOL to the token address (simple buy logic)
def buy_token(token_address, sol_amount=0.01):
    try:
        wallet = keypair
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
            opts=TxOpts(skip_confirmation=False, preflight_commitment=Confirmed)
        )

        print(f"✅ SNIPED {token_address} | TX: {resp['result']}")
        return True

    except Exception as e:
        print(f"[!] Snipe failed: {e}")
        return False
