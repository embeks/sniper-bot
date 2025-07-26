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
    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url
        self.client = Client(rpc_url)

    async def get_quote(self, input_mint: Pubkey, output_mint: Pubkey, amount: int, slippage_bps: int, user_pubkey: Pubkey):
        url = (
            f"https://quote-api.jup.ag/v6/quote?"
            f"inputMint={str(input_mint)}"
            f"&outputMint={str(output_mint)}"
            f"&amount={amount}"
            f"&slippageBps={slippage_bps}"
            f"&userPublicKey={str(user_pubkey)}"
        )
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url)
                response.raise_for_status()
                quote = response.json()
                if "data" not in quote or not quote["data"]:
                    raise ValueError(f"No valid quote routes returned: {json.dumps(quote, indent=2)}")
                return quote["data"][0]
        except Exception as e:
            raise RuntimeError(f"Failed to fetch Jupiter quote: {str(e)}")

    async def get_swap_transaction(self, route: dict, user_wallet: Keypair):
        url = "https://quote-api.jup.ag/v6/swap"
        payload = {
            "route": route,
            "userPublicKey": str(user_wallet.pubkey()),
            "wrapUnwrapSOL": True,
            "dynamicSlippage": True,
        }
        headers = {"Content-Type": "application/json"}

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                swap_txn = response.json()
                if "swapTransaction" not in swap_txn:
                    raise ValueError(f"Swap transaction failed: {json.dumps(swap_txn, indent=2)}")
                return base64.b64decode(swap_txn["swapTransaction"])
        except Exception as e:
            raise RuntimeError(f"Failed to fetch swap transaction: {str(e)}")

    def send_transaction(self, raw_txn_bytes: bytes, user_wallet: Keypair):
        try:
            txn = VersionedTransaction.deserialize(raw_txn_bytes)
            txn.sign([user_wallet])
            serialized_txn = base64.b64encode(txn.serialize()).decode("utf-8")
            tx_sig = self.client.send_raw_transaction(serialized_txn, opts=TxOpts(skip_preflight=True, preflight_commitment="processed"))
            return tx_sig["result"]
        except Exception as e:
            raise RuntimeError(f"Failed to sign or send transaction: {str(e)}")
