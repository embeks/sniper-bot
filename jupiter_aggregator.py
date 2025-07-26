import httpx
import logging
from solders.pubkey import Pubkey

JUPITER_QUOTE_API = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_API = "https://quote-api.jup.ag/v6/swap"

class JupiterAggregatorClient:
    def __init__(self, rpc_url):
        self.rpc_url = rpc_url

    async def get_quote(
        self,
        input_mint: Pubkey,
        output_mint: Pubkey,
        amount: int,
        slippage_bps: int = 100,
        user_pubkey: Pubkey = None,
        only_direct_routes: bool = False,
    ) -> dict | None:
        params = {
            "inputMint": str(input_mint),
            "outputMint": str(output_mint),
            "amount": amount,
            "slippageBps": slippage_bps,
            "userPublicKey": str(user_pubkey),
            "onlyDirectRoutes": str(only_direct_routes).lower()
        }

        try:
            logging.info(f"[JUPITER DEBUG PARAMS] {params}")
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(JUPITER_QUOTE_API, params=params)
                logging.info(f"[JUPITER] Quote URL: {response.url}")

            if response.status_code != 200:
                logging.error(f"[JUPITER] Quote failed: {response.status_code} - {response.text}")
                return None

            data = response.json()
            if not data.get("data"):
                logging.warning(f"[JUPITER] No quote found for input={input_mint}, output={output_mint}, amount={amount}")
                return None

            return data["data"][0]  # Best route

        except Exception as e:
            logging.error(f"[JUPITER] Exception getting quote: {e}")
            return None

    async def get_swap_transaction(self, route: dict) -> str | None:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(JUPITER_SWAP_API, json=route)

            if response.status_code != 200:
                logging.error(f"[JUPITER] Swap failed: {response.status_code} - {response.text}")
                return None

            swap_data = response.json()
            return swap_data.get("swapTransaction")

        except Exception as e:
            logging.error(f"[JUPITER] Exception getting swap transaction: {e}")
            return None

    def build_swap_transaction(self, tx_base64: str, keypair) -> bytes | None:
        try:
            from base64 import b64decode
            from solders.transaction import VersionedTransaction

            tx_bytes = b64decode(tx_base64)
            tx = VersionedTransaction.from_bytes(tx_bytes)
            tx.sign([keypair])
            return bytes(tx)

        except Exception as e:
            logging.error(f"[JUPITER] Error building swap transaction: {e}")
            return None
