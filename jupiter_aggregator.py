import base64
import json
import httpx
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solana.rpc.async_api import AsyncClient

class JupiterAggregatorClient:
    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url
        self.client = AsyncClient(rpc_url)

    async def get_quote(self, input_mint: Pubkey, output_mint: Pubkey, amount: int,
                        slippage_bps: int = 100, only_direct_routes: bool = False,
                        user_pubkey: Pubkey = None):
        url = "https://quote-api.jup.ag/v6/quote"
        params = {
            "inputMint": str(input_mint),
            "outputMint": str(output_mint),
            "amount": amount,
            "slippageBps": slippage_bps,
            "onlyDirectRoutes": str(only_direct_routes).lower(),
            "swapMode": "ExactIn",
        }
        if user_pubkey:
            params["userPublicKey"] = str(user_pubkey)

        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(url, params=params)
                if response.status_code == 200:
                    data = response.json()
                    return data.get("data", [None])[0]
                else:
                    print(f"[JupiterAggregator] Quote API failed: {response.text}")
            except Exception as e:
                print(f"[JupiterAggregator] Quote exception: {e}")
        return None

    async def get_swap_transaction(self, route: dict, keypair: Keypair):
        url = "https://quote-api.jup.ag/v6/swap"
        payload = {
            "route": route,
            "userPublicKey": str(keypair.pubkey()),
            "wrapUnwrapSOL": True,
            "useSharedAccounts": True,
        }
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(url, json=payload)
                if response.status_code == 200:
                    data = response.json()
                    return data.get("swapTransaction")
                else:
                    print(f"[JupiterAggregator] Swap build failed: {response.text}")
            except Exception as e:
                print(f"[JupiterAggregator] Swap exception: {e}")
        return None

    def build_swap_transaction(self, swap_tx_base64: str, keypair: Keypair):
        try:
            swap_tx_bytes = base64.b64decode(swap_tx_base64)
            versioned_tx = VersionedTransaction.deserialize(swap_tx_bytes)
            versioned_tx.sign([keypair])
            return versioned_tx
        except Exception as e:
            print(f"[JupiterAggregator] Build transaction error: {e}")
            return None

    async def send_transaction(self, txn: VersionedTransaction, keypair: Keypair):
        try:
            response = await self.client.send_raw_transaction(
                txn.serialize(),
                opts={"skip_preflight": True, "preflight_commitment": "processed"}
            )
            return response.value
        except Exception as e:
            print(f"[JupiterAggregator] Send TX error: {e}")
            return None
