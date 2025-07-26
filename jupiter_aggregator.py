import base64
import json
import httpx
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.signature import Signature
from solana.rpc.api import Client
from solana.rpc.types import TxOpts
import os

class JupiterAggregatorClient:
    def __init__(self):
        self.client = Client(os.environ["RPC_URL"])

    async def get_quote(self, input_mint: Pubkey, output_mint: Pubkey, amount: int, user_public_key: str, slippage_bps: int = 100):
        url = (
            f"https://quote-api.jup.ag/v6/quote"
            f"?inputMint={str(input_mint)}"
            f"&outputMint={str(output_mint)}"
            f"&amount={amount}"
            f"&slippageBps={slippage_bps}"
            f"&userPublicKey={user_public_key}"
        )
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url)
                response.raise_for_status()
                return response.json()
        except Exception as e:
            print(f"Error fetching Jupiter quote: {e}")
            return None

    async def get_swap_transaction(self, route: dict, user_public_key: str):
        swap_url = "https://quote-api.jup.ag/v6/swap"
        body = {
            "route": route,
            "userPublicKey": user_public_key,
            "wrapUnwrapSol": True,
            "feeAccount": None
        }
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(swap_url, json=body)
                response.raise_for_status()
                result = response.json()
                if 'swapTransaction' not in result:
                    print("Jupiter swap response missing 'swapTransaction'")
                    return None
                return result['swapTransaction']
        except Exception as e:
            print(f"Error fetching Jupiter swap transaction: {e}")
            return None

    def send_swap_transaction(self, swap_tx_base64: str, payer: Keypair):
        try:
            tx_bytes = base64.b64decode(swap_tx_base64)
            versioned_tx = VersionedTransaction.from_bytes(tx_bytes)
            recent_blockhash = self.client.get_latest_blockhash()["result"]["value"]["blockhash"]
            versioned_tx.message.recent_blockhash = recent_blockhash
            versioned_tx.sign([payer])
            tx_sig = self.client.send_raw_transaction(versioned_tx.serialize(), opts=TxOpts(skip_preflight=True, preflight_commitment="processed"))
            return tx_sig["result"]
        except Exception as e:
            print(f"Error sending swap transaction: {e}")
            return None
