import base64
import json
import httpx
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solana.rpc.api import Client

class JupiterAggregatorClient:
    def __init__(self, rpc_url):
        self.rpc_url = rpc_url
        self.client = Client(rpc_url)

    async def get_quote(self, input_mint: Pubkey, output_mint: Pubkey, amount: int, slippage_bps: int = 100, user_pubkey: Pubkey = None):
        url = "https://quote-api.jup.ag/v6/quote"
        params = {
            "inputMint": str(input_mint),
            "outputMint": str(output_mint),
            "amount": str(amount),
            "slippageBps": str(slippage_bps),
        }
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, params=params)
                data = response.json()
                return data if data else None
        except Exception as e:
            print(f"[JUPITER] Quote error: {e}")
            return None

    async def get_swap_transaction(self, quote: dict, keypair: Keypair):
        swap_url = "https://quote-api.jup.ag/v6/swap"
        try:
            payload = {
                "userPublicKey": str(keypair.pubkey()),
                "wrapUnwrapSOL": True,
                "useSharedAccounts": False,
                "computeUnitPriceMicroLamports": 2000,
                "quoteResponse": quote,
            }

            print(f"[JUPITER] Swap request body:\n{json.dumps(payload, indent=2)}")

            async with httpx.AsyncClient() as client:
                response = await client.post(swap_url, json=payload)
                response.raise_for_status()
                swap_tx = response.json().get("swapTransaction")

                if not swap_tx:
                    print("[JUPITER] No transaction returned.")
                    return None

                swap_tx_bytes = base64.b64decode(swap_tx)
                return VersionedTransaction.from_bytes(swap_tx_bytes)

        except Exception as e:
            print(f"[JUPITER] Swap TX error: {e}")
            return None

    async def send_transaction(self, txn: VersionedTransaction, keypair: Keypair):
        try:
            raw_tx = txn.to_bytes()
            tx_sig = self.client.send_raw_transaction(raw_tx)
            return str(tx_sig)
        except Exception as e:
            print(f"[JUPITER] Send error: {e}")
            return None
