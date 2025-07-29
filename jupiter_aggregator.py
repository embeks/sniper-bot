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
        Decode and sign a Jupiter `swapTransaction`.

        Jupiter's `/swap` API returns a base64-encoded versioned transaction
        that is unsigned for the user.  The aggregator or program may have
        already added its own signature at a later index, but the user must
        sign the message as the fee payer.  This method decodes the base64
        string into a `VersionedTransaction`, signs its message with the
        provided `solders.Keypair`, then re-populates a new transaction with
        the signature list.

        Steps:
          1. Decode the base64 string into a `VersionedTransaction`.
          2. Extract the message bytes.  Prefer `solders.message.to_bytes_versioned`
             if available; fall back to `bytes(raw_tx.message)` otherwise.
          3. Sign the message bytes with the provided keypair.
          4. Construct a new `VersionedTransaction` using
             `VersionedTransaction.populate(raw_tx.message, sigs)` where
             `sigs` contains the user's signature in the first slot and
             preserves any existing signatures from Jupiter.
          5. Log the decoded length and a short prefix of the bytes for
             debugging.

        If any step fails, a debug message is sent to Telegram and `None`
        is returned.

        :param swap_tx_base64: The base64-encoded swap transaction returned
            by Jupiter's API.
        :param keypair: The user's `solders.Keypair` for signing.
        :return: A fully signed `VersionedTransaction` on success, otherwise
            `None`.
        """
        try:
            if not swap_tx_base64:
                raise ValueError("swap_tx_base64 is empty or None")
            # Sanitize and decode base64
            sanitized = swap_tx_base64.replace("\n", "").replace(" ", "").strip()
            try:
                raw_bytes = base64.b64decode(sanitized)
            except Exception as e:
                logging.error(f"[JUPITER] Failed to decode base64 swap transaction: {e}")
                self._send_telegram_debug(f"❌ Failed to decode swap transaction: {e}")
                return None
            # Log length and prefix
            logging.warning(f"[JUPITER] Decoded tx_bytes length: {len(raw_bytes)}")
            logging.warning(f"[JUPITER] First 20 decoded bytes:\n{repr(raw_bytes[:20])}")
            # Deserialize to VersionedTransaction
            try:
                raw_tx = VersionedTransaction.from_bytes(raw_bytes)
            except Exception as e:
                logging.error(f"[JUPITER] Failed to deserialize transaction: {e}")
                self._send_telegram_debug(f"❌ Failed to deserialize swap transaction: {e}")
                return None
            # Obtain message bytes for signing
            try:
                from solders.message import to_bytes_versioned  # type: ignore
                msg_bytes = to_bytes_versioned(raw_tx.message)
            except Exception:
                msg_bytes = bytes(raw_tx.message)
            # Sign the message bytes with the user's keypair
            try:
                user_sig = keypair.sign_message(msg_bytes)
            except Exception as e:
                logging.error(f"[JUPITER] Failed to sign message: {e}")
                self._send_telegram_debug(f"❌ Failed to sign swap transaction: {e}")
                return None
            # Combine the user's signature with any existing signatures
            existing_sigs = list(raw_tx.signatures)
            if existing_sigs:
                sigs = [user_sig] + existing_sigs[1:]
            else:
                sigs = [user_sig]
            # Populate a new transaction with the message and signatures
            try:
                signed_tx = VersionedTransaction.populate(raw_tx.message, sigs)
            except Exception as e:
                logging.error(f"[JUPITER] Failed to populate signed transaction: {e}")
                self._send_telegram_debug(f"❌ Failed to build signed transaction: {e}")
                return None
            return signed_tx
        except Exception as e:
            logging.exception("[JUPITER] Unexpected error in build_swap_transaction")
            self._send_telegram_debug(f"❌ Unexpected swapTransaction error: {e}")
            return None

    def send_transaction(self, signed_tx, keypair: Keypair = None):
        """
        Submit a serialized transaction to the RPC endpoint.

        Accepts a `VersionedTransaction`, raw bytes, or a base64 string.
        The transaction bytes are sent via `send_raw_transaction` with
        `skip_preflight=True` and a `Confirmed` commitment.  Any RPC
        error information is forwarded to Telegram.  On success, the
        method returns the transaction signature as a string.
        """
        try:
            # Determine how to obtain raw bytes
            if isinstance(signed_tx, VersionedTransaction):
                raw_tx_bytes = bytes(signed_tx)
            elif isinstance(signed_tx, (bytes, bytearray)):
                raw_tx_bytes = bytes(signed_tx)
            elif isinstance(signed_tx, str):
                try:
                    raw_tx_bytes = base64.b64decode(signed_tx.replace("\n", "").replace(" ", "").strip())
                except Exception as e:
                    self._send_telegram_debug(f"❌ Base64 decode error: {e}")
                    return None
            else:
                raise TypeError(f"Unsupported transaction type: {type(signed_tx)}")
            # Send the raw bytes
            result = self.client.send_raw_transaction(
                raw_tx_bytes,
                opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed)
            )
            # Normalise the response shape
            if isinstance(result, dict):
                if result.get("error"):
                    error_info = json.dumps(result["error"], indent=2)
                    self._send_telegram_debug(f"❌ TX Error:\n```{error_info}```")
                    return None
                sig = result.get("result")
                if not sig:
                    self._send_telegram_debug(f"❌ TX failed — No tx hash returned:\n```{result}```")
                    return None
                return str(sig)
            # Some versions may return the signature directly
            return str(result)
        except Exception as e:
            self._send_telegram_debug(f"❌ Send error:\n{type(e).__name__}: {e}")
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
