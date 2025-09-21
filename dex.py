"""
DEX Integration - PumpFun Bonding Curves and Raydium
FIXED: Proper bonding curve detection using WebSocket data
"""

import time
import base64
import struct
import logging
from typing import Optional, Dict, Tuple
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.instruction import Instruction, AccountMeta
from solders.compute_budget import set_compute_unit_price, set_compute_unit_limit
from solana.rpc.api import Client
from solana.transaction import Transaction
from spl.token.instructions import get_associated_token_address

from config import (
    PUMPFUN_PROGRAM_ID, PUMPFUN_FEE_RECIPIENT,
    SYSTEM_PROGRAM_ID, TOKEN_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID,
    RENT_PROGRAM_ID, RPC_ENDPOINT, BUY_AMOUNT_SOL,
    MIN_BONDING_CURVE_SOL, MAX_BONDING_CURVE_SOL, MIGRATION_THRESHOLD_SOL
)

logger = logging.getLogger(__name__)

class PumpFunDEX:
    """PumpFun bonding curve integration"""
    
    # Feature flag for Phase 1 - DISABLED since we use PumpPortal
    USE_MANUAL_PUMPFUN_BUY = False
    
    def __init__(self, wallet_manager):
        """Initialize with wallet manager"""
        self.wallet = wallet_manager
        self.client = Client(RPC_ENDPOINT)
        
        # Track bonding curve states from WebSocket
        self.bonding_curves_cache = {}
        self.token_websocket_data = {}
        
    def update_token_data(self, mint: str, websocket_data: Dict):
        """Update token data from WebSocket - called by monitor"""
        self.token_websocket_data[mint] = {
            'data': websocket_data,
            'timestamp': time.time()
        }
        logger.debug(f"Updated WebSocket data for {mint[:8]}...")
    
    def derive_bonding_curve_pda(self, mint: Pubkey) -> Tuple[Pubkey, int]:
        """Derive the bonding curve PDA for a token"""
        seeds = [b"bonding-curve", bytes(mint)]
        return Pubkey.find_program_address(seeds, PUMPFUN_PROGRAM_ID)
    
    def get_bonding_curve_data(self, mint: str) -> Optional[Dict]:
        """Get bonding curve data - prioritize WebSocket data over chain queries"""
        try:
            # CRITICAL: First check if we have recent WebSocket data
            if mint in self.token_websocket_data:
                ws_data = self.token_websocket_data[mint]
                data_age = time.time() - ws_data['timestamp']
                
                # Use WebSocket data if it's less than 60 seconds old
                if data_age < 60:
                    token_data = ws_data['data']
                    
                    # Extract the nested data if it exists
                    if 'data' in token_data:
                        token_data = token_data['data']
                    
                    # Get virtual reserves from WebSocket data
                    v_sol = token_data.get('vSolInBondingCurve', 0)
                    v_tokens = token_data.get('vTokensInBondingCurve', 0)
                    
                    # Convert SOL to lamports for consistency
                    if v_sol > 0:
                        v_sol_lamports = int(v_sol * 1e9)
                    else:
                        v_sol_lamports = 0
                    
                    if v_tokens > 0:
                        v_tokens_raw = int(v_tokens * 1e6)  # Assuming 6 decimals
                    else:
                        v_tokens_raw = 0
                    
                    # Check if token has migrated (virtual reserves at or above threshold)
                    is_migrated = v_sol >= MIGRATION_THRESHOLD_SOL
                    
                    if is_migrated:
                        logger.info(f"Token {mint[:8]}... has migrated (SOL in curve: {v_sol:.2f})")
                        return None  # Return None for migrated tokens
                    
                    curve_data = {
                        'bonding_curve': token_data.get('bondingCurveKey', ''),
                        'virtual_token_reserves': v_tokens_raw,
                        'virtual_sol_reserves': v_sol_lamports,
                        'real_token_reserves': v_tokens_raw,  # Use virtual as estimate
                        'real_sol_reserves': v_sol_lamports,
                        'price_per_token': v_sol_lamports / v_tokens_raw if v_tokens_raw > 0 else 0,
                        'sol_in_curve': v_sol,
                        'is_migrating': False,  # Already checked above
                        'can_buy': True,  # If not migrated, can buy
                        'from_websocket': True
                    }
                    
                    logger.debug(f"Using WebSocket data for {mint[:8]}... (age: {data_age:.1f}s)")
                    return curve_data
            
            # Check cache for recent chain data
            if mint in self.bonding_curves_cache:
                cached = self.bonding_curves_cache[mint]
                cache_age = time.time() - cached['timestamp']
                if cache_age < 10:  # Use cache for 10 seconds
                    logger.debug(f"Using cached bonding curve for {mint[:8]}...")
                    return cached['data']
            
            # If no WebSocket data, try to fetch from chain (fallback)
            mint_pubkey = Pubkey.from_string(mint)
            bonding_curve, _ = self.derive_bonding_curve_pda(mint_pubkey)
            
            # Get account info
            response = self.client.get_account_info(bonding_curve)
            if not response.value:
                logger.debug(f"No bonding curve account found for {mint[:8]}...")
                return None
            
            # Parse account data - this is the problematic part
            # Since we're using PumpPortal anyway, just return a basic structure
            # indicating the token exists but we can't parse the exact data
            logger.warning(f"Chain query for {mint[:8]}... - parsing may be unreliable")
            
            # Return a basic structure indicating token exists on bonding curve
            # but we don't have exact reserve data
            curve_data = {
                'bonding_curve': str(bonding_curve),
                'virtual_token_reserves': 1_000_000_000_000,  # Placeholder
                'virtual_sol_reserves': 30_000_000_000,  # 30 SOL placeholder
                'real_token_reserves': 1_000_000_000_000,
                'real_sol_reserves': 30_000_000_000,
                'price_per_token': 0.00003,  # Approximate
                'sol_in_curve': 30,
                'is_migrating': False,
                'can_buy': True,
                'from_websocket': False,
                'is_estimate': True  # Flag that this is estimated data
            }
            
            # Cache it briefly
            self.bonding_curves_cache[mint] = {
                'data': curve_data,
                'timestamp': time.time()
            }
            
            return curve_data
            
        except Exception as e:
            logger.error(f"Failed to get bonding curve data for {mint[:8]}...: {e}")
            # Return None on error - main.py will handle appropriately
            return None
    
    def calculate_buy_amount(self, mint: str, sol_amount: float) -> Tuple[int, float]:
        """Calculate expected tokens from bonding curve buy"""
        try:
            curve_data = self.get_bonding_curve_data(mint)
            if not curve_data:
                logger.warning(f"No curve data for {mint[:8]}...")
                return 0, 0
            
            sol_lamports = int(sol_amount * 1e9)
            
            # Get current reserves
            virtual_sol = curve_data['virtual_sol_reserves']
            virtual_tokens = curve_data['virtual_token_reserves']
            
            if virtual_sol <= 0 or virtual_tokens <= 0:
                logger.warning(f"Invalid reserves for {mint[:8]}...")
                return 0, 0
            
            # Constant product formula: tokens_out = (tokens * sol_in) / (sol + sol_in)
            tokens_out = (virtual_tokens * sol_lamports) // (virtual_sol + sol_lamports)
            
            # Calculate price impact
            new_virtual_sol = virtual_sol + sol_lamports
            new_virtual_tokens = virtual_tokens - tokens_out
            
            if new_virtual_tokens > 0:
                new_price = new_virtual_sol / new_virtual_tokens
                old_price = virtual_sol / virtual_tokens
                price_impact = ((new_price / old_price) - 1) * 100
            else:
                price_impact = 100  # Max impact
            
            # Apply 1% slippage tolerance
            min_tokens_out = int(tokens_out * 0.99)
            
            logger.info(f"💰 Buy calculation for {mint[:8]}...")
            logger.info(f"   SOL in: {sol_amount:.4f}")
            logger.info(f"   Expected tokens: {tokens_out:,.0f}")
            logger.info(f"   Price impact: {price_impact:.2f}%")
            
            return min_tokens_out, price_impact
            
        except Exception as e:
            logger.error(f"Failed to calculate buy amount: {e}")
            return 0, 0
    
    def create_buy_instruction(self, mint: str, sol_amount: float, min_tokens: int) -> Optional[Instruction]:
        """Create PumpFun buy instruction - FIXED account ordering"""
        try:
            mint_pubkey = Pubkey.from_string(mint)
            bonding_curve, _ = self.derive_bonding_curve_pda(mint_pubkey)
            
            # Get token accounts
            associated_bonding_curve = get_associated_token_address(bonding_curve, mint_pubkey)
            user_token_account = get_associated_token_address(self.wallet.pubkey, mint_pubkey)
            
            # Build instruction data
            # Discriminator for 'buy' = [102, 6, 61, 18, 1, 218, 235, 234]
            discriminator = bytes([102, 6, 61, 18, 1, 218, 235, 234])
            
            # Pack amounts (little-endian, 8 bytes each)
            sol_amount_lamports = int(sol_amount * 1e9)
            data = discriminator + struct.pack('<Q', sol_amount_lamports) + struct.pack('<Q', min_tokens)
            
            # Build accounts - FIXED: Removed PUMPFUN_PROGRAM_ID from accounts list
            accounts = [
                AccountMeta(pubkey=PUMPFUN_FEE_RECIPIENT, is_signer=False, is_writable=True),
                AccountMeta(pubkey=mint_pubkey, is_signer=False, is_writable=False),
                AccountMeta(pubkey=bonding_curve, is_signer=False, is_writable=True),
                AccountMeta(pubkey=associated_bonding_curve, is_signer=False, is_writable=True),
                AccountMeta(pubkey=user_token_account, is_signer=False, is_writable=True),
                AccountMeta(pubkey=self.wallet.pubkey, is_signer=True, is_writable=True),
                AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
                AccountMeta(pubkey=TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
                AccountMeta(pubkey=RENT_PROGRAM_ID, is_signer=False, is_writable=False),
                AccountMeta(pubkey=Pubkey.from_string("metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"), is_signer=False, is_writable=False),
                AccountMeta(pubkey=ASSOCIATED_TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
            ]
            
            return Instruction(
                program_id=PUMPFUN_PROGRAM_ID,
                accounts=accounts,
                data=data
            )
            
        except Exception as e:
            logger.error(f"Failed to create buy instruction: {e}")
            return None
    
    def execute_buy(self, mint: str) -> Optional[str]:
        """Execute a PumpFun bonding curve buy (original method)"""
        # Check feature flag
        if not self.USE_MANUAL_PUMPFUN_BUY:
            logger.info("Manual PumpFun buy path disabled for Phase-1 (using PumpPortal).")
            return None
        
        try:
            # Check if token can be bought
            curve_data = self.get_bonding_curve_data(mint)
            if not curve_data:
                logger.warning(f"❌ No bonding curve for {mint[:8]}...")
                return None
            
            if not curve_data['can_buy']:
                if curve_data['is_migrating']:
                    logger.warning(f"❌ Token {mint[:8]}... is migrating to Raydium")
                else:
                    logger.warning(f"❌ Bonding curve has insufficient SOL: {curve_data['sol_in_curve']:.2f}")
                return None
            
            # Calculate buy amount
            min_tokens, price_impact = self.calculate_buy_amount(mint, BUY_AMOUNT_SOL)
            if min_tokens <= 0:
                logger.warning(f"❌ Invalid token amount calculated")
                return None
            
            if price_impact > 5:
                logger.warning(f"❌ Price impact too high: {price_impact:.2f}%")
                return None
            
            # Create buy instruction
            buy_ix = self.create_buy_instruction(mint, BUY_AMOUNT_SOL, min_tokens)
            if not buy_ix:
                logger.error(f"❌ Failed to create buy instruction")
                return None
            
            # Build transaction
            recent_blockhash_resp = self.client.get_latest_blockhash()
            
            from solana.transaction import Transaction as SolanaTransaction
            
            transaction = SolanaTransaction()
            transaction.fee_payer = self.wallet.pubkey
            transaction.recent_blockhash = recent_blockhash_resp.value.blockhash
            
            # Add priority fees
            transaction.add(set_compute_unit_price(100_000))  # 0.0001 SOL priority
            transaction.add(set_compute_unit_limit(250_000))
            
            # Add buy instruction
            transaction.add(buy_ix)
            
            # Sign transaction
            transaction.sign_partial(self.wallet.keypair)
            
            logger.info(f"🚀 Sending PumpFun BUY for {mint[:8]}...")
            
            # Send transaction
            response = self.client.send_raw_transaction(transaction.serialize())
            sig_str = str(response.value)
            
            logger.info(f"✅ [PumpFun] BUY transaction sent: {sig_str}")
            
            # Record the buy in cache
            if mint not in self.bonding_curves_cache:
                self.bonding_curves_cache[mint] = {}
            self.bonding_curves_cache[mint]['last_action'] = 'buy'
            self.bonding_curves_cache[mint]['buy_sig'] = sig_str
            self.bonding_curves_cache[mint]['buy_amount'] = BUY_AMOUNT_SOL
            self.bonding_curves_cache[mint]['expected_tokens'] = min_tokens
            
            return sig_str
            
        except Exception as e:
            logger.error(f"❌ PumpFun buy failed: {e}")
            return None
    
    def execute_buy_with_curve(self, mint: str, bonding_curve_key: str = None) -> Optional[str]:
        """Execute buy with known bonding curve key"""
        # Check feature flag
        if not self.USE_MANUAL_PUMPFUN_BUY:
            logger.info("Manual PumpFun buy path disabled for Phase-1 (using PumpPortal).")
            return None
        
        try:
            if bonding_curve_key:
                # Use the provided bonding curve directly
                logger.info(f"Using bonding curve from WebSocket: {bonding_curve_key}")
                bonding_curve = Pubkey.from_string(bonding_curve_key)
                mint_pubkey = Pubkey.from_string(mint)
                
                # Calculate a reasonable buy amount (1M tokens estimate)
                min_tokens = int(1000000 * 1e6)
                
                # Build transaction directly
                buy_ix = self._create_buy_ix_with_curve(mint_pubkey, bonding_curve, BUY_AMOUNT_SOL, min_tokens)
                
                if buy_ix:
                    # Send transaction
                    recent_blockhash_resp = self.client.get_latest_blockhash()
                    
                    from solana.transaction import Transaction as SolanaTransaction
                    
                    transaction = SolanaTransaction()
                    transaction.fee_payer = self.wallet.pubkey
                    transaction.recent_blockhash = recent_blockhash_resp.value.blockhash
                    
                    # Add priority fees
                    transaction.add(set_compute_unit_price(100_000))
                    transaction.add(set_compute_unit_limit(250_000))
                    
                    # Add buy instruction
                    transaction.add(buy_ix)
                    
                    # Sign transaction
                    transaction.sign_partial(self.wallet.keypair)
                    
                    logger.info(f"🚀 Sending PumpFun BUY for {mint[:8]}... with curve {bonding_curve_key[:8]}...")
                    
                    # Send transaction
                    response = self.client.send_raw_transaction(transaction.serialize())
                    sig_str = str(response.value)
                    
                    logger.info(f"✅ [PumpFun] BUY transaction sent: {sig_str}")
                    return sig_str
                else:
                    logger.error("Failed to create buy instruction")
                    return None
            else:
                # Fall back to original method
                return self.execute_buy(mint)
                
        except Exception as e:
            logger.error(f"Buy with curve failed: {e}")
            # Try fallback to regular buy
            logger.info("Falling back to regular buy method...")
            return self.execute_buy(mint)
    
    def _create_buy_ix_with_curve(self, mint: Pubkey, bonding_curve: Pubkey, sol_amount: float, min_tokens: int) -> Optional[Instruction]:
        """Create buy instruction with known bonding curve - FIXED account ordering"""
        try:
            # Get token accounts
            associated_bonding_curve = get_associated_token_address(bonding_curve, mint)
            user_token_account = get_associated_token_address(self.wallet.pubkey, mint)
            
            # Build instruction data for PumpFun buy
            discriminator = bytes([102, 6, 61, 18, 1, 218, 235, 234])
            sol_amount_lamports = int(sol_amount * 1e9)
            data = discriminator + struct.pack('<Q', sol_amount_lamports) + struct.pack('<Q', min_tokens)
            
            # Build accounts in correct order for PumpFun - without program ID in accounts
            accounts = [
                AccountMeta(pubkey=bonding_curve, is_signer=False, is_writable=True),
                AccountMeta(pubkey=associated_bonding_curve, is_signer=False, is_writable=True),
                AccountMeta(pubkey=user_token_account, is_signer=False, is_writable=True),
                AccountMeta(pubkey=self.wallet.pubkey, is_signer=True, is_writable=True),
                AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
                AccountMeta(pubkey=TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
                AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
                AccountMeta(pubkey=ASSOCIATED_TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
                AccountMeta(pubkey=PUMPFUN_FEE_RECIPIENT, is_signer=False, is_writable=True),
            ]
            
            return Instruction(
                program_id=PUMPFUN_PROGRAM_ID,
                accounts=accounts,
                data=data
            )
            
        except Exception as e:
            logger.error(f"Failed to create buy instruction: {e}")
            return None
    
    def create_sell_instruction(self, mint: str, token_amount: int, min_sol: int) -> Optional[Instruction]:
        """Create PumpFun sell instruction - FIXED account ordering"""
        try:
            mint_pubkey = Pubkey.from_string(mint)
            bonding_curve, _ = self.derive_bonding_curve_pda(mint_pubkey)
            
            # Get token accounts
            associated_bonding_curve = get_associated_token_address(bonding_curve, mint_pubkey)
            user_token_account = get_associated_token_address(self.wallet.pubkey, mint_pubkey)
            
            # Build instruction data
            # Discriminator for 'sell' = [51, 230, 133, 164, 1, 127, 131, 173]
            discriminator = bytes([51, 230, 133, 164, 1, 127, 131, 173])
            
            # Pack amounts
            data = discriminator + struct.pack('<Q', token_amount) + struct.pack('<Q', min_sol)
            
            # Build accounts - FIXED: Removed PUMPFUN_PROGRAM_ID from accounts list
            accounts = [
                AccountMeta(pubkey=PUMPFUN_FEE_RECIPIENT, is_signer=False, is_writable=True),
                AccountMeta(pubkey=mint_pubkey, is_signer=False, is_writable=False),
                AccountMeta(pubkey=bonding_curve, is_signer=False, is_writable=True),
                AccountMeta(pubkey=associated_bonding_curve, is_signer=False, is_writable=True),
                AccountMeta(pubkey=user_token_account, is_signer=False, is_writable=True),
                AccountMeta(pubkey=self.wallet.pubkey, is_signer=True, is_writable=True),
                AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
                AccountMeta(pubkey=ASSOCIATED_TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
                AccountMeta(pubkey=TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
                AccountMeta(pubkey=Pubkey.from_string("metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"), is_signer=False, is_writable=False),
            ]
            
            return Instruction(
                program_id=PUMPFUN_PROGRAM_ID,
                accounts=accounts,
                data=data
            )
            
        except Exception as e:
            logger.error(f"Failed to create sell instruction: {e}")
            return None
    
    def execute_sell(self, mint: str, token_amount: int) -> Optional[str]:
        """Execute a PumpFun bonding curve sell"""
        # This method is not used in Phase 1 since we use PumpPortal
        if not self.USE_MANUAL_PUMPFUN_BUY:
            logger.info("Manual PumpFun sell path disabled (using PumpPortal)")
            return None
            
        try:
            # Check bonding curve still exists
            curve_data = self.get_bonding_curve_data(mint)
            if not curve_data:
                logger.warning(f"Token {mint[:8]}... may have migrated")
                return None
            
            # Calculate minimum SOL output (with slippage)
            if curve_data['virtual_token_reserves'] > 0:
                sol_out = (curve_data['virtual_sol_reserves'] * token_amount) // \
                         (curve_data['virtual_token_reserves'] + token_amount)
                min_sol_out = int(sol_out * 0.98)  # 2% slippage
            else:
                min_sol_out = 0
            
            # Create sell instruction
            sell_ix = self.create_sell_instruction(mint, token_amount, min_sol_out)
            if not sell_ix:
                return None
            
            # Build transaction
            recent_blockhash = self.client.get_latest_blockhash().value.blockhash
            
            from solana.transaction import Transaction as SolanaTransaction
            
            transaction = SolanaTransaction()
            transaction.recent_blockhash = recent_blockhash
            transaction.fee_payer = self.wallet.pubkey
            
            # Add priority fees
            transaction.add(set_compute_unit_price(100_000))
            transaction.add(set_compute_unit_limit(250_000))
            
            # Add sell instruction
            transaction.add(sell_ix)
            
            # Sign transaction
            transaction.sign_partial(self.wallet.keypair)
            
            logger.info(f"🚀 Sending PumpFun SELL for {mint[:8]}...")
            
            # Send transaction
            response = self.client.send_raw_transaction(transaction.serialize())
            sig_str = str(response.value)
            
            logger.info(f"✅ [PumpFun] SELL transaction sent: {sig_str}")
            
            return sig_str
            
        except Exception as e:
            logger.error(f"❌ PumpFun sell failed: {e}")
            return None
