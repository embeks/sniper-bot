# =============================
# jupiter_aggregator.py — Jupiter V6 REST Client
# =============================

import json
import logging
import httpx
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solana.rpc.api import Client

class JupiterAggregatorClient:
    def __init__(self, rpc_url: str, base_url: str = "https://quote-api.jup.ag/v6"):
        self.client = Client(rpc_url)
        self.base_url = base_url

    async def get_quote(self, input_mint: Pubkey, output_mint: Pubkey, amount: int, slippage_bps: int, user_pubkey: Pubkey):
        url = (
            f"{self.base_url}/quote"
            f"?inputMint={str(input_mint)}"
            f"&outputMint={str(output_mint)}"
            f"&amount={amount}"
            f"&slippageBps={slippage_bps}"
            f"&userPublicKey={str(user_pubkey)}"
        )
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url)
                logging.info(f"[JUPITER] Quote URL: {response.url}")
                response.raise_for_status()
                data = response.json()
                if "data" not in data or not data["data"]:
                    logging.warning(f"[JUPITER] No quote returned:\n{json.dumps(data, indent=2)}")
                    return None
                return data["data"][0]
        except Exception as e:
            logging.error(f"[JUPITER] Quote fetch failed: {e}")
            return None

    async def get_swap_transaction(self, route: dict, keypair: Keypair):
        swap_url = f"{self.base_url}/swap"
        payload = {
            "route": route,
            "userPublicKey": str(keypair.pubkey()),
            "wrapUnwrapSOL": True,
            "dynamicSlippage": True
        }
        try:
            async with httpx.AsyncClient() as client:
                res = await client.post(swap_url, json=payload)
                logging.info(f"[JUPITER] Swap request URL: {swap_url}")
                res.raise_for_status()
                tx = res.json().get("swapTransaction")
                if not tx:
                    logging.warning(f"[JUPITER] No transaction returned:\n{res.text}")
                    return None
                return tx
        except Exception as e:
            logging.error(f"[JUPITER] Swap transaction fetch failed: {e}")
            return None

    def build_swap_transaction(self, swap_tx_base64: str, keypair: Keypair):
        try:
            tx_bytes = bytes.fromhex(swap_tx_base64)
            versioned_tx = VersionedTransaction.from_bytes(tx_bytes)
            versioned_tx.sign([keypair])
            return versioned_tx.serialize()
        except Exception as e:
            logging.error(f"[JUPITER] Failed to build transaction: {e}")
            return None

    def send_transaction(self, tx_bytes: bytes, keypair: Keypair):
        try:
            sig = self.client.send_raw_transaction(tx_bytes)
            return str(sig)
        except Exception as e:
            logging.error(f"[JUPITER] Failed to send transaction: {e}")
            return None
