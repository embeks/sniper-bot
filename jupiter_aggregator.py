import json
import base64
import httpx
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solana.rpc.api import Client
from solana.rpc.types import TxOpts


class JupiterAggregatorClient:
    def __init__(self, rpc_url):
        self.rpc_url = rpc_url
        self.client = Client(rpc_url)
        self.base_url = "https://quote-api.jup.ag/v6"

    async def get_quote(
        self,
        input_mint: Pubkey,
        output_mint: Pubkey,
        amount: int,
        slippage_bps: int = 100,
        user_pubkey: Pubkey = None,
        only_direct_routes=False
    ):
        try:
            url = f"{self.base_url}/quote"
            params = {
                "inputMint": str(input_mint),
                "outputMint": str(output_mint),
                "amount": amount,
                "slippageBps": slippage_bps,
                "swapMode": "ExactIn"
            }
            if user_pubkey:
                params["userPublicKey"] = str(user_pubkey)
            if only_direct_routes:
                params["onlyDirectRoutes"] = "true"

            async with httpx.AsyncClient() as client:
                response = await client.get(url, params=params)
                if response.status_code == 200:
                    return response.json()
                else:
                    print(f"[JUPITER] Quote HTTP {response.status_code} - {response.text}")
                    return None
        except Exception as e:
            print(f"[JUPITER] Quote error: {e}")
            return None

    async def get_swap_transaction(self, quote: dict, keypair: Keypair):
        try:
            swap_url = f"{self.base_url}/swap"
            body = {
                "userPublicKey": str(keypair.pubkey()),
                "wrapUnwrapSOL": True,
                "useSharedAccounts": False,  # use True only if you're managing shared wallets
                "computeUnitPriceMicroLamports": 2000,
                "quoteResponse": quote
            }

            print(f"[JUPITER] Swap request body:\n{json.dumps(body, indent=2)}")
            headers = {"Content-Type": "application/json"}

            async with httpx.AsyncClient() as client:
                response = await client.post(swap_url, json=body, headers=headers)

                print(f"[JUPITER] Swap response {response.status_code}: {response.text}")
                if response.status_code == 200:
                    data = response.json()
                    tx_base64 = data.get("swapTransaction")
                    if not tx_base64:
                        print("[JUPITER] No transaction in response")
                        return None
                    return tx_base64
                else:
                    return None
        except Exception as e:
            print(f"[JUPITER] Swap error: {e}")
            return None

    def build_swap_transaction(self, tx_base64: str, keypair: Keypair):
        try:
            tx_bytes = base64.b64decode(tx_base64)
            tx = VersionedTransaction.from_bytes(tx_bytes)
            return tx  # Already signed — do not sign again
        except Exception as e:
            print(f"[JUPITER] Transaction build error: {e}")
            return None

    def send_transaction(self, signed_tx: VersionedTransaction, keypair: Keypair):
        try:
            raw_tx = signed_tx.serialize()
            result = self.client.send_raw_transaction(raw_tx, opts=TxOpts(skip_preflight=True))
            return str(result.get("result"))
        except Exception as e:
            print(f"[JUPITER] Send error: {e}")
            return None
