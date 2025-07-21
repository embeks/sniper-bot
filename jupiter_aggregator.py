# =============================
# jupiter_aggregator.py — Jupiter Buy/Sell SDK Helper
# =============================

import base64
import json
import httpx
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

class JupiterAggregatorClient:
    def __init__(self, rpc_url):
        self.rpc_url = rpc_url
        self.rpc = Client(rpc_url)
        self.base_url = "https://quote-api.jup.ag"

    def get_quote(self, input_mint: Pubkey, output_mint: Pubkey, amount: int):
        try:
            url = f"{self.base_url}/v6/quote"
            params = {
                "inputMint": str(input_mint),
                "outputMint": str(output_mint),
                "amount": amount,
                "slippageBps": 100,  # 1% slippage
                "swapMode": "ExactIn"
            }
            with httpx.Client() as client:
                res = client.get(url, params=params)
                if res.status_code == 200:
                    data = res.json()
                    if "swapTransaction" in data:
                        return data
            return None
        except Exception as e:
            print(f"[JUP QUOTE ERROR] {e}")
            return None

    def build_swap_transaction(self, swap_tx_b64: str, keypair: Keypair):
        try:
            latest_blockhash = self.rpc.get_latest_blockhash()["result"]["value"]["blockhash"]
            tx_bytes = base64.b64decode(swap_tx_b64)
            tx = VersionedTransaction.deserialize(tx_bytes)
            tx.sign([keypair])
            return tx.serialize()
        except Exception as e:
            print(f"[JUP BUILD TX ERROR] {e}")
            return None
