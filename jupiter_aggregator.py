import json
import base64
import httpx
import logging
import os
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solana.rpc.api import Client
from solana.rpc.types import TxOpts
from solana.rpc.commitment import Confirmed


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
                    logging.error(f"[JUPITER] Quote HTTP {response.status_code} - {response.text}")
                    return None
        except Exception as e:
            logging.exception(f"[JUPITER] Quote error: {e}")
            return None

    async def get_swap_transaction(self, quote_response: dict, keypair: Keypair):
        try:
            swap_url = f"{self.base_url}/swap"
            body = {
                "userPublicKey": str(keypair.pubkey()),
                "wrapUnwrapSOL": True,
                "useSharedAccounts": False,
                "computeUnitPriceMicroLamports": 2000,
                "quoteResponse": quote_response
            }

            logging.info(f"[JUPITER] Swap request:\n{json.dumps(body, indent=2)}")
            headers = {"Content-Type": "application/json"}

            async with httpx.AsyncClient() as client:
                response = await client.post(swap_url, json=body, headers=headers)

                logging.info(f"[JUPITER] Swap response {response.status_code}: {response.text}")
                if response.status_code == 200:
                    data = response.json()
                    tx_base64 = data.get("swapTransaction")
                    if not tx_base64:
                        logging.error("[JUPITER] No 'swapTransaction' field in response")
                        return None
                    return tx_base64
                else:
                    logging.error(f"[JUPITER] Swap error: HTTP {response.status_code}")
                    return None
        except Exception as e:
            logging.exception(f"[JUPITER] Swap exception: {e}")
            return None

    def build_swap_transaction(self, swap_tx_base64: str, keypair: Keypair):
        try:
            if not swap_tx_base64:
                raise ValueError("swap_tx_base64 is empty or None")

            logging.info(f"[JUPITER] Raw swap_tx_base64 (first 100 chars): {swap_tx_base64[:100]}")

            try:
                tx_bytes = base64.b64decode(swap_tx_base64)
                logging.info(f"[JUPITER] Base64 decoded. Length: {len(tx_bytes)}")
            except Exception as decode_err:
                logging.exception("[JUPITER] Base64 decode failed")
                self._send_telegram_debug(f"❌ Base64 decode failed: {decode_err}")
                return None

            try:
                tx = VersionedTransaction.from_bytes(tx_bytes)
                logging.info(f"[JUPITER] Transaction version: {tx.version}")
                return tx
            except Exception as deser_err:
                logging.exception("[JUPITER] Deserialization failed")
                self._send_telegram_debug(f"❌ Deserialization failed: {deser_err}")
                return None

        except Exception as e:
            logging.exception("[JUPITER] Unexpected error in build_swap_transaction")
            self._send_telegram_debug(f"❌ Unexpected swapTransaction error: {e}")
            return None

    def send_transaction(self, signed_tx: VersionedTransaction, keypair: Keypair):
        try:
            raw_tx_bytes = bytes(signed_tx)  # ✅ Correct usage for solders

            result = self.client.send_raw_transaction(
                raw_tx_bytes,
                opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed)
            )

            logging.info(f"[JUPITER] TX Raw Result: {result}")

            if "error" in result:
                error_info = json.dumps(result["error"], indent=2)
                self._send_telegram_debug(f"❌ TX Error:\n```{error_info}```")
                return None

            if "result" not in result or not result["result"]:
                self._send_telegram_debug(f"❌ TX failed — No tx hash returned:\n```{result}```")
                return None

            return str(result["result"])

        except Exception as e:
            err_msg = f"❌ Send error:\n{type(e).__name__}: {e}"
            logging.exception(err_msg)
            self._send_telegram_debug(err_msg)
            return None

    def _send_telegram_debug(self, message: str):
        try:
            token = os.getenv("TELEGRAM_BOT_TOKEN")
            chat_id = os.getenv("TELEGRAM_CHAT_ID")
            if not token or not chat_id:
                logging.warning("[JUPITER] Telegram credentials not set in env")
                return
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
            httpx.post(url, json=payload, timeout=5)
        except Exception as e:
            logging.error(f"[JUPITER] Failed to send Telegram debug message: {e}")
