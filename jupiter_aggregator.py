# =============================
# jupiter_aggregator.py — Final Revert (Pre-Testing Version)
# =============================

import base64
import json
import httpx
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solana.rpc.api import Client

class JupiterAggregatorClient:
    def __init__(self, rpc_url):
        self.rpc = Client(rpc_url)
        self.headers = {"Content-Type": "application/json"}

    def get_quote(self, input_mint, output_mint, amount):
        url = (
            f"https://quote-api.jup.ag/v6/quote"
            f"?inputMint={input_mint}"
            f"&outputMint={output_mint}"
            f"&amount={amount}"
            f"&slippageBps=100"
        )
        try:
            response = httpx.get(url, timeout=10)
            return response.json()
        except:
            return None

    def build_swap_transaction(self, swap_tx_base64: str, keypair: Keypair):
        try:
            tx_bytes = base64.b64decode(swap_tx_base64)
            versioned_tx = VersionedTransaction.from_bytes(tx_bytes)
            versioned_tx.sign([keypair])
            return versioned_tx.serialize()
        except:
            return None
