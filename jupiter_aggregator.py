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
        Decode a base64‐encoded swap transaction returned from the Jupiter API and
        produce a signed ``VersionedTransaction`` ready for submission.  This method
        performs several steps:

        1. Validate that a non‐empty base64 string was supplied.
        2. Attempt to decode the string into raw bytes, logging the length and
           a short preview of the payload for debugging.  If decoding fails,
           a helpful message is logged and ``None`` is returned.
        3. Deserialize the raw bytes into a :class:`solders.transaction.VersionedTransaction`.
           If this fails, the error is logged and ``None`` is returned.
        4. Sign the deserialized message using the provided keypair.  The
           signature replaces any existing placeholder signature for the wallet
           within the transaction's ``signatures`` list.  If the wallet's
           public key cannot be found in the list of ``account_keys``, the
           signature is appended.  Signature errors are logged but do not stop
           the function from returning the partially signed transaction.

        A fully signed ``VersionedTransaction`` (or partially signed if signing
        fails) is returned on success.  If any irrecoverable error occurs,
        ``None`` is returned.
        """
        try:
            # Validate the input early.  It's common for the aggregator to
            # sometimes return ``None`` or an empty string when there is no
            # transaction available.  In that case we log and bail out.
            if not swap_tx_base64 or not isinstance(swap_tx_base64, str):
                logging.error("[JUPITER] Empty or invalid swap transaction string.")
                return None

            # Step 1 – base64 decode the transaction.  This will raise a
            # ``binascii.Error`` if the input is not valid base64.  We catch
            # and log it explicitly so that the caller has context about what
            # went wrong.  We also log the length and a preview of the first
            # bytes to aid debugging without dumping the entire payload.
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
            # Log a short hex preview of the first 32 bytes for debugging.  Using
            # ``hex()`` avoids printing non‐printable bytes directly in logs.
            preview = tx_bytes[:32].hex()
            logging.debug(f"[JUPITER] First 32 decoded bytes (hex): {preview}")

            # Step 2 – deserialize the bytes into a VersionedTransaction.  If
            # this fails it's likely that the payload was corrupt or from an
            # unexpected transaction format.
            try:
                raw_tx = VersionedTransaction.from_bytes(tx_bytes)
            except Exception as deser_err:
                logging.exception(f"[JUPITER] Failed to deserialize VersionedTransaction: {deser_err}")
                self._send_telegram_debug(
                    f"❌ VersionedTransaction.from_bytes failed: {type(deser_err).__name__}: {deser_err}\n"
                    f"Decoded length: {len(tx_bytes)} bytes\n"
                    f"First 32 bytes (hex): {preview}"
                )
                return None

            logging.info("[JUPITER] VersionedTransaction deserialized successfully.")

            # Step 3 – sign the transaction with our keypair.  Jupiter returns
            # partially signed transactions containing a dummy signature for the
            # user.  To submit the transaction we need to replace the dummy
            # signature with a real signature.  We use ``to_bytes_versioned``
            # from ``solders.message`` to get the canonical message bytes for
            # signing.  If any step fails, the error is logged and the
            # partially signed transaction is returned so the caller can decide
            # how to handle it.
            try:
                # import lazily to avoid circular imports if solders.message is
                # unavailable.  If import fails we return the raw transaction
                # without modifying signatures.
                from solders.message import to_bytes_versioned

                message_bytes = to_bytes_versioned(raw_tx.message)
                signature = keypair.sign_message(message_bytes)

                # Copy the existing signatures so we don't mutate the original list
                sigs = list(raw_tx.signatures)

                # Try to find our public key in the account keys.  The index
                # corresponds to the signature slot.  If not found, append the
                # signature to the end of the list.
                try:
                    sig_index = next(
                        i for i, pk in enumerate(raw_tx.message.account_keys)
                        if pk == keypair.pubkey()
                    )
                except StopIteration:
                    sig_index = None

                if sig_index is not None:
                    sigs[sig_index] = signature
                    logging.debug(f"[JUPITER] Replaced signature at index {sig_index}.")
                else:
                    sigs.append(signature)
                    logging.debug("[JUPITER] Wallet pubkey not found among account keys; signature appended.")

                # Assign the updated signatures back to the transaction
                raw_tx.signatures = sigs
                logging.info("[JUPITER] Swap transaction signed successfully.")
            except ImportError:
                # The solders.message module may not be available in the
                # execution environment.  In that case we return the
                # deserialized transaction unmodified.
                logging.warning(
                    "[JUPITER] solders.message.to_bytes_versioned could not be imported; "
                    "returning unsigned VersionedTransaction."
                )
            except Exception as sign_err:
                logging.exception(f"[JUPITER] Error while signing swap transaction: {sign_err}")
                self._send_telegram_debug(
                    f"⚠️ Error signing transaction: {type(sign_err).__name__}: {sign_err}\n"
                    f"Proceeding with partially signed transaction."
                )

            # Return the (possibly signed) VersionedTransaction.  The caller is
            # responsible for further handling (e.g. simulation or sending).
            return raw_tx
        except Exception as outer_err:
            # Catch-all to ensure no exceptions propagate unexpectedly.  This
            # should be rare, as most errors should be caught above.  We log
            # the error and return None to signal failure.
            logging.exception(f"[JUPITER] Unexpected error in build_swap_transaction: {outer_err}")
            self._send_telegram_debug(
                f"❌ Unexpected error in build_swap_transaction: {type(outer_err).__name__}: {outer_err}"
            )
            return None

    def send_transaction(self, signed_tx: VersionedTransaction, keypair: Keypair = None):
        try:
            # Serialize the VersionedTransaction into raw bytes.  ``bytes()``
            # raises if the transaction is not fully populated.
            raw_tx_bytes = bytes(signed_tx)

            # It's possible for valid transactions to be relatively short,
            # especially when swapping small amounts.  Instead of refusing to
            # send based on size alone, log a warning but continue.  The RPC
            # will perform its own validation and return an error if the
            # transaction is malformed.
            if len(raw_tx_bytes) < 200:
                logging.warning(
                    f"[JUPITER] Serialized transaction length is only {len(raw_tx_bytes)} bytes. "
                    f"Proceeding to send anyway."
                )

            result = self.client.send_raw_transaction(
                raw_tx_bytes,
                opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed)
            )

            # The solana-py RPC client returns a dict with either a "result"
            # containing the transaction signature or an "error" field.  We
            # inspect both and surface helpful messages via Telegram debug.
            if isinstance(result, dict) and "error" in result:
                error_info = json.dumps(result["error"], indent=2)
                self._send_telegram_debug(
                    f"❌ Transaction send error:\n```{error_info}```"
                )
                return None

            if not result or (isinstance(result, dict) and not result.get("result")):
                self._send_telegram_debug(
                    f"❌ TX failed — No tx hash returned:\n```{result}```"
                )
                return None

            # The RPC library may return either a dict with a 'result' key or
            # a simple signature string.  Normalize to a string here.
            if isinstance(result, dict):
                return str(result["result"])
            return str(result)

        except Exception as e:
            # Catch any unexpected exception and surface it via Telegram.  The
            # type name helps differentiate between different error classes.
            self._send_telegram_debug(
                f"❌ Send error:\n{type(e).__name__}: {e}"
            )
            logging.exception(f"[JUPITER] Unexpected exception while sending transaction: {e}")
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
