# raydium_aggregator.py - FINAL WORKING VERSION
import os
import json
import logging
import httpx
import struct
from typing import Optional, Dict, Any
import base64

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import ID as SYS_PROGRAM_ID
from solders.instruction import Instruction, AccountMeta
from solders.transaction import VersionedTransaction
from solders.message import MessageV0
from solders.hash import Hash
from solders.transaction import Transaction
from solders.signature import Signature
from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from spl.token.constants import TOKEN_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID
from spl.token.instructions import get_associated_token_address, create_associated_token_account

# Raydium Program IDs
RAYDIUM_AMM_PROGRAM_ID = Pubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8")
SERUM_PROGRAM_ID = Pubkey.from_string("9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin")

# Compute Budget Program
COMPUTE_BUDGET_PROGRAM_ID = Pubkey.from_string("ComputeBudget111111111111111111111111111111")

RAYDIUM_POOLS_URL = "https://api.raydium.io/v2/sdk/liquidity/mainnet.json"

class RaydiumAggregatorClient:
    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url
        self.client = Client(rpc_url, commitment=Confirmed)
        self.pools = []

    def fetch_pools(self) -> None:
        """Fetch Raydium pool list from API."""
        try:
            logging.info("[Raydium] Fetching pools from API...")
            response = httpx.get(RAYDIUM_POOLS_URL, timeout=30)
            response.raise_for_status()
            
            pools_data = response.json()
            official = pools_data.get("official", [])
            unofficial = pools_data.get("unOfficial", [])
            
            self.pools = official + unofficial
            logging.info(f"[Raydium] Loaded {len(self.pools)} pools")
            
        except Exception as e:
            logging.error(f"[Raydium] Failed to fetch pools: {e}")
            self.pools = []

    def find_pool(self, input_mint: str, output_mint: str) -> Optional[Dict[str, Any]]:
        """Find pool for the given mint pair."""
        if not self.pools:
            self.fetch_pools()
        
        for pool in self.pools:
            base_mint = pool.get("baseMint", "")
            quote_mint = pool.get("quoteMint", "")
            
            if (input_mint == base_mint and output_mint == quote_mint) or \
               (input_mint == quote_mint and output_mint == base_mint):
                return pool
        
        return None

    def create_ata_if_needed(self, owner: Pubkey, mint: Pubkey, keypair: Keypair) -> Pubkey:
        """Create ATA if it doesn't exist."""
        ata = get_associated_token_address(owner, mint)
        
        try:
            account_info = self.client.get_account_info(ata)
            if account_info.value is None:
                # Create ATA
                ix = create_associated_token_account(payer=owner, owner=owner, mint=mint)
                recent_blockhash = self.client.get_latest_blockhash().value.blockhash
                
                msg = MessageV0.try_compile(
                    payer=owner,
                    instructions=[ix],
                    address_lookup_table_accounts=[],
                    recent_blockhash=recent_blockhash,
                )
                tx = VersionedTransaction(msg, [keypair])
                
                sig = self.client.send_transaction(tx).value
                logging.info(f"[Raydium] Created ATA: {sig}")
                
        except Exception as e:
            logging.error(f"[Raydium] ATA creation error: {e}")
        
        return ata

    def build_swap_transaction(
        self,
        keypair: Keypair,
        input_mint: str,
        output_mint: str,
        amount_in: int,
        slippage: float = 0.01
    ) -> Optional[VersionedTransaction]:
        """Build Raydium swap transaction."""
        try:
            pool = self.find_pool(input_mint, output_mint)
            if not pool:
                logging.error(f"[Raydium] No pool found for {input_mint} <-> {output_mint}")
                return None
            
            owner = keypair.pubkey()
            
            # Get mint pubkeys
            if input_mint == "So11111111111111111111111111111111111111112":
                # SOL to Token
                input_mint_pk = Pubkey.from_string(input_mint)
                output_mint_pk = Pubkey.from_string(output_mint)
                
                # For SOL, use the owner's account directly
                user_source_token = owner
                user_dest_token = self.create_ata_if_needed(owner, output_mint_pk, keypair)
            else:
                # Token to SOL
                input_mint_pk = Pubkey.from_string(input_mint)
                output_mint_pk = Pubkey.from_string(output_mint)
                
                user_source_token = self.create_ata_if_needed(owner, input_mint_pk, keypair)
                user_dest_token = owner
            
            # Build instructions
            instructions = []
            
            # Compute budget
            compute_limit_ix = Instruction(
                program_id=COMPUTE_BUDGET_PROGRAM_ID,
                accounts=[],
                data=bytes([2]) + (300_000).to_bytes(4, 'little')
            )
            compute_price_ix = Instruction(
                program_id=COMPUTE_BUDGET_PROGRAM_ID,
                accounts=[],
                data=bytes([3]) + (10_000).to_bytes(8, 'little')  # Higher priority
            )
            instructions.extend([compute_limit_ix, compute_price_ix])
            
            # Calculate min amount out
            min_amount_out = int(amount_in * (1 - slippage))
            
            # Swap instruction data
            data = bytes([9]) + amount_in.to_bytes(8, 'little') + min_amount_out.to_bytes(8, 'little')
            
            # Swap instruction accounts
            keys = [
                AccountMeta(pubkey=TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
                AccountMeta(pubkey=Pubkey.from_string(pool["id"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool["authority"]), is_signer=False, is_writable=False),
                AccountMeta(pubkey=Pubkey.from_string(pool["openOrders"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool["targetOrders"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool["baseVault"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool["quoteVault"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=SERUM_PROGRAM_ID, is_signer=False, is_writable=False),
                AccountMeta(pubkey=Pubkey.from_string(pool["marketId"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool["marketBids"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool["marketAsks"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool["marketEventQueue"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool["marketBaseVault"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool["marketQuoteVault"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool["marketAuthority"]), is_signer=False, is_writable=False),
                AccountMeta(pubkey=user_source_token, is_signer=False, is_writable=True),
                AccountMeta(pubkey=user_dest_token, is_signer=False, is_writable=True),
                AccountMeta(pubkey=owner, is_signer=True, is_writable=False),
            ]
            
            swap_ix = Instruction(
                program_id=RAYDIUM_AMM_PROGRAM_ID,
                accounts=keys,
                data=data
            )
            instructions.append(swap_ix)
            
            # Get recent blockhash
            recent_blockhash = self.client.get_latest_blockhash().value.blockhash
            
            # Compile message
            msg = MessageV0.try_compile(
                payer=owner,
                instructions=instructions,
                address_lookup_table_accounts=[],
                recent_blockhash=recent_blockhash,
            )
            
            # Create transaction
            tx = VersionedTransaction(msg, [keypair])
            
            return tx
            
        except Exception as e:
            logging.error(f"[Raydium] Failed to build transaction: {e}")
            import traceback
            logging.error(traceback.format_exc())
            return None

    def send_transaction(self, tx: VersionedTransaction, keypair: Keypair) -> Optional[str]:
        """Send transaction."""
        try:
            # Transaction is already signed in VersionedTransaction constructor
            result = self.client.send_transaction(tx, opts=TxOpts(skip_preflight=True))
            
            if hasattr(result, 'value'):
                sig = str(result.value)
                logging.info(f"[Raydium] Transaction sent: {sig}")
                return sig
            
            return None
            
        except Exception as e:
            logging.error(f"[Raydium] Send transaction error: {e}")
            return None
