# =============================
# jupiter_aggregator.py — ELITE FINAL VERSION (FIXED: is_token_tradable)
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

    async def get_quote(self, input_mint: Pubkey, output_mint: Pubkey, amount: int, slippage_bps: int = 100, only_direct_routes: bool = False):
        url = (
            f"https://quote-api.jup.ag/v6/quote"
            f"?inputMint={str(input_mint)}"
            f"&outputMint={str(output_mint)}"
            f"&amount={amount}"
            f"&slippageBps={slippage_bps}"
            f"&onlyDirectRoutes={'true' if only_direct_routes else 'false'}"
        )
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url)
                if response.status_code != 200:
                    return None
                json_data = response.json()
                routes = json_data.get("data", [])
                if not routes:
                    return None
                return routes[0]  # contains swapTransaction and outAmount
        except Exception:
            return None

    async def get_swap_transaction(self, route: dict):
        url = "https://quote-api.jup.ag/v6/swap"
        headers = {"Content-Type": "application/json"}
        payload = {
            "route": route,
            "userPublicKey": str(self.client._provider.wallet.public_key if hasattr(self.client._provider, 'wallet') else ""),
            "wrapUnwrapSOL": True,
            "asLegacyTransaction": False
        }
        try:
            async with httpx.AsyncClient() as client:
                res = await client.post(url, headers=headers, json=payload)
                if res.status_code == 200:
                    return res.json().get("swapTransaction")
                return None
        except Exception:
            return None

    async def is_token_tradable(self, input_mint: Pubkey, output_mint: Pubkey, amount: int, slippage_bps: int = 100) -> bool:
        url = (
            f"https://quote-api.jup.ag/v6/quote"
            f"?inputMint={str(input_mint)}"
            f"&outputMint={str(output_mint)}"
            f"&amount={amount}"
            f"&slippageBps={slippage_bps}"
        )
        try:
            async with httpx.AsyncClient() as client:
                res = await client.get(url)
                if res.status_code != 200:
                    return False
                json_data = res.json()
                routes = json_data.get("data", [])
                if not routes:
                    return False
                return int(routes[0].get("outAmount", 0)) > 0
        except Exception:
            return False

    def build_swap_transaction(self, swap_tx_base64: str, keypair: Keypair) -> bytes:
        try:
            swap_tx = base64.b64decode(swap_tx_base64)
            tx = VersionedTransaction.from_bytes(swap_tx)
            tx.sign([keypair])
            return bytes(tx)
        except Exception:
            return None
