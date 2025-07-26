import json
import base64
import httpx
import logging
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solana.rpc.api import Client
from solana.transaction import Transaction
from solana.rpc.types import TxOpts
from solders.signature import Signature

class JupiterAggregatorClient:
    def __init__(self, rpc_url):
        self.rpc_url = rpc_url
        self.client = Client(rpc_url)

    async def get_quote(self, input_mint: Pubkey, output_mint: Pubkey, amount: int, slippage_bps: int = 100, user_pubkey: Pubkey = None):
        url = (
            "https://quote-api.jup.ag/v6/quote"
            f"?inputMint={str(input_mint)}"
            f"&outputMint={str(output_mint)}"
            f"&amount={amount}"
            f"&slippageBps={slippage_bps}"
        )
        if user_pubkey:
            url += f"&userPublicKey={str(user_pubkey)}"

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url)
                response.raise_for_status()
                return response.json()
        except Exception as e:
            logging.error(f"[JUPITER] Failed to get quote: {e}")
            return None

    async def get_swap_transaction(self, quote_response: dict, user_pubkey: Pubkey):
        url = "https://quote-api.jup.ag/v6/swap"
        payload = {
            "userPublicKey": str(user_pubkey),
            "wrapUnwrapSOL": True,
            "useSharedAccounts": False,
            "computeUnitPriceMicroLamports": 2000,
            "quoteResponse": quote_response,
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                swap_data = response.json()
                if "swapTransaction" in swap_data:
                    return base64.b64decode(swap_data["swapTransaction"])
                else:
                    logging.error(f"[JUPITER] No swapTransaction field in response: {swap_data}")
                    return None
        except Exception as e:
            logging.error(f"[JUPITER] Failed to get swap transaction: {e}")
            return None

    def send_transaction(self, tx_bytes: bytes, keypair: Keypair) -> Signature | None:
        try:
            tx = Transaction.deserialize(tx_bytes)
            tx.sign([keypair])
            tx_sig = self.client.send_transaction(tx, keypair, opts=TxOpts(skip_confirmation=False, preflight_commitment="confirmed"))
            return tx_sig["result"]
        except Exception as e:
            logging.error(f"[JUPITER] Send error: {e}")
            return None
