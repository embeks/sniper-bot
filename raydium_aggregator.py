import os
import json
import logging
import httpx
import struct
from typing import Optional, Dict, Any, Tuple
import base64
import base58
import time

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import ID as SYS_PROGRAM_ID
from solders.instruction import Instruction, AccountMeta
from solders.transaction import VersionedTransaction
from solders.message import MessageV0
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from spl.token.constants import TOKEN_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID
from spl.token.instructions import get_associated_token_address, create_associated_token_account

# Raydium Program IDs
RAYDIUM_AMM_PROGRAM_ID = Pubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8")
RAYDIUM_AUTHORITY = Pubkey.from_string("5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1")
SERUM_PROGRAM_ID = Pubkey.from_string("srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX")  # Correct Serum V3 ID
WSOL_MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")

class FixedRaydiumSwapBuilder:
    """Fixed Raydium V4 Swap Builder that actually works"""
    
    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url
        self.client = Client(rpc_url, commitment=Confirmed)
        self.pool_cache = {}
        
    def derive_market_authority(self, market_id: Pubkey) -> Pubkey:
        """Derive the market authority PDA for Serum market"""
        seeds = [bytes(market_id)]
        # Find PDA with nonce
        for nonce in range(0, 256):
            try:
                seeds_with_nonce = seeds + [bytes([nonce])]
                pda, _ = Pubkey.find_program_address(seeds_with_nonce, SERUM_PROGRAM_ID)
                return pda
            except:
                continue
        # Fallback to default
        return RAYDIUM_AUTHORITY
    
    def fetch_complete_pool_keys(self, amm_id: str) -> Optional[Dict[str, Any]]:
        """Fetch complete pool keys including market data"""
        try:
            amm_pubkey = Pubkey.from_string(amm_id)
            
            # Fetch AMM account
            response = self.client.get_account_info(amm_pubkey)
            if not response.value or not response.value.data:
                logging.error(f"Failed to fetch AMM account: {amm_id}")
                return None
            
            # Decode account data
            data = base64.b64decode(response.value.data[0])
            
            # Parse AMM V4 layout (offsets for important fields)
            # Status: offset 0 (8 bytes)
            # Nonce: offset 8 (1 byte)
            # Order num: offset 9 (8 bytes)
            # Depth: offset 17 (8 bytes)
            # Coin decimals: offset 25 (1 byte)
            # PC decimals: offset 26 (1 byte)
            # State: offset 27 (1 byte)
            # Reset flag: offset 28 (1 byte)
            # Min size: offset 29 (8 bytes)
            # Vol max cut ratio: offset 37 (2 bytes)
            # Amount wave: offset 39 (8 bytes)
            # Coin lot size: offset 47 (8 bytes)
            # PC lot size: offset 55 (8 bytes)
            # Min price multiplier: offset 63 (8 bytes)
            # Max price multiplier: offset 71 (8 bytes)
            # System decimals: offset 79 (8 bytes)
            
            # Public keys start at offset 87
            offset = 87
            
            # Read all pubkeys in order
            coin_vault = Pubkey.from_bytes(data[offset:offset+32])
            offset += 32
            pc_vault = Pubkey.from_bytes(data[offset:offset+32])
            offset += 32
            coin_mint = Pubkey.from_bytes(data[offset:offset+32])
            offset += 32
            pc_mint = Pubkey.from_bytes(data[offset:offset+32])
            offset += 32
            lp_mint = Pubkey.from_bytes(data[offset:offset+32])
            offset += 32
            open_orders = Pubkey.from_bytes(data[offset:offset+32])
            offset += 32
            market_id = Pubkey.from_bytes(data[offset:offset+32])
            offset += 32
            market_program_id = Pubkey.from_bytes(data[offset:offset+32])
            offset += 32
            target_orders = Pubkey.from_bytes(data[offset:offset+32])
            offset += 32
            withdraw_queue = Pubkey.from_bytes(data[offset:offset+32])
            offset += 32
            token_temp_account = Pubkey.from_bytes(data[offset:offset+32])
            offset += 32
            amm_owner = Pubkey.from_bytes(data[offset:offset+32])
            offset += 32
            pnl_owner = Pubkey.from_bytes(data[offset:offset+32])
            
            # Now fetch the market account to get bids, asks, etc.
            market_response = self.client.get_account_info(market_id)
            if not market_response.value:
                logging.error(f"Failed to fetch market account: {market_id}")
                return None
            
            market_data = base64.b64decode(market_response.value.data[0])
            
            # Parse Serum market layout
            # Skip to the pubkey section (offset varies by market version)
            # For Serum V3: offset is 45
            market_offset = 45
            
            market_coin_vault = Pubkey.from_bytes(market_data[market_offset:market_offset+32])
            market_offset += 32
            market_pc_vault = Pubkey.from_bytes(market_data[market_offset:market_offset+32])
            market_offset += 32
            
            # Skip mint fields (we already have them)
            market_offset += 64
            
            market_coin_lot_size = int.from_bytes(market_data[market_offset:market_offset+8], 'little')
            market_offset += 8
            market_pc_lot_size = int.from_bytes(market_data[market_offset:market_offset+8], 'little')
            market_offset += 8
            
            # Skip fee fields
            market_offset += 16
            
            market_bids = Pubkey.from_bytes(market_data[market_offset:market_offset+32])
            market_offset += 32
            market_asks = Pubkey.from_bytes(market_data[market_offset:market_offset+32])
            market_offset += 32
            market_event_queue = Pubkey.from_bytes(market_data[market_offset:market_offset+32])
            
            # Derive market authority
            market_authority = self.derive_market_authority(market_id)
            
            pool_keys = {
                "id": amm_id,
                "baseMint": str(coin_mint),
                "quoteMint": str(pc_mint),
                "lpMint": str(lp_mint),
                "baseVault": str(coin_vault),
                "quoteVault": str(pc_vault),
                "openOrders": str(open_orders),
                "targetOrders": str(target_orders),
                "withdrawQueue": str(withdraw_queue),
                "marketId": str(market_id),
                "marketProgramId": str(market_program_id),
                "marketAuthority": str(market_authority),
                "marketBaseVault": str(market_coin_vault),
                "marketQuoteVault": str(market_pc_vault),
                "marketBids": str(market_bids),
                "marketAsks": str(market_asks),
                "marketEventQueue": str(market_event_queue),
                "authority": str(amm_owner),
                "version": 4,
                "programId": str(RAYDIUM_AMM_PROGRAM_ID)
            }
            
            logging.info(f"[Raydium] Successfully fetched complete pool keys for {amm_id[:8]}...")
            return pool_keys
            
        except Exception as e:
            logging.error(f"[Raydium] Failed to fetch pool keys: {e}")
            import traceback
            logging.error(traceback.format_exc())
            return None
    
    def find_pool_by_mints(self, token_mint: str) -> Optional[Dict[str, Any]]:
        """Find pool by searching for AMM accounts with the given mints"""
        try:
            # Use getProgramAccounts with filters
            filters = [
                {
                    "dataSize": 752  # Raydium V4 AMM account size
                },
                {
                    "memcmp": {
                        "offset": 119,  # Coin mint offset
                        "bytes": base58.b58encode(bytes(Pubkey.from_string(token_mint))).decode()
                    }
                }
            ]
            
            # Alternative: Check PC mint position for WSOL
            filters_alt = [
                {
                    "dataSize": 752
                },
                {
                    "memcmp": {
                        "offset": 151,  # PC mint offset
                        "bytes": base58.b58encode(bytes(Pubkey.from_string(token_mint))).decode()
                    }
                }
            ]
            
            # Try both filters
            for filter_set in [filters, filters_alt]:
                accounts = self.client.get_program_accounts(
                    RAYDIUM_AMM_PROGRAM_ID,
                    encoding="base64",
                    filters=filter_set
                )
                
                if accounts.value:
                    for account_info in accounts.value:
                        pool_id = str(account_info.pubkey)
                        pool_keys = self.fetch_complete_pool_keys(pool_id)
                        if pool_keys:
                            # Verify it's a SOL pair
                            if pool_keys["baseMint"] == str(WSOL_MINT) or pool_keys["quoteMint"] == str(WSOL_MINT):
                                logging.info(f"[Raydium] Found pool {pool_id[:8]}... for {token_mint[:8]}...")
                                return pool_keys
            
            return None
            
        except Exception as e:
            logging.error(f"[Raydium] Pool search error: {e}")
            return None
    
    def build_swap_instruction(
        self,
        pool_keys: Dict[str, Any],
        user_source_token: Pubkey,
        user_dest_token: Pubkey,
        user_owner: Pubkey,
        amount_in: int,
        min_amount_out: int
    ) -> Instruction:
        """Build the swap instruction with correct account ordering"""
        
        # Swap instruction discriminator for swapBaseIn is 9
        discriminator = struct.pack("<B", 9)
        amount_in_bytes = struct.pack("<Q", amount_in)
        min_out_bytes = struct.pack("<Q", min_amount_out)
        
        instruction_data = discriminator + amount_in_bytes + min_out_bytes
        
        # CRITICAL: The exact account order for Raydium V4 swapBaseIn
        accounts = [
            AccountMeta(pubkey=TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(pubkey=Pubkey.from_string(pool_keys["id"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=Pubkey.from_string(pool_keys["authority"]), is_signer=False, is_writable=False),
            AccountMeta(pubkey=Pubkey.from_string(pool_keys["openOrders"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=Pubkey.from_string(pool_keys["targetOrders"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=Pubkey.from_string(pool_keys["baseVault"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=Pubkey.from_string(pool_keys["quoteVault"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=Pubkey.from_string(pool_keys["marketProgramId"]), is_signer=False, is_writable=False),
            AccountMeta(pubkey=Pubkey.from_string(pool_keys["marketId"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=Pubkey.from_string(pool_keys["marketBids"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=Pubkey.from_string(pool_keys["marketAsks"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=Pubkey.from_string(pool_keys["marketEventQueue"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=Pubkey.from_string(pool_keys["marketBaseVault"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=Pubkey.from_string(pool_keys["marketQuoteVault"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=Pubkey.from_string(pool_keys["marketAuthority"]), is_signer=False, is_writable=False),
            AccountMeta(pubkey=user_source_token, is_signer=False, is_writable=True),
            AccountMeta(pubkey=user_dest_token, is_signer=False, is_writable=True),
            AccountMeta(pubkey=user_owner, is_signer=True, is_writable=False),
        ]
        
        return Instruction(
            program_id=RAYDIUM_AMM_PROGRAM_ID,
            accounts=accounts,
            data=instruction_data
        )
    
    def create_wrap_sol_instructions(
        self,
        owner: Pubkey,
        amount: int
    ) -> Tuple[Pubkey, list]:
        """Create instructions to wrap SOL into WSOL"""
        # Get WSOL ATA
        wsol_account = get_associated_token_address(owner, WSOL_MINT)
        
        instructions = []
        
        # Create WSOL account if needed
        instructions.append(
            create_associated_token_account(
                payer=owner,
                owner=owner,
                mint=WSOL_MINT
            )
        )
        
        # Transfer SOL to WSOL account
        from solders.system_program import transfer, TransferParams
        instructions.append(
            transfer(
                TransferParams(
                    from_pubkey=owner,
                    to_pubkey=wsol_account,
                    lamports=amount
                )
            )
        )
        
        # Sync native instruction to wrap the SOL
        sync_native_data = bytes([17])  # syncNative instruction
        instructions.append(
            Instruction(
                program_id=TOKEN_PROGRAM_ID,
                accounts=[
                    AccountMeta(pubkey=wsol_account, is_signer=False, is_writable=True)
                ],
                data=sync_native_data
            )
        )
        
        return wsol_account, instructions
    
    def build_swap_transaction(
        self,
        keypair: Keypair,
        input_mint: str,
        output_mint: str,
        amount_in: int,
        slippage: float = 0.01,
        compute_units: int = 400000,
        priority_fee: int = 50000
    ) -> Optional[VersionedTransaction]:
        """Build a complete swap transaction that actually works"""
        try:
            owner = keypair.pubkey()
            sol_mint_str = str(WSOL_MINT)
            
            # Determine which is the token
            if input_mint == sol_mint_str:
                token_mint = output_mint
                is_buy = True
            elif output_mint == sol_mint_str:
                token_mint = input_mint
                is_buy = False
            else:
                logging.error("[Raydium] Neither mint is SOL")
                return None
            
            # Find pool
            pool_keys = self.find_pool_by_mints(token_mint)
            if not pool_keys:
                logging.error(f"[Raydium] No pool found for token {token_mint[:8]}...")
                return None
            
            instructions = []
            
            # Add compute budget instructions
            instructions.append(set_compute_unit_limit(compute_units))
            instructions.append(set_compute_unit_price(priority_fee))
            
            # Prepare token accounts
            if is_buy:
                # Buying token with SOL
                # We need to wrap SOL first
                wsol_account, wrap_instructions = self.create_wrap_sol_instructions(owner, amount_in)
                instructions.extend(wrap_instructions)
                
                # Create token ATA if needed
                token_ata = get_associated_token_address(owner, Pubkey.from_string(output_mint))
                
                # Check if token ATA exists, create if not
                token_account_info = self.client.get_account_info(token_ata)
                if token_account_info.value is None:
                    instructions.append(
                        create_associated_token_account(
                            payer=owner,
                            owner=owner,
                            mint=Pubkey.from_string(output_mint)
                        )
                    )
                
                # Determine source and destination based on pool orientation
                if pool_keys["baseMint"] == sol_mint_str:
                    # SOL is base, token is quote
                    user_source_token = wsol_account
                    user_dest_token = token_ata
                else:
                    # Token is base, SOL is quote
                    user_source_token = wsol_account
                    user_dest_token = token_ata
            else:
                # Selling token for SOL
                token_ata = get_associated_token_address(owner, Pubkey.from_string(input_mint))
                wsol_account = get_associated_token_address(owner, WSOL_MINT)
                
                # Create WSOL account if needed
                wsol_account_info = self.client.get_account_info(wsol_account)
                if wsol_account_info.value is None:
                    instructions.append(
                        create_associated_token_account(
                            payer=owner,
                            owner=owner,
                            mint=WSOL_MINT
                        )
                    )
                
                # Determine source and destination
                if pool_keys["baseMint"] == token_mint:
                    # Token is base, SOL is quote
                    user_source_token = token_ata
                    user_dest_token = wsol_account
                else:
                    # SOL is base, token is quote
                    user_source_token = token_ata
                    user_dest_token = wsol_account
            
            # Calculate minimum output with slippage
            min_amount_out = int(amount_in * (1 - slippage))
            
            # Build swap instruction
            swap_ix = self.build_swap_instruction(
                pool_keys=pool_keys,
                user_source_token=user_source_token,
                user_dest_token=user_dest_token,
                user_owner=owner,
                amount_in=amount_in,
                min_amount_out=min_amount_out
            )
            instructions.append(swap_ix)
            
            # If selling for SOL, add unwrap instruction
            if not is_buy:
                # Close WSOL account to unwrap back to SOL
                from spl.token.instructions import close_account, CloseAccountParams
                instructions.append(
                    close_account(
                        CloseAccountParams(
                            account=wsol_account,
                            dest=owner,
                            owner=owner,
                            program_id=TOKEN_PROGRAM_ID
                        )
                    )
                )
            
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
            
            logging.info(f"[Raydium] Transaction built successfully:")
            logging.info(f"  Pool: {pool_keys['id'][:8]}...")
            logging.info(f"  Direction: {'BUY' if is_buy else 'SELL'}")
            logging.info(f"  Amount In: {amount_in}")
            logging.info(f"  Min Out: {min_amount_out}")
            
            return tx
            
        except Exception as e:
            logging.error(f"[Raydium] Failed to build transaction: {e}")
            import traceback
            logging.error(traceback.format_exc())
            return None
    
    def send_transaction(self, tx: VersionedTransaction) -> Optional[str]:
        """Send transaction with retry logic"""
        max_retries = 3
        
        for attempt in range(max_retries):
            try:
                result = self.client.send_transaction(
                    tx,
                    opts=TxOpts(
                        skip_preflight=True,
                        preflight_commitment=Confirmed,
                        max_retries=3
                    )
                )
                
                if result.value:
                    sig = str(result.value)
                    logging.info(f"[Raydium] Transaction sent: {sig}")
                    
                    # Wait for confirmation
                    confirmed = self.client.confirm_transaction(
                        sig,
                        commitment=Confirmed
                    )
                    
                    if confirmed.value:
                        logging.info(f"[Raydium] Transaction confirmed: {sig}")
                        return sig
                    else:
                        logging.warning(f"[Raydium] Transaction not confirmed: {sig}")
                        
            except Exception as e:
                logging.error(f"[Raydium] Send attempt {attempt + 1} failed: {e}")
                if attempt < max_retries - 1:
                    time.sleep(2)
                    
        return None

# Example usage in your buy_token function:
def buy_token_fixed(keypair: Keypair, token_mint: str, amount_lamports: int, rpc_url: str) -> Optional[str]:
    """Fixed buy token function"""
    try:
        # Initialize the fixed swap builder
        swap_builder = FixedRaydiumSwapBuilder(rpc_url)
        
        # Build the transaction
        tx = swap_builder.build_swap_transaction(
            keypair=keypair,
            input_mint=str(WSOL_MINT),  # Buying with SOL
            output_mint=token_mint,
            amount_in=amount_lamports,
            slippage=0.01,  # 1% slippage
            compute_units=400000,
            priority_fee=50000
        )
        
        if not tx:
            logging.error("Failed to build swap transaction")
            return None
        
        # Send the transaction
        signature = swap_builder.send_transaction(tx)
        
        if signature:
            logging.info(f"✅ Token purchase successful: https://solscan.io/tx/{signature}")
            return signature
        else:
            logging.error("❌ Token purchase failed")
            return None
            
    except Exception as e:
        logging.error(f"Buy token error: {e}")
        import traceback
        logging.error(traceback.format_exc())
        return None
