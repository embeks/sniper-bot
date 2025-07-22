# ============================
# jupiter_aggregator.py — Elite Version (REST SDK)
# ============================

import httpx
import base64
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.signature import Signature
from solders.rpc.requests import SendTransaction
from solana.rpc.api import Client
import os

class JupiterAggregatorClient:
    def __init__(self, rpc_url):
        self.rpc = Client(rpc_url)
        self.base_url = "https://quote-api.jup.ag"

    def get_quote(self, input_mint: Pubkey, output_mint: Pubkey, amount: int):
        url = f"{self.base_url}/v6/quote"
        params = {
            "inputMint": str(input_mint),
            "outputMint": str(output_mint),
            "amount": str(amount),
            "slippageBps": "100",  # 1%
            "onlyDirectRoutes": "false",
        }
        try:
            r = httpx.get(url, params=params)
            if r.status_code == 200:
                data = r.json()
                return data["swap"] if "swap" in data else None
            else:
                return None
        except Exception:
            return None

    def build_swap_transaction(self, swap_tx_b64: str, keypair: Keypair) -> bytes:
        try:
            tx_bytes = base64.b64decode(swap_tx_b64)
            tx = VersionedTransaction.from_bytes(tx_bytes)
            tx.sign([keypair])
            return bytes(tx)
        except Exception as e:
            raise RuntimeError(f"Failed to build swap transaction: {e}")

    def send_transaction(self, tx_bytes: bytes) -> str:
        try:
            sig = self.rpc.send_raw_transaction(tx_bytes)
            return str(sig)
        except Exception as e:
            raise RuntimeError(f"Failed to send transaction: {e}")
