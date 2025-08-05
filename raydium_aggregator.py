# raydium_aggregator.py - FIXED VERSION (Compatible with older solders)
import os
import json
import logging
import httpx
import struct
from typing import Optional, Dict, Any

from solders.keypair import Keypair as SoldersKeypair
from solders.pubkey import Pubkey as SoldersPubkey
from solders.system_program import ID as SYS_PROGRAM_ID
from solders.instruction import Instruction, AccountMeta
from solders.transaction import Transaction
from solders.message import Message
from solana.rpc.api import Client
from solana.rpc.types import TxOpts
from spl.token.constants import TOKEN_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID
from spl.token.instructions import get_associated_token_address, create_associated_token_account

# Raydium Program IDs
RAYDIUM_AMM_PROGRAM_ID = SoldersPubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8")
RAYDIUM_AUTHORITY = SoldersPubkey.from_string("5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1")
SERUM_PROGRAM_ID = SoldersPubkey.from_string("9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin")

# Compute Budget Program
COMPUTE_BUDGET_PROGRAM_ID = SoldersPubkey.from_string("ComputeBudget111111111111111111111111111111")

RAYDIUM_POOLS_URL = "https://api.raydium.io/v2/sdk/liquidity/mainnet.json"

class RaydiumAggregatorClient:
    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url
        self.client = Client(rpc_url)
        self.pools = []
        # Don't fetch pools on init - fetch only when needed

    def fetch_pools(self, max_retries: int = 3, delay: int = 2) -> None:
        """Fetch Raydium pool list from API with retry logic."""
        # Clear existing pools to free memory
        self.pools = []
        
        for attempt in range(max_retries):
            try:
                logging.info(f"[Raydium] Fetching pools from API (attempt {attempt+1}/{max_retries})")
                
                # Use streaming to reduce memory usage
                with httpx.stream("GET", RAYDIUM_POOLS_URL, timeout=30) as r:
                    r.raise_for_status()
                    content = b""
                    for chunk in r.iter_bytes():
                        content += chunk
                    
                pools_data = json.loads(content)
                
                official = pools_data.get("official", [])
                unofficial = pools_data.get("unOfficial", [])
                
                # Only store essential pool data to save memory
                self.pools = []
                for pool in official + unofficial:
                    # Only keep pools with both mints present
                    if pool.get("baseMint") and pool.get("quoteMint"):
                        # Store only essential fields
                        self.pools.append({
                            "id": pool.get("id"),
                            "baseMint": pool.get("baseMint"),
                            "quoteMint": pool.get("quoteMint"),
                            "version": pool.get("version", 4),
                            "authority": pool.get("authority"),
                            "openOrders": pool.get("openOrders"),
                            "targetOrders": pool.get("targetOrders"),
                            "baseVault": pool.get("baseVault"),
                            "quoteVault": pool.get("quoteVault"),
                            "marketId": pool.get("marketId"),
                            "marketAuthority": pool.get("marketAuthority"),
                            "marketBaseVault": pool.get("marketBaseVault"),
                            "marketQuoteVault": pool.get("marketQuoteVault"),
                            "marketBids": pool.get("marketBids"),
                            "marketAsks": pool.get("marketAsks"),
                            "marketEventQueue": pool.get("marketEventQueue")
                        })
                
                logging.info(f"[Raydium] Pools loaded: {len(self.pools)}")
                
                if self.pools:
                    preview = [f"{p.get('baseMint')[:8]}...{p.get('baseMint')[-4:]} <-> {p.get('quoteMint')[:8]}...{p.get('quoteMint')[-4:]}" for p in self.pools[:3]]
                    logging.info(f"[Raydium] First 3 pool pairs: {preview}")
                else:
                    logging.warning("[Raydium] Pool list is EMPTY after API fetch!")
                
                # Clear the raw data to free memory
                del pools_data
                del content
                
                return
            except Exception as e:
                logging.error(f"[Raydium] Failed to fetch pools (attempt {attempt+1}): {e}")
                if attempt < max_retries - 1:
                    import time
                    time.sleep(delay)
        
        logging.error("[Raydium] Failed to fetch pools after all retries")
        self.pools = []

    def find_pool(self, input_mint: str, output_mint: str) -> Optional[Dict[str, Any]]:
        """Find pool for the given mint pair."""
        if not self.pools:
            self.fetch_pools()
        
        # Normalize mint addresses
        input_mint = str(input_mint)
        output_mint = str(output_mint)
        
        for pool in self.pools:
            base_mint = pool.get("baseMint", "")
            quote_mint = pool.get("quoteMint", "")
            
            if (input_mint == base_mint and output_mint == quote_mint) or \
               (input_mint == quote_mint and output_mint == base_mint):
                logging.info(f"[Raydium] Pool found: {pool.get('id')} for {input_mint} <-> {output_mint}")
                return pool
        
        logging.warning(f"[Raydium] No pool found for {input_mint} <-> {output_mint}")
        return None

    def ensure_ata_exists(self, owner: SoldersPubkey, mint: SoldersPubkey, keypair: SoldersKeypair) -> SoldersPubkey:
        """Ensure ATA exists, create if needed."""
        ata = get_associated_token_address(owner, mint)
        
        try:
            response = self.client.get_account_info(ata)
            if response.value is None:
                logging.info(f"[Raydium] Creating ATA for mint {mint}")
                
                create_ix = create_associated_token_account(
                    payer=owner,
                    owner=owner,
                    mint=mint
                )
                
                recent_blockhash = self.client.get_latest_blockhash().value.blockhash
                tx = Transaction.new_with_payer([create_ix], owner)
                tx.recent_blockhash = recent_blockhash
                tx.sign([keypair])
                
                sig = self.client.send_raw_transaction(bytes(tx))
                logging.info(f"[Raydium] ATA created, tx: {sig.value}")
                
                # Wait for confirmation
                import time
                time.sleep(2)
                
        except Exception as e:
            logging.error(f"[Raydium] Error checking/creating ATA: {e}")
        
        return ata

    def build_compute_budget_instructions(self) -> list[Instruction]:
        """Build compute budget instructions manually."""
        instructions = []
        
        # Set compute unit limit instruction
        # Instruction format: [0, limit(u32)]
        compute_limit_data = struct.pack("<BI", 2, 300000)  # 2 = SetComputeUnitLimit, 300k units
        compute_limit_ix = Instruction(
            program_id=COMPUTE_BUDGET_PROGRAM_ID,
            accounts=[],
            data=compute_limit_data
        )
        instructions.append(compute_limit_ix)
        
        # Set compute unit price instruction
        # Instruction format: [3, price(u64)]
        compute_price_data = struct.pack("<BQ", 3, 1000)  # 3 = SetComputeUnitPrice, 1000 microlamports
        compute_price_ix = Instruction(
            program_id=COMPUTE_BUDGET_PROGRAM_ID,
            accounts=[],
            data=compute_price_data
        )
        instructions.append(compute_price_ix)
        
        return instructions

    def build_swap_instruction(
        self,
        pool: Dict[str, Any],
        user_source_token: SoldersPubkey,
        user_dest_token: SoldersPubkey,
        user_owner: SoldersPubkey,
        amount_in: int,
        min_amount_out: int
    ) -> Instruction:
        """Build Raydium swap instruction."""
        
        # Swap instruction discriminator (9 for swap)
        discriminator = struct.pack("<B", 9)
        # Pack amount_in and min_amount_out as u64
        data = discriminator + struct.pack("<QQ", amount_in, min_amount_out)
        
        keys = [
            AccountMeta(pubkey=TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(pubkey=SoldersPubkey.from_string(pool["id"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=SoldersPubkey.from_string(pool["authority"]), is_signer=False, is_writable=False),
            AccountMeta(pubkey=SoldersPubkey.from_string(pool["openOrders"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=SoldersPubkey.from_string(pool["targetOrders"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=SoldersPubkey.from_string(pool["baseVault"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=SoldersPubkey.from_string(pool["quoteVault"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=SERUM_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(pubkey=SoldersPubkey.from_string(pool["marketId"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=SoldersPubkey.from_string(pool["marketBids"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=SoldersPubkey.from_string(pool["marketAsks"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=SoldersPubkey.from_string(pool["marketEventQueue"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=SoldersPubkey.from_string(pool["marketBaseVault"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=SoldersPubkey.from_string(pool["marketQuoteVault"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=SoldersPubkey.from_string(pool["marketAuthority"]), is_signer=False, is_writable=False),
            AccountMeta(pubkey=user_source_token, is_signer=False, is_writable=True),
            AccountMeta(pubkey=user_dest_token, is_signer=False, is_writable=True),
            AccountMeta(pubkey=user_owner, is_signer=True, is_writable=False),
        ]
        
        return Instruction(
            program_id=RAYDIUM_AMM_PROGRAM_ID,
            accounts=keys,
            data=data
        )

    def build_swap_transaction(
        self,
        keypair: SoldersKeypair,
        input_mint: str,
        output_mint: str,
        amount_in: int,
        slippage: float = 0.01
    ) -> Optional[Transaction]:
        """Build complete swap transaction."""
        try:
            # Find pool
            pool = self.find_pool(input_mint, output_mint)
            if not pool:
                logging.error(f"[Raydium] No pool found for {input_mint} <-> {output_mint}")
                return None
            
            owner = keypair.pubkey()
            input_mint_pubkey = SoldersPubkey.from_string(input_mint)
            output_mint_pubkey = SoldersPubkey.from_string(output_mint)
            
            # Ensure ATAs exist
            user_source_ata = self.ensure_ata_exists(owner, input_mint_pubkey, keypair)
            user_dest_ata = self.ensure_ata_exists(owner, output_mint_pubkey, keypair)
            
            # Calculate minimum output with slippage
            # For a simple estimate, we'll use the slippage directly
            # In production, you'd want to calculate based on pool reserves
            min_amount_out = int(amount_in * (1 - slippage))
            
            # Determine swap direction
            if input_mint == pool["baseMint"]:
                # Swapping base to quote
                user_source_token = user_source_ata
                user_dest_token = user_dest_ata
            else:
                # Swapping quote to base
                user_source_token = user_source_ata
                user_dest_token = user_dest_ata
            
            # Build instructions
            instructions = []
            
            # Add compute budget instructions
            instructions.extend(self.build_compute_budget_instructions())
            
            # Build swap instruction
            swap_ix = self.build_swap_instruction(
                pool=pool,
                user_source_token=user_source_token,
                user_dest_token=user_dest_token,
                user_owner=owner,
                amount_in=amount_in,
                min_amount_out=min_amount_out
            )
            instructions.append(swap_ix)
            
            # Get recent blockhash
            recent_blockhash = self.client.get_latest_blockhash().value.blockhash
            
            # Build transaction
            tx = Transaction.new_with_payer(instructions, owner)
            tx.recent_blockhash = recent_blockhash
            
            logging.info(f"[Raydium] Swap TX built successfully for {input_mint} -> {output_mint}")
            return tx
            
        except Exception as e:
            logging.error(f"[Raydium] Failed to build swap transaction: {e}")
            import traceback
            logging.error(traceback.format_exc())
            return None

    def send_transaction(self, tx: Transaction, keypair: SoldersKeypair) -> Optional[str]:
        """Send transaction with proper error handling."""
        try:
            # Sign transaction
            tx.sign([keypair])
            
            # Send transaction
            opts = TxOpts(skip_preflight=True, preflight_commitment="confirmed")
            response = self.client.send_raw_transaction(bytes(tx), opts)
            
            if hasattr(response, 'value'):
                sig = str(response.value)
                logging.info(f"[Raydium] Transaction sent successfully: {sig}")
                return sig
            else:
                logging.error(f"[Raydium] Failed to send transaction: {response}")
                return None
                
        except Exception as e:
            logging.error(f"[Raydium] Transaction send error: {e}")
            import traceback
            logging.error(traceback.format_exc())
            return None
