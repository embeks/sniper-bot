import base64
import json
import httpx
import logging
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.signature import Signature
from solana.rpc.api import Client
from solana.rpc.types import TxOpts
import os

JUPITER_QUOTE_API = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_API = "https://quote-api.jup.ag/v6/swap"

class JupiterAggregatorClient:
    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url
        self.client = Client(rpc_url)

    async def get_quote(
        self,
        input_mint: Pubkey,
        output_mint: Pubkey,
        amount: int,
        slippage_bps: int,
        user_pubkey: Pubkey,
        only_direct_routes: bool = False
    ) -> dict | None:
        params = {
            "inputMint": str(input_mint),
            "outputMint": str(output_mint),
            "amount": amount,
            "slippageBps": slippage_bps,
            "userPublicKey": str(user_pubkey),
            "onlyDirectRoutes": str(only_direct_routes).lower(),
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(JUPITER_QUOTE_API, params=params)

            logging.info(f"[JUPITER] Quote URL: {response.url}")

            if response.status_code != 200:
                logging.error(f"[JUPITER] Quote failed: {response.status_code} — {response.text}")
                return None

            data = response.json()
            if not data.get("data"):
                logging.warning(f"[JUPITER] No quote found for input={input_mint}, output={output_mint}, amount={amount}")
                return None

            logging.info(f"[JUPITER] ✅ Quote received for {output_mint} — route found.")
            return data["data"][0]

        except Exception as e:
            logging.exception(f"[JUPITER] Exception in get_quote: {e}")
            return None

    async def get_swap_transaction(self, route: dict, user_wallet: Keypair) -> bytes | None:
        payload = {
            "route": route,
            "userPublicKey": str(user_wallet.pubkey()),
            "wrapUnwrapSOL": True,
            "dynamicSlippage": True,
        }
        headers = {"Content-Type": "application/json"}

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(JUPITER_SWAP_API, json=payload, headers=headers)

            if response.status_code != 200:
                logging.error(f"[JUPITER] Swap failed: {response.status_code} — {response.text}")
                return None

            swap_data = response.json()
            if "swapTransaction" not in swap_data:
                logging.error(f"[JUPITER] No swapTransaction returned: {json.dumps(swap_data, indent=2)}")
                return None

            logging.info("[JUPITER] ✅ Swap transaction built successfully.")
            return base64.b64decode(swap_data["swapTransaction"])

        except Exception as e:
            logging.exception(f"[JUPITER] Exception in get_swap_transaction: {e}")
            return None

    def send_transaction(self, raw_txn_bytes: bytes, user_wallet: Keypair) -> str | None:
        try:
            txn = VersionedTransaction.deserialize(raw_txn_bytes)
            txn.sign([user_wallet])
            serialized_txn = base64.b64encode(txn.serialize()).decode("utf-8")
            tx_sig = self.client.send_raw_transaction(
                serialized_txn,
                opts=TxOpts(skip_preflight=True, preflight_commitment="processed")
            )
            logging.info(f"[JUPITER] ✅ Transaction sent: {tx_sig['result']}")
            return tx_sig["result"]

        except Exception as e:
            logging.exception(f"[JUPITER] Exception in send_transaction: {e}")
            return None
