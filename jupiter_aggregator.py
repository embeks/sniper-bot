# =============================
# jupiter_aggregator.py — Jupiter REST SDK Wrapper
# =============================

import httpx
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from base64 import b64decode

class JupiterAggregatorClient:
    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url

    def get_quote(self, input_mint: Pubkey, output_mint: Pubkey, amount: int, slippage_bps: int = 100) -> dict:
        url = f"https://quote-api.jup.ag/v6/quote"
        params = {
            "inputMint": str(input_mint),
            "outputMint": str(output_mint),
            "amount": amount,
            "slippageBps": slippage_bps,
            "onlyDirectRoutes": False
        }
        response = httpx.get(url, params=params)
        return response.json().get("data", [None])[0]

    def build_swap_transaction(self, swap_tx_b64: str, keypair: Keypair) -> bytes:
        from solana.transaction import Transaction
        from solana.rpc.api import Client

        tx_bytes = b64decode(swap_tx_b64)
        tx = Transaction.deserialize(tx_bytes)
        tx.sign(keypair)
        return tx.serialize()
