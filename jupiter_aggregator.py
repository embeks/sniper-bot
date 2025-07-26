import base64
import json
import httpx
import os

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
                response.raise_for_status()
                return response.json()
        except Exception as e:
            print(f"[Quote Error] {e}")
            return None

    async def swap(self, transactions: dict):
        try:
            swap_tx_b64 = transactions.get("swapTransaction")
            if not swap_tx_b64:
                raise ValueError("No swap transaction found in response")

            swap_tx_bytes = base64.b64decode(swap_tx_b64)
            versioned_tx = VersionedTransaction.from_bytes(swap_tx_bytes)

            private_key_list = json.loads(os.getenv("SOLANA_PRIVATE_KEY"))
            keypair = Keypair.from_bytes(bytes(private_key_list))
            signed_tx = versioned_tx.sign([keypair])
            txid = self.client.send_raw_transaction(signed_tx.serialize())
            return str(txid)
        except Exception as e:
            print(f"[Swap Error] {e}")
            return None

