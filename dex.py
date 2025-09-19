"""
DEX Integration - PumpFun Bonding Curves and Raydium
Focus: Direct bonding curve execution with correct calculations
"""

import time
import base64
import struct
import logging
from typing import Optional, Dict, Tuple
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.instruction import Instruction, AccountMeta
from solders.transaction import Transaction
from solders.compute_budget import set_compute_unit_price, set_compute_unit_limit
from solana.rpc.api import Client
from spl.token.instructions import get_associated_token_address

from config import (
    PUMPFUN_PROGRAM_ID, PUMPFUN_FEE_RECIPIENT,
    SYSTEM_PROGRAM_ID, TOKEN_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID,
    RENT_PROGRAM_ID, RPC_ENDPOINT, BUY_AMOUNT_SOL,
    MIN_BONDING_CURVE_SOL, MAX_BONDING_CURVE_SOL
)

logger = logging.getLogger(__name__)

class PumpFunDEX:
    """PumpFun bonding curve integration"""
    
    def __init__(self, wallet_manager):
        """Initialize with wallet manager"""
        self.wallet = wallet_manager
        self.client = Client(RPC_ENDPOINT)
        
        # Track bonding curve states
        self.bonding_curves = {}
        
    def derive_bonding_curve_pda(self, mint: Pubkey) -> Tuple[Pubkey, int]:
        """Derive the bonding curve PDA for a token"""
        seeds = [b"bonding-curve", bytes(mint)]
        return Pubkey.find_program_address(seeds, PUMPFUN_PROGRAM_ID)
    
    def get_bonding_curve_data(self, mint: str) -> Optional[Dict]:
        """Fetch and parse bonding curve data"""
        try:
            # The WebSocket gives us the bonding curve key directly!
            # Check if we have it from the token data
            if hasattr(self, 'last_token_data') and self.last_token_data:
                if self.last_token_data.get('mint') == mint:
                    bonding_curve_str = self.last_token_data.get('bondingCurveKey')
                    if bonding_curve_str:
                        logger.info(f"Using bonding curve from WebSocket: {bonding_curve_str}")
                        bonding_curve = Pubkey.from_string(bonding_curve_str)
                    else:
                        # Derive it
                        mint_pubkey = Pubkey.from_string(mint)
                        bonding_curve, _ = self.derive_bonding_curve_pda(mint_pubkey)
                else:
                    # Derive it normally
                    mint_pubkey = Pubkey.from_string(mint)
                    bonding_curve, _ = self.derive_bonding_curve_pda(mint_pubkey)
            else:
                # Derive it normally
                mint_pubkey = Pubkey.from_string(mint)
                bonding_curve, _ = self.derive_bonding_curve_pda(mint_pubkey)
            
            # Get account info
            response = self.client.get_account_info(bonding_curve)
            if not response.value:
                logger.debug(f"No bonding curve found for {mint[:8]}...")
                return None
            
            # Parse account data
            data = response.value.data
            if isinstance(data, list) and len(data) > 0:
                if isinstance(data[0], str):
                    decoded = base64.b64decode(data[0])
                else:
                    decoded = bytes(data)
                
                # Parse bonding curve structure
                # Discriminator (8) + virtual_token_reserves (8) + virtual_sol_reserves (8) + 
                # real_token_reserves (8) + real_sol_reserves (8) + ...
                if len(decoded) >= 40:
                    virtual_token_reserves = int.from_bytes(decoded[8:16], 'little')
                    virtual_sol_reserves = int.from_bytes(decoded[16:24], 'little')
                    real_token_reserves = int.from_bytes(decoded[24:32], 'little')
                    real_sol_reserves = int.from_bytes(decoded[32:40], 'little')
                    
                    # Calculate current price
                    if virtual_token_reserves > 0:
                        price_per_token = virtual_sol_reserves / virtual_token_reserves
                    else:
                        price_per_token = 0
                    
                    # Check if approaching migration
                    sol_in_curve = real_sol_reserves / 1e9
                    is_migrating = sol_in_curve >= MAX_BONDING_CURVE_SOL
                    
                    curve_data = {
                        'bonding_curve': str(bonding_curve),
                        'virtual_token_reserves': virtual_token_reserves,
                        'virtual_sol_reserves': virtual_sol_reserves,
                        'real_token_reserves': real_token_reserves,
                        'real_sol_reserves': real_sol_reserves,
                        'price_per_token': price_per_token,
                        'sol_in_curve': sol_in_curve,
                        'is_migrating': is_migrating,
                        'can_buy': sol_in_curve < MAX_BONDING_CURVE_SOL and sol_in_curve >= MIN_BONDING_CURVE_SOL
                    }
                    
                    # Cache the data
                    self.bonding_curves[mint] = {
                        'data': curve_data,
                        'timestamp': time.time()
                    }
                    
                    return curve_data
            
            return None
            
        except Exception as e:
            logger.error(f"Failed to get bonding curve data: {e}")
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
        """Create PumpFun buy instruction"""
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
            
            # Build accounts
            accounts = [
                AccountMeta(pubkey=PUMPFUN_PROGRAM_ID, is_signer=False, is_writable=False),
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
    
    def execute_buy_with_curve(self, mint: str, bonding_curve_key: str = None) -> Optional[str]:
        """Execute buy with known bonding curve key"""
        try:
            if bonding_curve_key:
                # Use the provided bonding curve directly
                logger.info(f"Using bonding curve from WebSocket: {bonding_curve_key}")
                bonding_curve = Pubkey.from_string(bonding_curve_key)
                
                # We need to verify it exists
                response = self.client.get_account_info(bonding_curve)
                if not response.value:
                    logger.warning(f"Bonding curve account doesn't exist yet")
                    return None
                    
                # Create buy instruction using this bonding curve
                mint_pubkey = Pubkey.from_string(mint)
                
                # Calculate a reasonable buy amount
                min_tokens = int(1000000 * 1e6)  # 1M tokens as estimate
                
                # Build transaction directly
                buy_ix = self._create_buy_ix_with_curve(mint_pubkey, bonding_curve, BUY_AMOUNT_SOL, min_tokens)
                
                if buy_ix:
                    # Send transaction
                    recent_blockhash = self.client.get_latest_blockhash().value.blockhash
                    
                    transaction = Transaction()
                    transaction.recent_blockhash = recent_blockhash
                    transaction.fee_payer = self.wallet.pubkey
                    
                    # Add priority fees
                    transaction.add(set_compute_unit_price(100_000))
                    transaction.add(set_compute_unit_limit(250_000))
                    
                    # Add buy instruction
                    transaction.add(buy_ix)
                    
                    # Sign and send
                    transaction.sign(self.wallet.keypair)
                    
                    logger.info(f"🚀 Sending PumpFun BUY for {mint[:8]}...")
                    signature = self.client.send_transaction(transaction, self.wallet.keypair)
                    sig_str = str(signature.value)
                    
                    logger.info(f"✅ [PumpFun] BUY transaction sent: {sig_str}")
                    return sig_str
                else:
                    logger.error("Failed to create buy instruction")
                    return None
            else:
                # Fall back to original method
                return self.execute_buy(mint)
                
        except Exception as e:
            logger.error(f"Buy failed: {e}")
            return None
    
    def _create_buy_ix_with_curve(self, mint: Pubkey, bonding_curve: Pubkey, sol_amount: float, min_tokens: int) -> Optional[Instruction]:
        """Create buy instruction with known bonding curve"""
        try:
            # Get token accounts
            associated_bonding_curve = get_associated_token_address(bonding_curve, mint)
            user_token_account = get_associated_token_address(self.wallet.pubkey, mint)
            
            # Build instruction data
            discriminator = bytes([102, 6, 61, 18, 1, 218, 235, 234])
            sol_amount_lamports = int(sol_amount * 1e9)
            data = discriminator + struct.pack('<Q', sol_amount_lamports) + struct.pack('<Q', min_tokens)
            
            # Build accounts
            accounts = [
                AccountMeta(pubkey=PUMPFUN_PROGRAM_ID, is_signer=False, is_writable=False),
                AccountMeta(pubkey=PUMPFUN_FEE_RECIPIENT, is_signer=False, is_writable=True),
                AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
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
            recent_blockhash = self.client.get_latest_blockhash().value.blockhash
            
            transaction = Transaction()
            transaction.recent_blockhash = recent_blockhash
            transaction.fee_payer = self.wallet.pubkey
            
            # Add priority fees
            transaction.add(set_compute_unit_price(100_000))  # 0.0001 SOL priority
            transaction.add(set_compute_unit_limit(250_000))
            
            # Add buy instruction
            transaction.add(buy_ix)
            
            # Sign and send
            transaction.sign(self.wallet.keypair)
            
            logger.info(f"🚀 Sending PumpFun BUY for {mint[:8]}...")
            signature = self.client.send_transaction(transaction, self.wallet.keypair)
            sig_str = str(signature.value)
            
            logger.info(f"✅ [PumpFun] BUY transaction sent: {sig_str}")
            
            # Record the buy
            self.bonding_curves[mint]['last_action'] = 'buy'
            self.bonding_curves[mint]['buy_sig'] = sig_str
            self.bonding_curves[mint]['buy_amount'] = BUY_AMOUNT_SOL
            self.bonding_curves[mint]['expected_tokens'] = min_tokens
            
            return sig_str
            
        except Exception as e:
            logger.error(f"❌ PumpFun buy failed: {e}")
            return None
    
    def create_sell_instruction(self, mint: str, token_amount: int, min_sol: int) -> Optional[Instruction]:
        """Create PumpFun sell instruction"""
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
            
            # Build accounts (same as buy)
            accounts = [
                AccountMeta(pubkey=PUMPFUN_PROGRAM_ID, is_signer=False, is_writable=False),
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
            
            transaction = Transaction()
            transaction.recent_blockhash = recent_blockhash
            transaction.fee_payer = self.wallet.pubkey
            
            # Add priority fees
            transaction.add(set_compute_unit_price(100_000))
            transaction.add(set_compute_unit_limit(250_000))
            
            # Add sell instruction
            transaction.add(sell_ix)
            
            # Sign and send
            transaction.sign(self.wallet.keypair)
            
            logger.info(f"🚀 Sending PumpFun SELL for {mint[:8]}...")
            signature = self.client.send_transaction(transaction, self.wallet.keypair)
            sig_str = str(signature.value)
            
            logger.info(f"✅ [PumpFun] SELL transaction sent: {sig_str}")
            
            return sig_str
            
        except Exception as e:
            logger.error(f"❌ PumpFun sell failed: {e}")
            return None
