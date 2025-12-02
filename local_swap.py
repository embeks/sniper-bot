"""
Local Pump.fun Swap Builder - Eliminates PumpPortal API latency
Saves 200-500ms per trade by building transactions locally
"""

import struct
import logging
import time
from typing import Optional, Tuple
from solders.pubkey import Pubkey
from solders.instruction import Instruction, AccountMeta
from solders.transaction import Transaction
from solders.message import Message
from solana.rpc.api import Client
from solana.rpc.types import TxOpts

from config import (
    PUMPFUN_PROGRAM_ID,
    PUMPFUN_FEE_RECIPIENT,
    TOKEN_PROGRAM_ID,
    TOKEN_2022_PROGRAM_ID,
    ASSOCIATED_TOKEN_PROGRAM_ID,
    SYSTEM_PROGRAM_ID,
    RENT_PROGRAM_ID,
)

logger = logging.getLogger(__name__)

# Pump.fun instruction discriminators (first 8 bytes of sha256("global:buy") etc)
BUY_DISCRIMINATOR = bytes([0x66, 0x06, 0x3d, 0x12, 0x01, 0xda, 0xeb, 0xea])
SELL_DISCRIMINATOR = bytes([0x33, 0xe6, 0x85, 0xa4, 0x01, 0x7f, 0x83, 0xad])


class LocalSwapBuilder:
    """Build Pump.fun swap transactions locally - no external API calls"""
    
    def __init__(self, wallet_manager, rpc_client: Client):
        self.wallet = wallet_manager
        self.client = rpc_client
        
        # Derive global PDA once (constant)
        self.global_pda = Pubkey.find_program_address(
            [b"global"],
            PUMPFUN_PROGRAM_ID
        )[0]
        
        # Event authority PDA
        self.event_authority = Pubkey.find_program_address(
            [b"__event_authority"],
            PUMPFUN_PROGRAM_ID
        )[0]
        
        logger.info(f"LocalSwapBuilder initialized")
        logger.info(f"  Global PDA: {self.global_pda}")
        logger.info(f"  Event Authority: {self.event_authority}")
    
    def derive_bonding_curve_pda(self, mint: Pubkey) -> Tuple[Pubkey, int]:
        """Derive bonding curve PDA for a token"""
        return Pubkey.find_program_address(
            [b"bonding-curve", bytes(mint)],
            PUMPFUN_PROGRAM_ID
        )
    
    def derive_associated_token_account(self, owner: Pubkey, mint: Pubkey) -> Pubkey:
        """Derive ATA address for Token-2022"""
        return Pubkey.find_program_address(
            [bytes(owner), bytes(TOKEN_2022_PROGRAM_ID), bytes(mint)],
            ASSOCIATED_TOKEN_PROGRAM_ID
        )[0]
    
    def calculate_tokens_out(
        self,
        sol_amount_lamports: int,
        virtual_sol_reserves: int,
        virtual_token_reserves: int
    ) -> int:
        """
        Calculate tokens received for SOL input (constant product AMM)
        Formula: tokens_out = (sol_in * token_reserves) / (sol_reserves + sol_in)
        """
        tokens_out = (sol_amount_lamports * virtual_token_reserves) // (virtual_sol_reserves + sol_amount_lamports)
        return tokens_out
    
    def calculate_sol_out(
        self,
        token_amount: int,
        virtual_sol_reserves: int,
        virtual_token_reserves: int
    ) -> int:
        """
        Calculate SOL received for token input (constant product AMM)
        Formula: sol_out = (token_in * sol_reserves) / (token_reserves + token_in)
        """
        sol_out = (token_amount * virtual_sol_reserves) // (virtual_token_reserves + token_amount)
        return sol_out
    
    def build_buy_instruction(
        self,
        mint: Pubkey,
        bonding_curve: Pubkey,
        associated_bonding_curve: Pubkey,
        user_ata: Pubkey,
        token_amount: int,
        max_sol_cost: int
    ) -> Instruction:
        """
        Build Pump.fun buy instruction
        
        Args:
            mint: Token mint address
            bonding_curve: Bonding curve PDA
            associated_bonding_curve: Bonding curve's token account
            user_ata: User's associated token account
            token_amount: Minimum tokens to receive (atomic units)
            max_sol_cost: Maximum SOL to spend (lamports)
        """
        # Instruction data: discriminator + token_amount (u64) + max_sol_cost (u64)
        data = BUY_DISCRIMINATOR + struct.pack('<Q', token_amount) + struct.pack('<Q', max_sol_cost)
        
        # Account order matters!
        accounts = [
            AccountMeta(self.global_pda, is_signer=False, is_writable=False),
            AccountMeta(PUMPFUN_FEE_RECIPIENT, is_signer=False, is_writable=True),
            AccountMeta(mint, is_signer=False, is_writable=False),
            AccountMeta(bonding_curve, is_signer=False, is_writable=True),
            AccountMeta(associated_bonding_curve, is_signer=False, is_writable=True),
            AccountMeta(user_ata, is_signer=False, is_writable=True),
            AccountMeta(self.wallet.pubkey, is_signer=True, is_writable=True),
            AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(TOKEN_2022_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(RENT_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(self.event_authority, is_signer=False, is_writable=False),
            AccountMeta(PUMPFUN_PROGRAM_ID, is_signer=False, is_writable=False),
        ]

        return Instruction(PUMPFUN_PROGRAM_ID, data, accounts)

    def build_sell_instruction(
        self,
        mint: Pubkey,
        bonding_curve: Pubkey,
        associated_bonding_curve: Pubkey,
        user_ata: Pubkey,
        token_amount: int,
        min_sol_output: int
    ) -> Instruction:
        """
        Build Pump.fun sell instruction
        
        Args:
            mint: Token mint address
            bonding_curve: Bonding curve PDA
            associated_bonding_curve: Bonding curve's token account
            user_ata: User's associated token account
            token_amount: Tokens to sell (atomic units)
            min_sol_output: Minimum SOL to receive (lamports)
        """
        # Instruction data: discriminator + token_amount (u64) + min_sol_output (u64)
        data = SELL_DISCRIMINATOR + struct.pack('<Q', token_amount) + struct.pack('<Q', min_sol_output)
        
        accounts = [
            AccountMeta(self.global_pda, is_signer=False, is_writable=False),
            AccountMeta(PUMPFUN_FEE_RECIPIENT, is_signer=False, is_writable=True),
            AccountMeta(mint, is_signer=False, is_writable=False),
            AccountMeta(bonding_curve, is_signer=False, is_writable=True),
            AccountMeta(associated_bonding_curve, is_signer=False, is_writable=True),
            AccountMeta(user_ata, is_signer=False, is_writable=True),
            AccountMeta(self.wallet.pubkey, is_signer=True, is_writable=True),
            AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(ASSOCIATED_TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(TOKEN_2022_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(self.event_authority, is_signer=False, is_writable=False),
            AccountMeta(PUMPFUN_PROGRAM_ID, is_signer=False, is_writable=False),
        ]

        return Instruction(PUMPFUN_PROGRAM_ID, data, accounts)
    
    async def create_buy_transaction(
        self,
        mint: str,
        sol_amount: float,
        curve_data: dict,
        slippage_bps: int = 3000  # 30% default (matching your current setting)
    ) -> Optional[str]:
        """
        Build and send a buy transaction locally
        
        Args:
            mint: Token mint address
            sol_amount: SOL to spend
            curve_data: Bonding curve data with virtual_sol_reserves and virtual_token_reserves
            slippage_bps: Slippage in basis points (3000 = 30%)
            
        Returns:
            Transaction signature or None on failure
        """
        try:
            start = time.time()
            
            mint_pubkey = Pubkey.from_string(mint)
            
            # Derive PDAs
            bonding_curve, _ = self.derive_bonding_curve_pda(mint_pubkey)
            associated_bonding_curve = self.derive_associated_token_account(bonding_curve, mint_pubkey)
            user_ata = self.derive_associated_token_account(self.wallet.pubkey, mint_pubkey)
            
            # Get reserves from curve_data
            virtual_sol = curve_data.get('virtual_sol_reserves', 0)
            virtual_tokens = curve_data.get('virtual_token_reserves', 0)
            
            if virtual_sol == 0 or virtual_tokens == 0:
                logger.error(f"Invalid curve data: sol={virtual_sol}, tokens={virtual_tokens}")
                return None
            
            # Calculate tokens out
            sol_lamports = int(sol_amount * 1e9)
            tokens_out = self.calculate_tokens_out(sol_lamports, virtual_sol, virtual_tokens)
            
            # Apply slippage to get minimum tokens (we're buying, so we want at least this many)
            min_tokens = int(tokens_out * (10000 - slippage_bps) / 10000)
            
            # Max SOL cost with slippage
            max_sol_cost = int(sol_lamports * (10000 + slippage_bps) / 10000)
            
            logger.info(f"⚡ Building LOCAL buy TX for {mint[:8]}...")
            logger.info(f"   SOL in: {sol_amount} ({sol_lamports:,} lamports)")
            logger.info(f"   Expected tokens: {tokens_out:,}")
            logger.info(f"   Min tokens ({slippage_bps/100:.0f}% slip): {min_tokens:,}")
            logger.info(f"   Max SOL cost: {max_sol_cost:,} lamports")
            
            # Build instruction
            buy_ix = self.build_buy_instruction(
                mint_pubkey,
                bonding_curve,
                associated_bonding_curve,
                user_ata,
                min_tokens,
                max_sol_cost
            )
            
            # Check if ATA exists, if not add create instruction
            ata_info = self.client.get_account_info(user_ata)
            instructions = []
            
            if not ata_info.value:
                # Manual ATA creation for Token-2022
                ata_accounts = [
                    AccountMeta(self.wallet.pubkey, is_signer=True, is_writable=True),
                    AccountMeta(user_ata, is_signer=False, is_writable=True),
                    AccountMeta(self.wallet.pubkey, is_signer=False, is_writable=False),
                    AccountMeta(mint_pubkey, is_signer=False, is_writable=False),
                    AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
                    AccountMeta(TOKEN_2022_PROGRAM_ID, is_signer=False, is_writable=False),
                ]
                create_ata_ix = Instruction(ASSOCIATED_TOKEN_PROGRAM_ID, bytes(), ata_accounts)
                instructions.append(create_ata_ix)
                logger.info(f"   Adding create ATA instruction (Token-2022)")
            
            instructions.append(buy_ix)
            
            # Get recent blockhash
            blockhash_resp = self.client.get_latest_blockhash()
            recent_blockhash = blockhash_resp.value.blockhash
            
            # Build and sign transaction
            message = Message.new_with_blockhash(
                instructions,
                self.wallet.pubkey,
                recent_blockhash
            )
            
            tx = Transaction.new_unsigned(message)
            tx.sign([self.wallet.keypair], recent_blockhash)
            
            build_time = (time.time() - start) * 1000
            logger.info(f"   ⚡ TX built in {build_time:.1f}ms (vs 200-500ms PumpPortal)")
            
            # Send transaction
            opts = TxOpts(skip_preflight=True, preflight_commitment="processed")
            
            response = self.client.send_raw_transaction(bytes(tx), opts)
            sig = str(response.value)
            
            if sig.startswith("1111111"):
                logger.error("Transaction failed - invalid signature")
                return None
            
            total_time = (time.time() - start) * 1000
            logger.info(f"✅ LOCAL buy TX sent in {total_time:.1f}ms: {sig}")
            
            return sig
            
        except Exception as e:
            logger.error(f"Failed to create local buy transaction: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None
    
    async def create_sell_transaction(
        self,
        mint: str,
        token_amount_ui: float,
        curve_data: dict,
        slippage_bps: int = 5000,  # 50% default for sells
        token_decimals: int = 6
    ) -> Optional[str]:
        """
        Build and send a sell transaction locally
        
        Args:
            mint: Token mint address  
            token_amount_ui: Tokens to sell (UI/human-readable amount)
            curve_data: Bonding curve data
            slippage_bps: Slippage in basis points
            token_decimals: Token decimals (default 6 for PumpFun)
            
        Returns:
            Transaction signature or None on failure
        """
        try:
            start = time.time()
            
            mint_pubkey = Pubkey.from_string(mint)
            
            # Convert UI amount to atomic
            token_amount = int(token_amount_ui * (10 ** token_decimals))
            
            # Derive PDAs
            bonding_curve, _ = self.derive_bonding_curve_pda(mint_pubkey)
            associated_bonding_curve = self.derive_associated_token_account(bonding_curve, mint_pubkey)
            user_ata = self.derive_associated_token_account(self.wallet.pubkey, mint_pubkey)
            
            # Get reserves
            virtual_sol = curve_data.get('virtual_sol_reserves', 0)
            virtual_tokens = curve_data.get('virtual_token_reserves', 0)
            
            if virtual_sol == 0 or virtual_tokens == 0:
                logger.error(f"Invalid curve data for sell: sol={virtual_sol}, tokens={virtual_tokens}")
                return None
            
            # Calculate SOL out
            sol_out = self.calculate_sol_out(token_amount, virtual_sol, virtual_tokens)
            
            # Apply slippage for minimum SOL output
            min_sol_output = int(sol_out * (10000 - slippage_bps) / 10000)
            
            logger.info(f"⚡ Building LOCAL sell TX for {mint[:8]}...")
            logger.info(f"   Tokens: {token_amount_ui:,.2f} ({token_amount:,} atomic)")
            logger.info(f"   Expected SOL: {sol_out / 1e9:.6f}")
            logger.info(f"   Min SOL ({slippage_bps/100:.0f}% slip): {min_sol_output / 1e9:.6f}")
            
            # Build instruction
            sell_ix = self.build_sell_instruction(
                mint_pubkey,
                bonding_curve,
                associated_bonding_curve,
                user_ata,
                token_amount,
                min_sol_output
            )
            
            # Get recent blockhash
            blockhash_resp = self.client.get_latest_blockhash()
            recent_blockhash = blockhash_resp.value.blockhash
            
            # Build and sign transaction
            message = Message.new_with_blockhash(
                [sell_ix],
                self.wallet.pubkey,
                recent_blockhash
            )
            
            tx = Transaction.new_unsigned(message)
            tx.sign([self.wallet.keypair], recent_blockhash)
            
            build_time = (time.time() - start) * 1000
            logger.info(f"   ⚡ TX built in {build_time:.1f}ms")
            
            # Send transaction
            opts = TxOpts(skip_preflight=True, preflight_commitment="processed")
            
            response = self.client.send_raw_transaction(bytes(tx), opts)
            sig = str(response.value)
            
            if sig.startswith("1111111"):
                logger.error("Transaction failed - invalid signature")
                return None
            
            total_time = (time.time() - start) * 1000
            logger.info(f"✅ LOCAL sell TX sent in {total_time:.1f}ms: {sig}")
            
            return sig
            
        except Exception as e:
            logger.error(f"Failed to create local sell transaction: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None
