# =============================
# jupiter_aggregator.py — REST Jupiter Buy/Sell SDK + Raydium Fallback
# =============================

import base64
import httpx
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solana.rpc.api import Client


class JupiterAggregatorClient:
    def __init__(self, rpc_url):
        self.rpc_url = rpc_url
        self.client = Client(rpc_url)

    async def get_quote(
        self,
        input_mint: Pubkey,
        output_mint: Pubkey,
        amount: int,
        slippage_bps: int = 100,
        only_direct_routes: bool = False,  # ✅ optional flag
    ):
        url = (
            f"https://quote-api.jup.ag/v6/quote"
            f"?inputMint={str(input_mint)}"
            f"&outputMint={str(output_mint)}"
            f"&amount={amount}"
            f"&slippageBps={slippage_bps}"
            f"&onlyDirectRoutes={'true' if only_direct_routes else 'false'}"
        )
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(url)
                if response.status_code != 200:
                    print(f"[JupiterAggregator] ⚠️ Quote failed with status {response.status_code}")
                    return None
                data = response.json()

                if not data.get("outAmount"):
                    print(f"[JupiterAggregator] ❌ No outAmount for {output_mint}")
                    return None

                return data
        except Exception as e:
            print(f"[JupiterAggregator] ❌ get_quote error: {e}")
            return None

    def build_swap_transaction(self, swap_tx_b64: str, keypair: Keypair) -> bytes:
        try:
            tx_bytes = base64.b64decode(swap_tx_b64)
            tx = VersionedTransaction.from_bytes(tx_bytes)
            tx.sign([keypair])
            return tx.serialize()
        except Exception as e:
            print(f"[JupiterAggregator] ❌ build_tx error: {e}")
            return None

    def send_transaction(self, signed_tx: bytes) -> str:
        try:
            result = self.client.send_raw_transaction(signed_tx)
            return str(result.value)
        except Exception as e:
            print(f"[JupiterAggregator] ❌ send_tx error: {e}")
            return None
