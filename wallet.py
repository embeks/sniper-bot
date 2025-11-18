"""
Wallet Management - LATENCY OPTIMIZED: Cached decimals + async wrapper
"""

import base58
import base64
import logging
import json
import struct
import time
import asyncio
from functools import lru_cache
from typing import Optional, Dict, List, Tuple
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed
from spl.token.instructions import get_associated_token_address

from config import (
    PRIVATE_KEY, RPC_ENDPOINT,
    TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID,
    MIN_SOL_BALANCE, BUY_AMOUNT_SOL, MAX_POSITIONS
)

logger = logging.getLogger(__name__)


class WalletManager:
    """Manages wallet operations with deterministic verification"""
    
    def __init__(self):
        try:
            if PRIVATE_KEY.startswith('[') and PRIVATE_KEY.endswith(']'):
                key_array = json.loads(PRIVATE_KEY)
                self.keypair = Keypair.from_bytes(bytes(key_array))
            else:
                decoded =
 base58.b58decode(PRIVATE_KEY)
                self.keypair = Keypair.from_bytes(decoded)

            self.pubkey = self.keypair.pubkey()
            self.client = Client(RPC_ENDPOINT)

            self._verify_wallet()

            logger.info(f"✅ Wallet initialized: {self.pubkey}")

        except Exception as e:
            logger.error(f"❌ Failed to initialize wallet: {e}")
            raise

    def _verify_wallet(self):
        try:
            response = self.client.get_balance(self.pubkey)
            balance_sol = response.value / 1e9

            logger.info(f"📊 Wallet balance: {balance_sol:.4f} SOL")

            if balance_sol < MIN_SOL_BALANCE:
                logger.warning(f"⚠️ Low balance: {balance_sol:.4f} SOL (minimum: {MIN_SOL_BALANCE} SOL)")

            tradeable_balance = balance_sol - MIN_SOL_BALANCE
            max_trades = int(tradeable_balance / BUY_AMOUNT_SOL)

            if max_trades < 1:
                logger.warning(f"⚠️ Limited trading capability. Balance: {balance_sol:.4f} SOL, Reserved: {MIN_SOL_BALANCE} SOL")
            else:
                logger.info(f"💰 Can execute up to {min(max_trades, MAX_POSITIONS)} trades")

        except Exception as e:
            logger.error(f"❌ Wallet verification failed: {e}")
            raise

    def get_sol_balance(self) -> float:
        try:
            response = self.client.get_balance(self.pubkey)
            return response.value / 1e9
        except Exception as e:
            logger.error(f"Failed to get SOL balance: {e}")
            return 0.0

    def get_token_balance(self, mint: str, max_retries: int = 3, retry_delay: float = 1.0) -> float:
        """
        Now Token-2022 compatible:
        - Classic ATA via Tokenkeg
        - Token-2022 ATA via TokenzQdB
        - Fallback scans both program IDs
        """
        for attempt in range(max_retries):
            try:
                mint_pubkey = Pubkey.from_string(mint)

                # Try classic SPL ATA first
                token_account = get_associated_token_address(self.pubkey, mint_pubkey)

                response = self.client.get_token_account_balance(token_account, commitment=Confirmed)

                if response.value:
                    ui_amount = response.value.ui_amount
                    if ui_amount:
                        return float(ui_amount)

                    raw_amount = response.value.amount
                    decimals = response.value.decimals
                    if raw_amount and decimals:
                        return float(int(raw_amount) / (10 ** int(decimals)))

                time.sleep(retry_delay)

            except Exception:
                time.sleep(retry_delay)

        logger.warning(f"⚠️ Direct ATA balance failed for {mint[:8]}..., trying fallback (Tokenkeg + Token-2022)")

        try:
            all_accounts = self.get_all_token_accounts()
            if mint in all_accounts:
                bal = all_accounts[mint]["balance"]
                logger.info(f"✅ Found via fallback: {bal:,.2f} tokens for {mint[:8]}...")
                return bal
            else:
                logger.warning(f"❌ Token {mint[:8]}... not found in any token accounts")
                return 0.0
        except Exception as e:
            logger.error(f"❌ Fallback method also failed: {e}")
            return 0.0

    def get_token_balance_raw(self, mint: str) -> int:
        try:
            mint_pubkey = Pubkey.from_string(mint)
            token_account = get_associated_token_address(self.pubkey, mint_pubkey)

            response = self.client.get_token_account_balance(token_account)
            if response.value:
                raw_amount = response.value.amount
                return int(raw_amount) if raw_amount else 0
            return 0

        except Exception:
            return 0

    def get_all_token_accounts(self) -> Dict[str, Dict]:
        """
        UPDATED: scan BOTH SPL programs:
        - TOKEN_PROGRAM_ID (classic SPL)
        - TOKEN_2022_PROGRAM_ID (Token-2022)
        """
        program_ids = [TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID]

        token_accounts = {}

        try:
            from solana.rpc.types import TokenAccountOpts

            # jsonParsed pass for both programs
            for pid in program_ids:
                response = self.client.get_token_accounts_by_owner_json_parsed(
                    self.pubkey,
                    TokenAccountOpts(program_id=pid)
                )

                if response.value:
                    for account in response.value:
                        try:
                            acc_data = account.account.data

                            if isinstance(acc_data, dict) and "parsed" in acc_data:
                                parsed = acc_data["parsed"]
                                info = parsed.get("info", {})
                                mint = info.get("mint")

                                if mint:
                                    token_amount = info.get("tokenAmount", {})
                                    token_accounts[mint] = {
                                        "pubkey": str(account.pubkey),
                                        "balance": float(token_amount.get("uiAmount", 0)),
                                        "decimals": token_amount.get("decimals", 0),
                                        "raw_amount": token_amount.get("amount", "0")
                                    }
                        except Exception:
                            continue

            if token_accounts:
                logger.debug(f"✅ Found {len(token_accounts)} token accounts via jsonParsed (dual programs)")
                return token_accounts

        except Exception:
            pass

        # Base64 RAW method fallback for both programs
        try:
            for pid in program_ids:
                response = self.client.get_token_accounts_by_owner(
                    self.pubkey,
                    {"programId": str(pid)}
                )

                if response.value:
                    for account in response.value:
                        try:
                            acc_data = account.account.data

                            data_bytes = None

                            if isinstance(acc_data, bytes):
                                data_bytes = acc_data
                            elif isinstance(acc_data, str):
                                data_bytes = base64.b64decode(acc_data)
                            elif isinstance(acc_data, list) and acc_data:
                                data_bytes = base64.b64decode(acc_data[0])
                            elif isinstance(acc_data, dict) and "parsed" in acc_data:
                                parsed = acc_data["parsed"]
                                info = parsed.get("info", {})
                                mint = info.get("mint")
                                if mint:
                                    token_amount = info.get("tokenAmount", {})
                                    token_accounts[mint] = {
                                        "pubkey": str(account.pubkey),
                                        "balance": float(token_amount.get("uiAmount", 0)),
                                        "decimals": token_amount.get("decimals", 0),
                                        "raw_amount": token_amount.get("amount", "0")
                                    }
                                continue

                            if not data_bytes or len(data_bytes) < 165:
                                continue

                            mint_bytes = data_bytes[0:32]
                            amount_bytes = data_bytes[64:72]

                            mint = str(Pubkey(mint_bytes))
                            raw_amount = struct.unpack("<Q", amount_bytes)[0]

                            decimals = self.get_token_decimals(mint)
                            ui_amount = raw_amount / (10 ** decimals)

                            token_accounts[mint] = {
                                "pubkey": str(account.pubkey),
                                "balance": float(ui_amount),
                                "decimals": decimals,
                                "raw_amount": str(raw_amount)
                            }

                        except Exception:
                            continue

            logger.debug(f"✅ Found {len(token_accounts)} token accounts via base64 decode (dual programs)")
            return token_accounts

        except Exception as e:
            logger.error(f"❌ CRITICAL: All methods failed to get token accounts: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return {}

    def can_trade(self) -> bool:
        try:
            balance = self.get_sol_balance()
            required = MIN_SOL_BALANCE + BUY_AMOUNT_SOL + 0.000005 + 0.0001 + (BUY_AMOUNT_SOL * 0.01)

            if balance < required:
                logger.warning(f"Insufficient balance: {balance:.4f} SOL (need {required:.4f} SOL)")
                return False

            return True

        except Exception as e:
            logger.error(f"Failed to check trade capability: {e}")
            return False

    def verify_transaction_destination(self, tx_accounts: List[str]) -> bool:
        our_wallet = str(self.pubkey)
        if our_wallet not in tx_accounts:
            logger.warning("⚠️ Transaction doesn't involve our wallet!")
            return False
        return True

    def get_token_account_or_create_ix(self, mint: str):
        try:
            mint_pubkey = Pubkey.from_string(mint)
            return get_associated_token_address(self.pubkey, mint_pubkey)
        except Exception as e:
            logger.error(f"Failed to derive token account: {e}")
            raise

    def estimate_profit_loss(self, mint: str, entry_price: float, current_price: float, amount: float) -> Dict:
        try:
            entry_value = entry_price * amount
            current_value = current_price * amount
            pnl_usd = current_value - entry_value
            pnl_percentage = ((current_value / entry_value) - 1) * 100 if entry_value > 0 else 0

            return {
                "entry_value": entry_value,
                "current_value": current_value,
                "pnl_usd": pnl_usd,
                "pnl_percentage": pnl_percentage,
                "is_profit": pnl_usd > 0
            }

        except Exception:
            return {
                "entry_value": 0,
                "current_value": 0,
                "pnl_usd": 0,
                "pnl_percentage": 0,
                "is_profit": False
            }

    @lru_cache(maxsize=4096)
    def get_token_decimals(self, mint: str) -> int:
        try:
            mint_pubkey = Pubkey.from_string(mint)

            response = self.client.get_account_info(mint_pubkey)
            if response.value and response.value.data:
                mint_data = response.value.data

                if isinstance(mint_data, str):
                    mint_bytes = base64.b64decode(mint_data)
                elif isinstance(mint_data, bytes):
                    mint_bytes = mint_data
                elif isinstance(mint_data, list) and len(mint_data) == 2 and mint_data[1] == "base64":
                    mint_bytes = base64.b64decode(mint_data[0])
                elif isinstance(mint_data, dict) and "parsed" in mint_data:
                    parsed_info = mint_data.get("parsed", {}).get("info", {})
                    return parsed_info.get("decimals", 6)
                else:
                    return 6

                if len(mint_bytes) > 44:
                    decimals = mint_bytes[44]
                    if decimals > 12:
                        return 6
                    return decimals
                return 6

            return 6

        except Exception:
            return 6

    async def get_token_decimals_async(self, mint: str) -> int:
        return await asyncio.to_thread(self.get_token_decimals, mint)

    def log_wallet_status(self):
        try:
            sol_balance = self.get_sol_balance()
            token_accounts = self.get_all_token_accounts()

            logger.info("=" * 50)
            logger.info("📊 WALLET STATUS")
            logger.info(f"Address: {self.pubkey}")
            logger.info(f"SOL Balance: {sol_balance:.4f}")
            logger.info(f"Token Positions: {len(token_accounts)}")

            if token_accounts:
                logger.info("Active Positions:")
                for mint, data in list(token_accounts.items())[:5]:
                    if data["balance"] > 0:
                        logger.info(f"  • {mint[:8]}... Balance: {data['balance']:,.2f}")

            logger.info("=" * 50)

        except Exception as e:
            logger.error(f"Failed to log wallet status: {e}")
