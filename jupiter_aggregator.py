# =============================
# jupiter_aggregator.py — Final (Updated for SOLANA_PRIVATE_KEY)
# =============================

import base64
import json
import os
import httpx

from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.signature import Signature
from solana.rpc.api import Client

JUPITER_BASE_URL = os.getenv("JUPITER_BASE_URL", "https://quote-api.jup.ag")
RPC_URL = os.getenv("RPC_URL")
SOLANA_CLIENT = Client(RPC_URL)

# Load wallet
WALLET_PRIVATE_KEY = json.loads(os.getenv("SOLANA_PRIVATE_KEY"))
KEYPAIR = Keypair.from_bytes(bytes(WALLET_PRIVATE_KEY))
WALLET_ADDRESS = str(KEYPAIR.pubkey())

class JupiterAggregatorClient:
    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url
        self.client = Client(rpc_url)

    async def get_quote(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int = 100):
        url = (
            f"{JUPITER_BASE_URL}/v6/quote"
            f"?inputMint={input_mint}"
            f"&outputMint={output_mint}"
            f"&amount={amount}"
            f"&slippageBps={slippage_bps}"
        )
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url)
                data = response.json()
                if "data" not in data or not data["data"]:
                    return None
                return data["data"][0]
        except Exception as e:
            print(f"[JUPITER QUOTE ERROR] {e}")
            return None

    async def get_swap_tx(self, quote: dict):
        url = f"{JUPITER_BASE_URL}/v6/swap"
        payload = {
            "userPublicKey": WALLET_ADDRESS,
            "wrapUnwrapSOL": True,
            "feeAccount": None,
            "computeUnitPriceMicroLamports": "5000",
            **quote,
        }
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=payload)
                data = response.json()
                swap_tx = base64.b64decode(data["swapTransaction"])
                return VersionedTransaction.deserialize(swap_tx)
        except Exception as e:
            print(f"[JUPITER SWAP ERROR] {e}")
            return None

    def send_transaction(self, transaction: VersionedTransaction):
        try:
            tx = transaction.sign([KEYPAIR])
            encoded = base64.b64encode(tx.serialize()).decode("utf-8")
            response = self.client.send_raw_transaction(encoded)
            signature = response["result"]
            print(f"[✅ Jupiter TX Success] {signature}")
            return signature
        except Exception as e:
            print(f"[TX ERROR] {e}")
            return None
