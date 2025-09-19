"""
Wallet Management - Deterministic verification and balance tracking
"""

import base58
import logging
from typing import Optional, Dict, List
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.rpc.responses import GetTokenAccountsByOwnerResp
from solana.rpc.api import Client
from spl.token.instructions import get_associated_token_address

from config import (
    PRIVATE_KEY, RPC_ENDPOINT, TOKEN_PROGRAM_ID,
    MIN_SOL_BALANCE, BUY_AMOUNT_SOL, MAX_POSITIONS
)

logger = logging.getLogger(__name__)

class WalletManager:
    """Manages wallet operations with deterministic verification"""
    
    def __init__(self):
        """Initialize wallet from private key"""
        try:
            # Decode private key
            if PRIVATE_KEY.startswith('[') and PRIVATE_KEY.endswith(']'):
                # Array format
                key_array = eval(PRIVATE_KEY)
                self.keypair = Keypair.from_bytes(bytes(key_array))
            else:
                # Base58 format
                decoded = base58.b58decode(PRIVATE_KEY)
                self.keypair = Keypair.from_bytes(decoded)
            
            self.pubkey = self.keypair.pubkey()
            self.client = Client(RPC_ENDPOINT)
            
            # Verify wallet
            self._verify_wallet()
            
            logger.info(f"✅ Wallet initialized: {self.pubkey}")
            
        except Exception as e:
            logger.error(f"❌ Failed to initialize wallet: {e}")
            raise
    
    def _verify_wallet(self):
        """Verify wallet matches expected address"""
        try:
            # Get SOL balance to verify wallet is accessible
            response = self.client.get_balance(self.pubkey)
            balance_sol = response.value / 1e9
            
            logger.info(f"📊 Wallet balance: {balance_sol:.4f} SOL")
            
            if balance_sol < MIN_SOL_BALANCE:
                logger.warning(f"⚠️ Low balance: {balance_sol:.4f} SOL (minimum: {MIN_SOL_BALANCE} SOL)")
            
            # Check if we have enough for trading
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
        """Get current SOL balance"""
        try:
            response = self.client.get_balance(self.pubkey)
            return response.value / 1e9
        except Exception as e:
            logger.error(f"Failed to get SOL balance: {e}")
            return 0.0
    
    def get_token_balance(self, mint: str) -> float:
        """Get balance for a specific token"""
        try:
            mint_pubkey = Pubkey.from_string(mint)
            token_account = get_associated_token_address(self.pubkey, mint_pubkey)
            
            response = self.client.get_token_account_balance(token_account)
            if response.value:
                return float(response.value.ui_amount or 0)
            return 0.0
            
        except Exception as e:
            logger.debug(f"No balance for token {mint[:8]}...")
            return 0.0
    
    def get_all_token_accounts(self) -> Dict[str, Dict]:
        """Get all token accounts owned by this wallet"""
        try:
            response = self.client.get_token_accounts_by_owner(
                self.pubkey,
                {"programId": TOKEN_PROGRAM_ID}
            )
            
            token_accounts = {}
            
            if response.value:
                for account in response.value:
                    try:
                        account_data = account.account.data
                        if isinstance(account_data, dict):
                            parsed = account_data.get('parsed', {})
                            info = parsed.get('info', {})
                            
                            mint = info.get('mint')
                            if mint:
                                token_accounts[mint] = {
                                    'pubkey': str(account.pubkey),
                                    'balance': float(info.get('tokenAmount', {}).get('uiAmount', 0)),
                                    'decimals': info.get('tokenAmount', {}).get('decimals', 0)
                                }
                    except:
                        continue
            
            return token_accounts
            
        except Exception as e:
            logger.error(f"Failed to get token accounts: {e}")
            return {}
    
    def can_trade(self) -> bool:
        """Check if wallet can execute a trade"""
        try:
            balance = self.get_sol_balance()
            required = MIN_SOL_BALANCE + BUY_AMOUNT_SOL
            
            if balance < required:
                logger.warning(f"Insufficient balance: {balance:.4f} SOL (need {required:.4f} SOL)")
                return False
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to check trade capability: {e}")
            return False
    
    def verify_transaction_destination(self, tx_accounts: List[str]) -> bool:
        """Verify transaction involves our wallet"""
        our_wallet = str(self.pubkey)
        
        if our_wallet not in tx_accounts:
            logger.warning(f"⚠️ Transaction doesn't involve our wallet!")
            logger.debug(f"Our wallet: {our_wallet}")
            logger.debug(f"TX accounts: {tx_accounts[:5]}...")  # Show first 5
            return False
        
        return True
    
    def get_token_account_or_create_ix(self, mint: str):
        """Get token account address (for use in transactions)"""
        try:
            mint_pubkey = Pubkey.from_string(mint)
            return get_associated_token_address(self.pubkey, mint_pubkey)
        except Exception as e:
            logger.error(f"Failed to derive token account: {e}")
            raise
    
    def estimate_profit_loss(self, mint: str, entry_price: float, current_price: float, amount: float) -> Dict:
        """Calculate P&L for a position"""
        try:
            # Calculate values
            entry_value = entry_price * amount
            current_value = current_price * amount
            pnl_usd = current_value - entry_value
            pnl_percentage = ((current_value / entry_value) - 1) * 100 if entry_value > 0 else 0
            
            return {
                'entry_value': entry_value,
                'current_value': current_value,
                'pnl_usd': pnl_usd,
                'pnl_percentage': pnl_percentage,
                'is_profit': pnl_usd > 0
            }
            
        except Exception as e:
            logger.error(f"Failed to calculate P&L: {e}")
            return {
                'entry_value': 0,
                'current_value': 0,
                'pnl_usd': 0,
                'pnl_percentage': 0,
                'is_profit': False
            }
    
    def log_wallet_status(self):
        """Log current wallet status"""
        try:
            sol_balance = self.get_sol_balance()
            token_accounts = self.get_all_token_accounts()
            
            logger.info("=" * 50)
            logger.info(f"📊 WALLET STATUS")
            logger.info(f"Address: {self.pubkey}")
            logger.info(f"SOL Balance: {sol_balance:.4f}")
            logger.info(f"Token Positions: {len(token_accounts)}")
            
            if token_accounts:
                logger.info("Active Positions:")
                for mint, data in list(token_accounts.items())[:5]:  # Show first 5
                    if data['balance'] > 0:
                        logger.info(f"  • {mint[:8]}... Balance: {data['balance']:,.2f}")
            
            logger.info("=" * 50)
            
        except Exception as e:
            logger.error(f"Failed to log wallet status: {e}")
