# solana_sniper.py

from solana.publickey import PublicKey
from solana.rpc.api import Client
from solana.transaction import Transaction
from solana.system_program import TransferParams, transfer
from solana.keypair import Keypair
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
import os
import json

# 🔧 Setup RPC client
client = Client("https://api.mainnet-beta.solana.com")

# 🔑 Load wallet keypair from .json file
def load_keypair(filepath="solana_wallet.json"):
    with open(filepath, "r") as f:
        secret = json.load(f)
    return Keypair.from_secret_key(bytes(secret))

# 🧨 Send SOL to the token address (simple buy logic)
def buy_token(token_address, sol_amount=0.01):
    try:
        wallet = load_keypair()
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
