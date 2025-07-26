import base64
import json
import httpx
import logging
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solana.rpc.api import Client
from solana.rpc.types import TxOpts
from dotenv import load_dotenv
import os

load_dotenv()
JUPITER_BASE_URL = os.getenv("JUPITER_BASE_URL", "https://quote-api.jup.ag")

class JupiterAggregatorClient:
    def __init__(self, rpc_url):
        self.rpc_url = rpc_url
        self.client = Client(rpc_url)

    async def get_quote(self, input_mint: Pubkey, output_mint: Pubkey, amount: int, slippage_bps: int = 100, user_pubkey: Pubkey = None, only_direct_routes: bool = False):
        url = f"{JUPITER_BASE_URL}/v6/quote"
        params = {
            "inputMint": str(input_mint),
            "outputMint": str(output_mint),
            "amount": amount,
            "slippageBps": slippage_bps,
            "onlyDirectRoutes": str(only_direct_routes).lower()
        }
        if user_pubkey:
            params["userPublicKey"] = str(user_pubkey)

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, params=params)
                if response.status_code != 200:
                    logging.warning(f"[JUPITER] Quote request failed: {response.status_code} {response.text}")
                    return None

                data = response.json()
                if not data or "routePlan" not in data:
                    logging.warning(f"[JUPITER] No quote returned:\n{json.dumps(data, indent=2)}")
                    return None

                logging.info(f"[JUPITER] Quote success:\n{json.dumps(data, indent=2)}")
                return data
        except Exception as e:
            logging.exception(f"[JUPITER] Exception in get_quote: {e}")
            return None

    async def get_swap_transaction(self, quote: dict, user_keypair: Keypair):
        swap_url = f"{JUPITER_BASE_URL}/v6/swap"
        payload = {
            "route": quote,
            "userPublicKey": str(user_keypair.pubkey()),
            "wrapUnwrapSOL": True,
            "useSharedAccounts": True,
            "feeAccount": None,
            "asLegacyTransaction": False
        }

        try:
            logging.info(f"[JUPITER] Sending swap request with payload:\n{json.dumps(payload, indent=2)}")

            async with httpx.AsyncClient() as client:
                res = await client.post(swap_url, json=payload)
                logging.info(f"[JUPITER] Swap response {res.status_code}:\n{res.text}")

                if res.status_code != 200:
                    return None

                data = res.json()
                if "swapTransaction" not in data:
                    logging.warning("[JUPITER] No swapTransaction in response.")
                    return None

                return base64.b64decode(data["swapTransaction"])
        except Exception as e:
            logging.exception(f"[JUPITER] Exception in get_swap_transaction: {e}")
            return None

    def build_swap_transaction(self, txn_bytes: bytes, user_keypair: Keypair):
        try:
            txn = VersionedTransaction.deserialize(txn_bytes)
            return txn
        except Exception as e:
            logging.exception(f"[JUPITER] Failed to deserialize swap transaction: {e}")
            return None

    def send_transaction(self, txn: VersionedTransaction, user_keypair: Keypair):
        try:
            sig = self.client.send_transaction(txn, user_keypair, opts=TxOpts(skip_preflight=True, preflight_commitment="processed"))
            return str(sig)
        except Exception as e:
            logging.exception(f"[JUPITER] Send transaction failed: {e}")
            return None
