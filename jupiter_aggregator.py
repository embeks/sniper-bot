import base64
import json
import httpx
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.signature import Signature
from solana.rpc.api import Client
import os

# === ENV ===
RPC_URL = os.getenv("RPC_URL")
SOLANA_PRIVATE_KEY = json.loads(os.getenv("SOLANA_PRIVATE_KEY"))

# === DECODE WALLET ===
keypair = Keypair.from_bytes(bytes(SOLANA_PRIVATE_KEY))
PUBLIC_KEY = str(keypair.pubkey())

class JupiterAggregatorClient:
    def __init__(self):
        self.rpc_url = RPC_URL
        self.client = Client(RPC_URL)
        self.base_url = "https://quote-api.jup.ag/v6"

    async def get_quote(self, input_mint: Pubkey, output_mint: Pubkey, amount: int, slippage_bps: int = 100):
        url = (
            f"{self.base_url}/quote"
            f"?inputMint={str(input_mint)}"
            f"&outputMint={str(output_mint)}"
            f"&amount={amount}"
            f"&slippageBps={slippage_bps}"
            f"&userPublicKey={PUBLIC_KEY}"
        )
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url)
                response.raise_for_status()
                return response.json()
        except Exception as e:
            print(f"[JupiterAggregatorClient] Quote fetch error: {e}")
            return None

    async def get_swap_transaction(self, quote_response):
        url = f"{self.base_url}/swap"
        payload = {
            "route": quote_response["data"][0],
            "userPublicKey": PUBLIC_KEY,
            "wrapUnwrapSOL": True,
            "feeAccount": None
        }
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                swap_tx = base64.b64decode(response.json()["swapTransaction"])
                return VersionedTransaction.deserialize(swap_tx)
        except Exception as e:
            print(f"[JupiterAggregatorClient] Swap fetch error: {e}")
            return None
