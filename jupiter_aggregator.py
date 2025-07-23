# =============================
# jupiter_aggregator.py — REST Jupiter Buy/Sell SDK (FINAL FIXED)
# =============================

import base64
import json
import httpx
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.signature import Signature
from solana.rpc.api import Client

class JupiterAggregatorClient:
    def __init__(self, rpc_url):
        self.rpc_url = rpc_url
        self.client = Client(rpc_url)

    async def get_quote(self, input_mint: Pubkey, output_mint: Pubkey, amount: int, slippage_bps: int = 100):
        url = (
            f"https://quote-api.jup.ag/v6/quote"
            f"?inputMint={str(input_mint)}"
            f"&outputMint={str(output_mint)}"
            f"&amount={amount}"
            f"&slippageBps={slippage_bps}"
        )
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url)
                data = await response.json()  # ✅ FIXED: await the coroutine
                if data.get("data"):
                    return data["data"][0]  # Top route
                return None
        except Exception as e:
            print(f"[JupiterAggregatorClient] Quote Error: {e}")
            return None

    def build_swap_transaction(self, swap_tx_b64: str, keypair: Keypair) -> bytes:
        try:
            tx_bytes = base64.b64decode(swap_tx_b64)
            tx = VersionedTransaction.from_bytes(tx_bytes)
            tx.sign([keypair])
            return tx.serialize()
        except Exception as e:
            print(f"[JupiterAggregatorClient] Build TX Error: {e}")
            return None

    def send_transaction(self, signed_tx: bytes) -> str:
        try:
            result = self.client.send_raw_transaction(signed_tx)
            return str(result.value) if result else None
        except Exception as e:
            print(f"[JupiterAggregatorClient] Send TX Error: {e}")
            return None
