# === jupiter_aggregator.py ===
import base64
import json
import httpx
import os

from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.signature import Signature
from solana.rpc.api import Client

RPC_URL = os.getenv("RPC_URL")
JUPITER_BASE_URL = os.getenv("JUPITER_BASE_URL", "https://quote-api.jup.ag")
SOL_MINT = "So11111111111111111111111111111111111111112"

# Load wallet
private_key = json.loads(os.getenv("SOLANA_PRIVATE_KEY"))
keypair = Keypair.from_bytes(bytes(private_key))
wallet_pubkey = str(keypair.pubkey())
rpc_client = Client(RPC_URL)


class JupiterAggregatorClient:
    def __init__(self):
        self.base_url = JUPITER_BASE_URL
        self.wallet = wallet_pubkey

    async def get_quote(self, output_mint: str, amount_sol: float, slippage_bps: int = 100):
        input_mint = SOL_MINT
        amount = int(amount_sol * 1e9)

        url = (
            f"{self.base_url}/v6/quote?"
            f"inputMint={input_mint}&outputMint={output_mint}"
            f"&amount={amount}&slippageBps={slippage_bps}&platformFeeBps=0"
            f"&userPublicKey={self.wallet}"
        )

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(url)
                response.raise_for_status()
                data = response.json()
                if "routes" not in data or not data["routes"]:
                    return None
                return data["routes"][0]
        except Exception as e:
            print(f"🛑 Quote fetch error: {e}")
            return None

    async def execute_swap(self, route: dict):
        url = f"{self.base_url}/v6/swap"

        payload = {
            "route": route,
            "userPublicKey": self.wallet,
            "wrapUnwrapSOL": True,
            "dynamicComputeUnitLimit": True,
            "useLegacyTransaction": False
        }

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                res = await client.post(url, json=payload)
                res.raise_for_status()
                tx = res.json()["swapTransaction"]
                raw_tx = base64.b64decode(tx)
                versioned_tx = VersionedTransaction.deserialize(raw_tx)
                sig = rpc_client.send_transaction(versioned_tx, keypair)
                print(f"✅ Sent Jupiter swap: {sig}")
                return sig
        except Exception as e:
            print(f"❌ Swap execution failed: {e}")
            return None
