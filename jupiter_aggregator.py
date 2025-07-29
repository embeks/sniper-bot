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
                # In solana-py >=0.28 the get_latest_blockhash returns a
                # GetLatestBlockhashResp object.  Extract the blockhash via
                # its ``value`` attribute rather than treating it as a dict.
                latest = self.client.get_latest_blockhash()
                # fall back to old behaviour if it's a mapping
                if isinstance(latest, dict):
                    blockhash = latest["result"]["value"]["blockhash"]
                else:
                    blockhash = latest.value.blockhash  # type: ignore[attr-defined]

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
           public key is not among the ``account_keys``, the signature is not
           appended to avoid mismatching the expected signature count.  Errors
           encountered during signing are logged; in these cases the
           partially signed transaction is still returned for the caller to
           handle.

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

            # Step 1 – base64 decode the transaction.  Catch and log any decoding
            # errors explicitly so that the caller has context about what went
            # wrong.
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

            # Step 2 – deserialize the bytes into a VersionedTransaction.
            try:
                raw_tx = VersionedTransaction.from_bytes(tx_bytes)
            except Exception as deser_err:
                logging.exception(f"[JUPITER] Failed to deserialize VersionedTransaction: {deser_err}")
                self._send_telegram_debug(
                    f"❌ VersionedTransaction.from_bytes failed: {type(deser_err).__name__}: {deser_err}\n"
                    f"Decoded length: {len(tx_bytes)} bytes\nFirst 32 bytes (hex): {preview_hex}"
                )
                return None

            logging.info("[JUPITER] VersionedTransaction deserialized successfully.")

            # Step 3 – attempt to sign the transaction with our keypair.  We use
            # solders.message.to_bytes_versioned to get the message bytes.  If
            # this module isn't available (e.g. solders isn't installed) we
            # return the deserialized transaction without modification.
            try:
                from solders.message import to_bytes_versioned  # type: ignore

                message_bytes = to_bytes_versioned(raw_tx.message)
                signature = keypair.sign_message(message_bytes)

                # Copy the existing signatures so we don't mutate the original list.
                sigs = list(raw_tx.signatures)

                # Determine where our signature should go.  The index is the
                # position of our pubkey in the message's account keys.  If the
                # key is not present, we cannot sign because adding an extra
                # signature would break the required signature count set in the
                # message header.  In that case we log and leave the
                # transaction unsigned.
                try:
                    sig_index = next(
                        i for i, pk in enumerate(raw_tx.message.account_keys)
                        if pk == keypair.pubkey()
                    )
                except StopIteration:
                    sig_index = None

                if sig_index is None:
                    logging.warning(
                        "[JUPITER] Wallet pubkey not found among account keys; cannot attach signature. "
                        "Proceeding with original signatures."
                    )
                else:
                    sigs[sig_index] = signature
                    raw_tx.signatures = sigs
                    logging.info(
                        f"[JUPITER] Swap transaction signed. Updated signature at index {sig_index}."
                    )
            except ImportError:
                logging.warning(
                    "[JUPITER] solders.message.to_bytes_versioned could not be imported; "
                    "returning unsigned VersionedTransaction."
                )
            except Exception as sign_err:
                logging.exception(f"[JUPITER] Error while signing swap transaction: {sign_err}")
                self._send_telegram_debug(
                    f"⚠️ Error signing transaction: {type(sign_err).__name__}: {sign_err}\n"
                    "Proceeding with partially signed transaction."
                )

            return raw_tx

        except Exception as outer_err:
            logging.exception(f"[JUPITER] Unexpected error in build_swap_transaction: {outer_err}")
            self._send_telegram_debug(
                f"❌ Unexpected error in build_swap_transaction: {type(outer_err).__name__}: {outer_err}"
            )
            return None

    def send_transaction(self, signed_tx: VersionedTransaction, keypair: Keypair = None):
        """
        Submit a signed :class:`VersionedTransaction` to the Solana RPC.  The
        provided ``signed_tx`` should already contain all necessary signatures.

        The optional ``keypair`` argument is accepted for backwards
        compatibility but is unused.  A warning is emitted if the serialized
        transaction is very small, but the function will still attempt to send
        it.  Any RPC errors are surfaced via the Telegram debug channel and
        logged.
        """
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

            # Normalize the RPC response.  The solana-py client may return
            # either a dict containing 'result' or an error.  We handle both
            # cases and surface errors via Telegram.
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
                tx_sig = result["result"]
            else:
                tx_sig = result

            logging.info(f"[JUPITER] Transaction submitted. Signature: {tx_sig}")
            return str(tx_sig)

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

