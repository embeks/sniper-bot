import base64
import json
import httpx
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.signature import Signature
from solana.rpc.api import Client
import os

JUPITER_BASE_URL = os.getenv("JUPITER_BASE_URL", "https://quote-api.jup.ag")
SOL_MINT = "So11111111111111111111111111111111111111112"

class JupiterAggregatorClient:
    def __init__(self):
        self.client = Client(os.getenv("RPC_URL"))

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
                resp = await client.get(url)
                if resp.status_code == 200:
                    return resp.json()
                return None
        except Exception as e:
            print(f"Quote fetch error: {e}")
            return None

    async def get_swap_transaction(self, quote_response, user_keypair: Keypair):
        try:
            swap_url = f"{JUPITER_BASE_URL}/v6/swap"
            payload = {
                "userPublicKey": str(user_keypair.pubkey()),
                "quoteResponse": quote_response,
                "wrapUnwrapSOL": True,
                "computeUnitPriceMicroLamports": 1,
                "asLegacyTransaction": False
            }
            headers = {"Content-Type": "application/json"}
            async with httpx.AsyncClient() as client:
                response = await client.post(swap_url, json=payload, headers=headers)
                if response.status_code == 200:
                    swap_tx = response.json()
                    txn = base64.b64decode(swap_tx["swapTransaction"])
                    return VersionedTransaction.deserialize(txn)
                print(f"Swap fetch failed: {response.text}")
                return None
        except Exception as e:
            print(f"Swap error: {e}")
            return None

    def send_transaction(self, transaction: VersionedTransaction, keypair: Keypair):
        try:
            tx_sig = self.client.send_transaction(transaction, keypair)
            if isinstance(tx_sig, Signature):
                return str(tx_sig)
            elif isinstance(tx_sig, dict) and "result" in tx_sig:
                return tx_sig["result"]
            return None
        except Exception as e:
            print(f"Send transaction error: {e}")
            return None
