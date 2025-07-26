# =============================
# jupiter_aggregator.py — REST Jupiter Buy/Sell SDK (Fixed Wallet Public Key)
# =============================

import base64
import json
import httpx
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.signature import Signature
from solana.rpc.api import Client
import os
from dotenv import load_dotenv

load_dotenv()

RPC_URL = os.getenv("RPC_URL")
WALLET_PRIVATE_KEY = json.loads(os.getenv("PRIVATE_KEY"))

class JupiterAggregatorClient:
    def __init__(self, rpc_url):
        self.rpc_url = rpc_url
        self.client = Client(rpc_url)

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
                resp = await client.get(url)
                data = resp.json()
                return data["data"][0] if "data" in data and len(data["data"]) > 0 else None
        except Exception as e:
            print(f"[JupiterAggregator] Error fetching quote: {e}")
            return None

    async def get_swap_tx(self, quote: dict, user_pubkey: str):
        url = "https://quote-api.jup.ag/v6/swap"
        payload = {
            "quoteResponse": quote,
            "userPublicKey": user_pubkey,
            "wrapUnwrapSOL": True,
            "computeUnitPriceMicroLamports": 10000,
            "asLegacyTransaction": False
        }
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=payload)
                swap_tx = response.json()
                return swap_tx["swapTransaction"] if "swapTransaction" in swap_tx else None
        except Exception as e:
            print(f"[JupiterAggregator] Error getting swap transaction: {e}")
            return None

    def sign_and_send_tx(self, swap_tx_b64: str):
        try:
            keypair = Keypair.from_bytes(bytes(WALLET_PRIVATE_KEY))
            swap_tx_bytes = base64.b64decode(swap_tx_b64)
            tx = VersionedTransaction.deserialize(swap_tx_bytes)
            tx.sign([keypair])
            tx_sig = self.client.send_raw_transaction(tx.serialize(), opts={"skipPreflight": True, "maxRetries": 3})
            return str(tx_sig)
        except Exception as e:
            print(f"[JupiterAggregator] Error signing/sending tx: {e}")
            return None

    async def buy(self, input_mint: Pubkey, output_mint: Pubkey, amount: int):
        keypair = Keypair.from_bytes(bytes(WALLET_PRIVATE_KEY))
        user_pubkey = str(keypair.pubkey())

        quote = await self.get_quote(input_mint, output_mint, amount)
        if not quote:
            print("[JupiterAggregator] Quote not available.")
            return None

        swap_tx_b64 = await self.get_swap_tx(quote, user_pubkey)
        if not swap_tx_b64:
            print("[JupiterAggregator] Failed to get swap transaction.")
            return None

        tx_sig = self.sign_and_send_tx(swap_tx_b64)
        if tx_sig:
            print(f"[JupiterAggregator] Buy TX sent: {tx_sig}")
        return tx_sig
