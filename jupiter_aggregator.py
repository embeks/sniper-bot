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
from solana.transaction import Transaction, TransactionInstruction, AccountMeta
from spl.token.constants import TOKEN_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID
from solana.system_program import SYS_PROGRAM_ID
from spl.token.instructions import get_associated_token_address
from solana.publickey import PublicKey


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
        user_pubkey: Pubkey = None
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

    def _create_ata_if_missing(self, owner: PublicKey, mint: PublicKey, keypair: Keypair):
        ata = get_associated_token_address(owner, mint)
        res = self.client.get_account_info(ata)

        if res.value is None:
            logging.warning(f"[JUPITER] Creating missing ATA for {str(mint)}")

            ix = TransactionInstruction(
                keys=[
                    AccountMeta(pubkey=PublicKey(str(keypair.pubkey())), is_signer=True, is_writable=True),
                    AccountMeta(pubkey=ata, is_signer=False, is_writable=True),
                    AccountMeta(pubkey=owner, is_signer=False, is_writable=False),
                    AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
                    AccountMeta(pubkey=SYS_PROGRAM_ID, is_signer=False, is_writable=False),
                    AccountMeta(pubkey=TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
                    AccountMeta(pubkey=ASSOCIATED_TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
                ],
                program_id=ASSOCIATED_TOKEN_PROGRAM_ID,
                data=b"",
            )

            tx = Transaction()
            tx.add(ix)

            try:
                blockhash = self.client.get_latest_blockhash()["result"]["value"]["blockhash"]
                tx.recent_blockhash = str(blockhash)
                tx.fee_payer = PublicKey(str(keypair.pubkey()))
                tx.sign([keypair])
                result = self.client.send_raw_transaction(
                    bytes(tx),
                    opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed)
                )
                logging.info(f"[JUPITER] ATA Creation TX: {result}")
            except Exception as e:
                logging.error(f"[JUPITER] Failed to create ATA: {e}")

    async def get_swap_transaction(self, quote_response: dict, keypair: Keypair):
        try:
            token_accounts = await self._get_token_accounts(str(keypair.pubkey()))
            if not token_accounts:
                output_mint = PublicKey(quote_response["outputMint"])
                logging.warning(f"[JUPITER] No token accounts found — adding fallback for {quote_response['outputMint']}")
                self._create_ata_if_missing(PublicKey(str(keypair.pubkey())), output_mint, keypair)

            swap_url = f"{self.base_url}/swap"
            body = {
                "userPublicKey": str(keypair.pubkey()),
                "wrapUnwrapSOL": True,
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
                        logging.error(f"❌ Jupiter quote returned no swapTransaction for {quote_response['outputMint']}")
                        self._send_telegram_debug(f"❌ Jupiter quote returned no swapTransaction for {quote_response['outputMint']}")
                        return None
                    return tx_base64
                else:
                    logging.error(f"[JUPITER] Swap error: HTTP {response.status_code}")
                    return None
        except Exception as e:
            logging.exception(f"[JUPITER] Swap exception: {e}")
            return None

    def build_swap_transaction(self, swap_tx_base64: str, keypair: Keypair):
        """
        Decode a base64‑encoded swap transaction returned from the Jupiter API
        and produce a serialized transaction ready for submission.

        This method performs several steps:

        1. Validate that a non‑empty base64 string was supplied.
        2. Attempt to decode the string into raw bytes, logging the length and
           a short preview of the payload for debugging.  If decoding fails,
           a helpful message is logged and ``None`` is returned.
        3. Attempt to deserialize the bytes into a :class:`VersionedTransaction` for
           debugging and potential signing.  If deserialization fails, the raw
           bytes are still returned.
        4. Optionally sign the transaction if the ``solders.message`` module is
           available and the user's pubkey is present in the account keys.  The
           signature replaces the existing placeholder signature.  Any signing
           errors are logged but do not prevent returning the raw bytes.

        The function always returns the raw serialized transaction bytes.  Downstream
        code can submit these bytes directly via RPC.  If any irrecoverable
        error occurs before serialization, ``None`` is returned.
        """
        try:
            if not swap_tx_base64 or not isinstance(swap_tx_base64, str):
                logging.error("[JUPITER] Empty or invalid swap transaction string.")
                return None

            # Base64 decode
            try:
                tx_bytes = base64.b64decode(swap_tx_base64)
            except Exception as decode_err:
                logging.exception(f"[JUPITER] Failed to decode swap transaction: {decode_err}")
                self._send_telegram_debug(
                    f"❌ Base64 decode failed: {type(decode_err).__name__}: {decode_err}\n"
                    f"Input (first 100 chars): `{swap_tx_base64[:100]}`"
                )
                return None

            logging.info(f"[JUPITER] Decoded {len(tx_bytes)} bytes from swap transaction.")
            preview_hex = tx_bytes[:32].hex()
            logging.debug(f"[JUPITER] First 32 decoded bytes (hex): {preview_hex}")

            # Attempt to deserialize for debug/signing
            versioned_tx = None
            try:
                versioned_tx = VersionedTransaction.from_bytes(tx_bytes)
                logging.info("[JUPITER] VersionedTransaction deserialized successfully.")
            except Exception as deser_err:
                logging.warning(
                    f"[JUPITER] Could not deserialize VersionedTransaction: {deser_err}. "
                    "Continuing with raw bytes."
                )

            # If we could deserialize and solders.message is available, try to sign
            if versioned_tx:
                try:
                    from solders.message import to_bytes_versioned  # type: ignore

                    message_bytes = to_bytes_versioned(versioned_tx.message)
                    signature = keypair.sign_message(message_bytes)

                    # Only replace signature if our key is present
                    try:
                        sig_index = next(
                            i for i, pk in enumerate(versioned_tx.message.account_keys)
                            if pk == keypair.pubkey()
                        )
                    except StopIteration:
                        sig_index = None

                    if sig_index is not None:
                        sigs = list(versioned_tx.signatures)
                        sigs[sig_index] = signature
                        versioned_tx.signatures = sigs
                        tx_bytes = bytes(versioned_tx)
                        logging.info(
                            f"[JUPITER] Swap transaction signed. Updated signature at index {sig_index}."
                        )
                    else:
                        logging.warning(
                            "[JUPITER] Wallet pubkey not found among account keys; cannot attach signature. "
                            "Proceeding with original signatures."
                        )
                except ImportError:
                    logging.warning(
                        "[JUPITER] solders.message.to_bytes_versioned could not be imported; "
                        "returning unsigned serialized bytes."
                    )
                except Exception as sign_err:
                    logging.exception(f"[JUPITER] Error while signing swap transaction: {sign_err}")
                    self._send_telegram_debug(
                        f"⚠️ Error signing transaction: {type(sign_err).__name__}: {sign_err}\n"
                        "Proceeding with partially signed transaction."
                    )

            # Return the raw (possibly re-serialized) bytes
            return tx_bytes

        except Exception as outer_err:
            logging.exception(f"[JUPITER] Unexpected error in build_swap_transaction: {outer_err}")
            self._send_telegram_debug(
                f"❌ Unexpected error in build_swap_transaction: {type(outer_err).__name__}: {outer_err}"
            )
            return None

    def send_transaction(self, tx, keypair: Keypair | None = None) -> str | None:
        """
        Submit a serialized transaction to the RPC and return its signature.

        This helper accepts either raw transaction bytes or a `VersionedTransaction`
        instance.  It logs unexpected results and attempts to normalise the
        response across different versions of solana‑py.  The optional
        ``keypair`` parameter is ignored but kept for backwards compatibility.
        """
        try:
            # Accept either bytes or VersionedTransaction.  For anything else,
            # attempt to coerce into bytes.
            if isinstance(tx, (bytes, bytearray)):
                raw_tx_bytes = bytes(tx)
            else:
                # If a VersionedTransaction is provided, its ``bytes()``
                # representation yields the fully serialized transaction.
                try:
                    raw_tx_bytes = bytes(tx)  # type: ignore[arg-type]
                except Exception:
                    logging.error("[JUPITER] send_transaction received an unsupported tx type")
                    return None

            # Warn on suspiciously small transactions but still attempt to send.
            if len(raw_tx_bytes) < 64:
                logging.warning(f"[JUPITER] Raw TX very short ({len(raw_tx_bytes)} bytes); sending anyway.")

            # Submit the transaction.  send_raw_transaction returns a dict-like
            # structure in older solana‑py versions; in newer versions it may
            # raise an exception on RPC error.  We normalise both cases.
            try:
                result = self.client.send_raw_transaction(
                    raw_tx_bytes,
                    opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed)
                )
            except Exception as rpc_exc:
                # If an exception is thrown, report and abort
                self._send_telegram_debug(f"❌ Send error: {type(rpc_exc).__name__}: {rpc_exc}")
                return None

            # result may be a dict or an object with `.get`/index semantics
            signature: str | None = None
            if isinstance(result, dict):
                # solana‑py <=0.30 returns { 'result': <sig> } or {'error': ...}
                if result.get("result"):
                    signature = str(result["result"])
                elif result.get("error"):
                    err_json = json.dumps(result["error"], indent=2)
                    self._send_telegram_debug(f"❌ TX Error:\n```{err_json}```")
                    return None
                else:
                    self._send_telegram_debug(f"❌ Unexpected send result:\n```{result}```")
                    return None
            else:
                # Newer versions of solana‑py return an RPCResponse object with
                # 'result' attribute or behave like a dict when indexed
                try:
                    # Attempt to index like a dict
                    signature = str(result["result"])  # type: ignore[index]
                except Exception:
                    try:
                        # Fall back to treating the entire response as the signature
                        signature = str(result)
                    except Exception:
                        self._send_telegram_debug(f"❌ Unrecognised send result type: {result}")
                        return None

            if not signature:
                self._send_telegram_debug(f"❌ TX failed — No tx hash returned:\n```{result}```")
                return None

            logging.info(f"[JUPITER] TX sent: {signature}")
            return signature

        except Exception as e:
            # Catch any other unexpected exceptions
            self._send_telegram_debug(f"❌ Send error: {type(e).__name__}: {e}")
            return None

    def _send_telegram_debug(self, message: str):
        try:
            token = os.getenv("TELEGRAM_BOT_TOKEN")
            chat_id = os.getenv("TELEGRAM_CHAT_ID")
            if not token or not chat_id:
                return
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
            httpx.post(url, json=payload, timeout=5)
        except Exception as e:
            logging.error(f"[JUPITER] Failed to send Telegram debug message: {e}")

