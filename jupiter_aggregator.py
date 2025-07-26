import base64
import json
import httpx
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.signature import Signature
from solana.rpc.api import Client
import os

class JupiterAggregatorClient:
    def __init__(self):
        self.client = Client(os.getenv("RPC_URL"))

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
                if response.status_code == 200:
                    return response.json()
                else:
                    print(f"Quote error: {response.text}")
                    return None
        except Exception as e:
            print(f"Quote exception: {e}")
            return None

    async def get_swap_transaction(self, user_public_key: str, quote_response: dict):
        url = "https://quote-api.jup.ag/v6/swap"
        payload = {
            "userPublicKey": user_public_key,
            "wrapUnwrapSOL": True,
            "computeUnitPriceMicroLamports": 10000,
            **quote_response  # include the quote directly
        }
        headers = {"Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, headers=headers, data=json.dumps(payload))
                if response.status_code == 200:
                    swap_json = response.json()
                    swap_txn_b64 = swap_json.get("swapTransaction")
                    if not swap_txn_b64:
                        print("Swap transaction missing in response")
                        return None
                    swap_txn_bytes = base64.b64decode(swap_txn_b64)
                    txn = VersionedTransaction.deserialize(swap_txn_bytes)
                    return txn
                else:
                    print(f"Swap error: {response.text}")
                    return None
        except Exception as e:
            print(f"Swap exception: {e}")
            return None

    def send_transaction(self, signed_txn: VersionedTransaction):
        try:
            sig = self.client.send_raw_transaction(signed_txn.serialize())
            return sig
        except Exception as e:
            print(f"Send txn exception: {e}")
            return None
