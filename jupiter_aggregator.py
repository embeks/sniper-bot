import base64
import json
import httpx
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.signature import Signature
from solana.rpc.api import Client
import os

# Load from env
token_list_url = "https://token.jup.ag/all"
RPC_URL = os.getenv("RPC_URL")
WALLET_PRIVATE_KEY = json.loads(os.getenv("SOLANA_PRIVATE_KEY"))
WALLET_KEYPAIR = Keypair.from_bytes(bytes(WALLET_PRIVATE_KEY))
WALLET_PUBLIC_KEY = str(WALLET_KEYPAIR.pubkey())

class JupiterAggregatorClient:
    def __init__(self):
        self.url = "https://quote-api.jup.ag/v6"
        self.client = httpx.AsyncClient()

    async def get_quote(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int = 100):
        try:
            params = {
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": amount,
                "slippageBps": slippage_bps,
                "userPublicKey": WALLET_PUBLIC_KEY,
                "onlyDirectRoutes": False,
            }
            response = await self.client.get(f"{self.url}/quote", params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
            return data.get("data", [None])[0]  # Best route
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
            print("[Jupiter] Swap body payload:", json.dumps(body, indent=2)[:500])
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

    async def close(self):
        await self.client.aclose()
