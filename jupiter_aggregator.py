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
from spl.token.instructions import get_associated_token_address, create_associated_token_account  # Fixed here ✅
from solana.transaction import Transaction

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
        only_direct_routes=True
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
                    logging.info(f"[JUPITER] Quote response OK: {response.json()}")
                    return response.json()
                else:
                    logging.error(f"[JUPITER] Quote HTTP {response.status_code} - {response.text}")
                    return None
        except Exception as e:
            logging.exception(f"[JUPITER] Quote error: {e}")
            return None

    async def _get_token_accounts(self, wallet_address: str):
        try:
            url = f"https://quote-api.jup.ag/v6/token-accounts/{wallet_address}"
            async with httpx.AsyncClient() as client:
                response = await client.get(url)
                if response.status_code == 200:
                    return response.json()
                else:
                    logging.error(f"[JUPITER] Failed to fetch token accounts: {response.text}")
                    return []
        except Exception as e:
            logging.exception("[JUPITER] Error getting token accounts")
            return []

    def _create_ata_if_missing(self, owner: Pubkey, mint: Pubkey, keypair: Keypair):
        ata = get_associated_token_address(owner, mint)
        res = self.client.get_account_info(ata)

        if res.value is None:
            logging.warning(f"[JUPITER] Creating missing ATA for {str(mint)}")
            ix = create_associated_token_account(
                payer=owner,
                owner=owner,
                mint=mint
            )
            tx = Transaction()
            tx.add(ix)
            try:
                result = self.client.send_transaction(tx, keypair, opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed))
                logging.info(f"[JUPITER] ATA Creation TX: {result}")
            except Exception as e:
                logging.error(f"[JUPITER] Failed to create ATA: {e}")

    async def get_swap_transaction(self, quote_response: dict, keypair: Keypair):
        try:
            token_accounts = await self._get_token_accounts(str(keypair.pubkey()))
            if not token_accounts:
                output_mint = Pubkey.from_string(quote_response["outputMint"])
                logging.warning(f"[JUPITER] No token accounts found — adding fallback for {quote_response['outputMint']}")
                self._create_ata_if_missing(keypair.pubkey(), output_mint, keypair)
                token_accounts = await self._get_token_accounts(str(keypair.pubkey()))  # REFRESHED ✅

            swap_url = f"{self.base_url}/swap"
            body = {
                "userPublicKey": str(keypair.pubkey()),
                "wrapUnwrapSOL": False,
                "useSharedAccounts": True,
                "computeUnitPriceMicroLamports": 2000,
                "userTokenAccounts": token_accounts,
                "quoteResponse": json.loads(json.dumps(quote_response))
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

            logging.warning(f"[JUPITER] swapTransaction length: {len(swap_tx_base64)}")
            logging.warning(f"[JUPITER] First 100 chars of swapTransaction:\n{swap_tx_base64[:100]}")

            try:
                tx_bytes = base64.b64decode(swap_tx_base64)
                logging.warning(f"[JUPITER] Decoded tx_bytes length: {len(tx_bytes)}")
                logging.warning(f"[JUPITER] First 20 decoded bytes:\n{tx_bytes[:20]}")
                if len(tx_bytes) < 400 or tx_bytes.startswith(b'\x01\x00\x00'):
                    self._send_telegram_debug(
                        f"❌ Decoded tx looks malformed.\nLength: {len(tx_bytes)} bytes\nFirst 20 bytes: `{tx_bytes[:20]}`\n```{swap_tx_base64[:400]}```"
                    )
                    return None
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
            raw_tx_bytes = bytes(signed_tx)

            if len(raw_tx_bytes) < 400:
                logging.error(f"[JUPITER] Raw tx too short: {len(raw_tx_bytes)} bytes")
                self._send_telegram_debug(f"❌ Raw TX too short: {len(raw_tx_bytes)} bytes. Aborting send.")
                return None

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
