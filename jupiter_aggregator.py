# jupiter_aggregator.py — Jupiter V6 integration with userPublicKey
import base64
import json
import httpx
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.signature import Signature
from solana.rpc.api import Client
import os

RPC_URL = os.getenv("RPC_URL")
PRIVATE_KEY = json.loads(os.getenv("SOLANA_PRIVATE_KEY"))
KEYPAIR = Keypair.from_bytes(bytes(PRIVATE_KEY))
WALLET_PUBLIC_KEY = str(KEYPAIR.pubkey())
client = Client(RPC_URL)

class JupiterAggregatorClient:
    def __init__(self):
        self.client = httpx.AsyncClient()
        self.url = "https://quote-api.jup.ag/v6"

    async def get_quote(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int = 100):
        try:
            response = await self.client.get(
                f"{self.url}/quote",
                params={
                    "inputMint": input_mint,
                    "outputMint": output_mint,
                    "amount": amount,
                    "slippageBps": slippage_bps,
                    "userPublicKey": WALLET_PUBLIC_KEY,
                },
                timeout=10,
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"[Jupiter] Quote error: {e}")
            return None

    async def get_swap_transaction(self, route):
        try:
            body = {
                "route": route,
                "userPublicKey": WALLET_PUBLIC_KEY,
                "wrapUnwrapSOL": True,
                "dynamicSlippage": True,
            }
            response = await self.client.post(
                f"{self.url}/swap",
                headers={"Content-Type": "application/json"},
                json=body,
                timeout=15,
            )
            response.raise_for_status()
            swap_txn = response.json()["swapTransaction"]
            return VersionedTransaction.from_bytes(base64.b64decode(swap_txn))
        except Exception as e:
            print(f"[Jupiter] Swap TX error: {e}")
            return None

    def send_swap_transaction(self, transaction: VersionedTransaction) -> Signature:
        try:
            transaction.sign([KEYPAIR])
            return client.send_raw_transaction(transaction.serialize())
        except Exception as e:
            print(f"[Jupiter] Send TX error: {e}")
            return None
