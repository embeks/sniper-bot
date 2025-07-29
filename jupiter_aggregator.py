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
        """
        Ensure that an associated token account exists for the given owner and mint.

        This helper checks for the existence of the ATA via `get_account_info`. If none
        is found, it constructs and sends a transaction to create one using the
        Associated Token Account program.  Some versions of `solana-py` return a
        dictionary from `get_latest_blockhash()`, while newer versions return a
        `GetLatestBlockhashResp` object.  We handle both cases when extracting
        the blockhash.  We also convert the `solders.Keypair` into a
        `solana.keypair.Keypair` because `solana-py` transactions cannot be signed
        directly with a `solders.Keypair`.
        """
        ata = get_associated_token_address(owner, mint)
        res = self.client.get_account_info(ata)
        if res.value is not None:
            # ATA already exists, nothing to do.
            return
        logging.warning(f"[JUPITER] Creating missing ATA for {str(mint)}")

        # Build the create-ATA instruction
        ix = TransactionInstruction(
            keys=[
                AccountMeta(pubkey=owner, is_signer=True, is_writable=True),
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
            # Handle both dict and object return types for get_latest_blockhash
            blockhash_resp = self.client.get_latest_blockhash()
            if isinstance(blockhash_resp, dict):
                blockhash = blockhash_resp["result"]["value"]["blockhash"]
            else:
                # Newer solana-py returns an object with value.blockhash
                blockhash = blockhash_resp.value.blockhash
            tx.recent_blockhash = str(blockhash)
            tx.fee_payer = owner

            # Convert solders Keypair -> solana-py Keypair using the first 32 bytes
            try:
                from solana.keypair import Keypair as SolanaKeypair  # type: ignore
                sol_kp = SolanaKeypair.from_secret_key(bytes(keypair)[:32])
            except Exception as e:
                logging.error(f"[JUPITER] Failed to convert keypair for ATA creation: {e}")
                return

            # Sign and send transaction
            try:
                tx.sign(sol_kp)
                result = self.client.send_transaction(
                    tx,
                    sol_kp,
                    opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed)
                )
                logging.info(f"[JUPITER] ATA Creation TX: {result}")
            except Exception as e:
                logging.error(f"[JUPITER] Failed to create ATA: {e}")
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
        Decode the Jupiter swap transaction from base64 and optionally sign it.

        The Jupiter API returns a base64-encoded, partially-signed versioned
        transaction.  This method decodes the bytes and attempts to deserialize
        it into a `VersionedTransaction`.  If successful, it will look for the
        user's public key in the transaction's `account_keys` and replace the
        corresponding null signature with a real signature.  If the solders
        message helper `to_bytes_versioned` cannot be imported, the method
        falls back to signing using `bytes(message)`; if that also fails, it
        returns the raw bytes.  Any exceptions are logged and result in
        `None` being returned.
        """
        try:
            if not swap_tx_base64:
                raise ValueError("swap_tx_base64 is empty or None")
            # Decode the base64 string
            tx_bytes = base64.b64decode(swap_tx_base64)
            logging.warning(f"[JUPITER] Decoded tx_bytes length: {len(tx_bytes)}")
            logging.warning(f"[JUPITER] First 20 decoded bytes:\n{repr(tx_bytes[:20])}")

            # Attempt to deserialize to inspect or sign
            tx = None
            try:
                tx = VersionedTransaction.from_bytes(tx_bytes)
            except Exception as e:
                logging.warning(f"[JUPITER] Could not deserialize VersionedTransaction: {e}")

            if tx is not None:
                # Attempt to sign the transaction by replacing the null signature
                try:
                    from solders.message import to_bytes_versioned  # type: ignore
                    # Extract the message bytes for signing
                    message_bytes = to_bytes_versioned(tx.message)
                    # Compute signature
                    user_sig = keypair.sign_message(message_bytes)
                    # Find index of our pubkey in account_keys
                    sig_index = next(
                        i for i, k in enumerate(tx.message.account_keys)
                        if k == keypair.pubkey()
                    )
                    sigs = tx.signatures
                    sigs[sig_index] = user_sig
                    tx.signatures = sigs
                    return tx  # return signed VersionedTransaction
                except Exception:
                    # If to_bytes_versioned is unavailable, attempt to sign using bytes(message)
                    try:
                        message_bytes = bytes(tx.message)
                        user_sig = keypair.sign_message(message_bytes)
                        sig_index = next(
                            i for i, k in enumerate(tx.message.account_keys)
                            if k == keypair.pubkey()
                        )
                        sigs = tx.signatures
                        sigs[sig_index] = user_sig
                        tx.signatures = sigs
                        logging.info("[JUPITER] Signed tx using fallback message bytes")
                        return tx
                    except Exception as inner:
                        logging.warning(
                            f"[JUPITER] solders.message.to_bytes_versioned could not be imported or fallback signing failed; returning unsigned serialized bytes. Error: {inner}"
                        )
                        # Fall back to raw bytes
                        return tx_bytes
            # If we couldn't deserialize, return raw bytes
            return tx_bytes
        except Exception as e:
            logging.exception("[JUPITER] Unexpected error in build_swap_transaction")
            self._send_telegram_debug(f"❌ Unexpected swapTransaction error: {e}")
            return None

    def send_transaction(self, signed_tx, keypair: Keypair = None):
        """
        Submit a serialized transaction to the RPC endpoint.

        Accepts either a `VersionedTransaction` or raw bytes.  This method
        normalises the RPC response across solana-py versions, logs any RPC
        errors via Telegram, and returns the transaction signature on success.
        """
        try:
            # Determine if we were given a VersionedTransaction or raw bytes
            if isinstance(signed_tx, VersionedTransaction):
                raw_tx_bytes = bytes(signed_tx)
            elif isinstance(signed_tx, (bytes, bytearray)):
                raw_tx_bytes = bytes(signed_tx)
            else:
                raise TypeError(f"Unsupported transaction type: {type(signed_tx)}")

            # Send the raw transaction
            result = self.client.send_raw_transaction(
                raw_tx_bytes,
                opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed)
            )
            # Normalise the response shape
            if isinstance(result, dict):
                # solana-py returns a dict with 'result' on success or 'error'
                if "error" in result and result["error"]:
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



