# raydium_aggregator.py - FIXED VERSION WITH WSOL WRAPPING
import os
import json
import logging
import httpx
import struct
from typing import Optional, Dict, Any, Tuple, List
import base64
import base58
import time

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import ID as SYS_PROGRAM_ID, transfer, TransferParams
from solders.instruction import Instruction, AccountMeta
from solders.transaction import VersionedTransaction
from solders.message import MessageV0
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from spl.token.constants import TOKEN_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID
from spl.token.instructions import get_associated_token_address, create_associated_token_account, close_account, CloseAccountParams

# CORRECTED Raydium Program IDs
RAYDIUM_AMM_PROGRAM_ID = Pubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8")
RAYDIUM_AUTHORITY = Pubkey.from_string("5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1")
SERUM_PROGRAM_ID = Pubkey.from_string("srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX")  # FIXED: Correct Serum V3
WSOL_MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")

class RaydiumAggregatorClient:
    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url
        self.client = Client(rpc_url, commitment=Confirmed)
        self.pool_cache = {}
        self.cache_duration = 300  # 5 minutes
        
    def derive_market_authority(self, market_id: str) -> str:
        """Derive the market authority PDA for Serum market"""
        try:
            market_pubkey = Pubkey.from_string(market_id)
            seeds = [bytes(market_pubkey)]
            
            for nonce in range(0, 100):
                try:
                    seeds_with_nonce = seeds + [bytes([nonce])]
                    pda, bump = Pubkey.find_program_address(seeds_with_nonce, SERUM_PROGRAM_ID)
                    return str(pda)
                except:
                    continue
                    
            return str(RAYDIUM_AUTHORITY)
        except:
            return str(RAYDIUM_AUTHORITY)
    
    def fetch_complete_pool_keys(self, pool_id: str) -> Optional[Dict[str, Any]]:
        """Fetch and decode complete pool keys from on-chain data"""
        try:
            pool_pubkey = Pubkey.from_string(pool_id)
            
            response = self.client.get_account_info(pool_pubkey)
            if not response.value or not response.value.data:
                return None
            
            data = base64.b64decode(response.value.data[0])
            
            # Parse Raydium V4 AMM account structure
            offset = 87  # Start of pubkey section
            
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
            
            # Fetch Serum market data
            market_response = self.client.get_account_info(market_id)
            if not market_response.value:
                logging.error(f"Failed to fetch market account: {market_id}")
                return None
            
            market_data = base64.b64decode(market_response.value.data[0])
            
            # Parse Serum V3 market
            market_offset = 45  # Skip to pubkeys section
            
            base_vault = Pubkey.from_bytes(market_data[market_offset:market_offset+32])
            market_offset += 32
            quote_vault = Pubkey.from_bytes(market_data[market_offset:market_offset+32])
            market_offset += 32
            
            # Skip request queue
            market_offset += 32
            
            event_queue = Pubkey.from_bytes(market_data[market_offset:market_offset+32])
            market_offset += 32
            
            bids = Pubkey.from_bytes(market_data[market_offset:market_offset+32])
            market_offset += 32
            
            asks = Pubkey.from_bytes(market_data[market_offset:market_offset+32])
            
            # Derive market authority
            market_authority = self.derive_market_authority(str(market_id))
            
            return {
                "id": pool_id,
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
                "marketAuthority": market_authority,
                "marketBaseVault": str(base_vault),
                "marketQuoteVault": str(quote_vault),
                "marketBids": str(bids),
                "marketAsks": str(asks),
                "marketEventQueue": str(event_queue),
                "authority": str(amm_owner),
                "version": 4,
                "programId": str(RAYDIUM_AMM_PROGRAM_ID)
            }
            
        except Exception as e:
            logging.error(f"Failed to fetch complete pool keys: {e}")
            return None
        
    def find_pool_realtime(self, token_mint: str) -> Optional[Dict[str, Any]]:
        """Find Raydium pool using multiple methods."""
        try:
            sol_mint = str(WSOL_MINT)
            
            # Check cache first
            cache_key = f"{token_mint}-{sol_mint}"
            if cache_key in self.pool_cache:
                cached = self.pool_cache[cache_key]
                if time.time() - cached['timestamp'] < self.cache_duration:
                    logging.info(f"[Raydium] Using cached pool for {token_mint[:8]}...")
                    return cached['pool']
            
            logging.info(f"[Raydium] Searching for pool with {token_mint[:8]}...")
            
            # Known pool IDs - fetch complete data
            known_pool_ids = {
                "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R": "AVs9TA4nWDzfPJE9gGVNJMVhcQy3V9PGazuz33BfG2RA",  # RAY
                "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "6a1CsrpeZubDjEJE9s1CMVheB6HWM5d7m1cj2jkhyXhj",  # USDC
            }
            
            if token_mint in known_pool_ids:
                pool_id = known_pool_ids[token_mint]
                pool = self.fetch_complete_pool_keys(pool_id)
                if pool:
                    self.pool_cache[cache_key] = {'pool': pool, 'timestamp': time.time()}
                    logging.info(f"[Raydium] Found known pool for {token_mint[:8]}...")
                    return pool
            
            # Search for pool by program accounts
            pool = self._find_pool_by_accounts(token_mint, sol_mint)
            if pool:
                self.pool_cache[cache_key] = {'pool': pool, 'timestamp': time.time()}
                return pool
            
            logging.warning(f"[Raydium] No pool found for {token_mint[:8]}...")
            return None
            
        except Exception as e:
            logging.error(f"[Raydium] Pool search error: {e}")
            import traceback
            logging.error(traceback.format_exc())
            return None
    
    def _find_pool_by_accounts(self, token_mint: str, sol_mint: str) -> Optional[Dict[str, Any]]:
        """Find pool by searching program accounts."""
        try:
            filters = [
                {"dataSize": 752},  # Raydium V4 AMM size
                {
                    "memcmp": {
                        "offset": 119,  # Coin mint offset
                        "bytes": base58.b58encode(bytes(Pubkey.from_string(token_mint))).decode()
                    }
                }
            ]
            
            filters_alt = [
                {"dataSize": 752},
                {
                    "memcmp": {
                        "offset": 151,  # PC mint offset  
                        "bytes": base58.b58encode(bytes(Pubkey.from_string(token_mint))).decode()
                    }
                }
            ]
            
            for filter_set in [filters, filters_alt]:
                accounts = self.client.get_program_accounts(
                    RAYDIUM_AMM_PROGRAM_ID,
                    encoding="base64",
                    filters=filter_set
                )
                
                if accounts.value:
                    for account_info in accounts.value:
                        pool_id = str(account_info.pubkey)
                        pool = self.fetch_complete_pool_keys(pool_id)
                        if pool:
                            # Verify it's a SOL pair
                            if pool["baseMint"] == sol_mint or pool["quoteMint"] == sol_mint:
                                logging.info(f"[Raydium] Found pool {pool_id[:8]}...")
                                return pool
            
            return None
            
        except Exception as e:
            logging.debug(f"Account search failed: {e}")
            return None
    
    def _check_token_exists(self, token_mint: str) -> bool:
        """Check if token exists and is tradeable using Jupiter Price API."""
        try:
            url = f"https://price.jup.ag/v4/price?ids={token_mint}"
            with httpx.Client(timeout=5) as client:
                resp = client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    return token_mint in data.get("data", {})
        except:
            pass
        return False
    
    def find_pool(self, input_mint: str, output_mint: str) -> Optional[Dict[str, Any]]:
        """Find pool for the given mint pair."""
        sol_mint = str(WSOL_MINT)
        
        # Determine which is the token
        if input_mint == sol_mint:
            token_mint = output_mint
        elif output_mint == sol_mint:
            token_mint = input_mint
        else:
            logging.warning("[Raydium] Neither mint is SOL")
            return None
        
        # Find pool in real-time
        return self.find_pool_realtime(token_mint)
    
    def create_wsol_account_instructions(self, owner: Pubkey, amount: int) -> Tuple[Pubkey, List[Instruction]]:
        """Create instructions to wrap SOL into WSOL - CRITICAL FIX"""
        wsol_account = get_associated_token_address(owner, WSOL_MINT)
        instructions = []
        
        # Check if WSOL account exists
        try:
            account_info = self.client.get_account_info(wsol_account)
            if account_info.value is None:
                # Create WSOL ATA
                instructions.append(
                    create_associated_token_account(
                        payer=owner,
                        owner=owner,
                        mint=WSOL_MINT
                    )
                )
        except:
            # Create WSOL ATA
            instructions.append(
                create_associated_token_account(
                    payer=owner,
                    owner=owner,
                    mint=WSOL_MINT
                )
            )
        
        # Transfer SOL to WSOL account
        instructions.append(
            transfer(
                TransferParams(
                    from_pubkey=owner,
                    to_pubkey=wsol_account,
                    lamports=amount
                )
            )
        )
        
        # Sync native to wrap the SOL - CRITICAL
        sync_native_data = bytes([17])  # syncNative instruction discriminator
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
    
    def create_ata_if_needed(self, owner: Pubkey, mint: Pubkey, keypair: Keypair) -> Tuple[Pubkey, Optional[Instruction]]:
        """Create ATA if it doesn't exist, return ATA and instruction if created."""
        ata = get_associated_token_address(owner, mint)
        
        try:
            account_info = self.client.get_account_info(ata)
            if account_info.value is None:
                logging.info(f"[Raydium] Need to create ATA for {mint}")
                create_ix = create_associated_token_account(
                    payer=owner,
                    owner=owner,
                    mint=mint
                )
                return ata, create_ix
        except Exception as e:
            logging.error(f"[Raydium] ATA check error: {e}")
            create_ix = create_associated_token_account(
                payer=owner,
                owner=owner,
                mint=mint
            )
            return ata, create_ix
        
        return ata, None

    def build_swap_transaction(
        self,
        keypair: Keypair,
        input_mint: str,
        output_mint: str,
        amount_in: int,
        slippage: float = 0.01
    ) -> Optional[VersionedTransaction]:
        """Build Raydium swap transaction - FIXED VERSION WITH WSOL WRAPPING."""
        try:
            pool = self.find_pool(input_mint, output_mint)
            if not pool:
                logging.error(f"[Raydium] No pool found for {input_mint} <-> {output_mint}")
                return None
            
            owner = keypair.pubkey()
            sol_mint_str = str(WSOL_MINT)
            
            instructions = []
            
            # Add compute budget instructions for priority
            instructions.append(set_compute_unit_limit(400000))
            instructions.append(set_compute_unit_price(100000))  # Increased priority
            
            # Determine swap direction and prepare accounts
            if input_mint == sol_mint_str:
                # BUYING: SOL -> Token (CRITICAL FIX: WRAP SOL FIRST)
                is_buy = True
                
                # Wrap SOL into WSOL - THIS IS THE CRITICAL FIX
                wsol_account, wrap_instructions = self.create_wsol_account_instructions(owner, amount_in)
                instructions.extend(wrap_instructions)
                
                # Create token ATA if needed
                token_mint_pubkey = Pubkey.from_string(output_mint)
                token_ata, create_token_ix = self.create_ata_if_needed(owner, token_mint_pubkey, keypair)
                if create_token_ix:
                    instructions.append(create_token_ix)
                
                # Set source and destination based on pool orientation
                if pool["baseMint"] == sol_mint_str:
                    user_source_token = wsol_account  # Use WSOL, not native SOL
                    user_dest_token = token_ata
                else:
                    user_source_token = wsol_account
                    user_dest_token = token_ata
            else:
                # SELLING: Token -> SOL
                is_buy = False
                
                # Get token ATA
                token_mint_pubkey = Pubkey.from_string(input_mint)
                token_ata = get_associated_token_address(owner, token_mint_pubkey)
                
                # Create WSOL account for receiving
                wsol_account, create_wsol_ix = self.create_ata_if_needed(owner, WSOL_MINT, keypair)
                if create_wsol_ix:
                    instructions.append(create_wsol_ix)
                
                # Set source and destination
                if pool["baseMint"] == input_mint:
                    user_source_token = token_ata
                    user_dest_token = wsol_account
                else:
                    user_source_token = token_ata
                    user_dest_token = wsol_account
            
            # Calculate minimum output with slippage
            min_amount_out = int(amount_in * (1 - slippage))
            
            # Build swap instruction data
            discriminator = struct.pack("<B", 9)  # swapBaseIn
            amount_in_bytes = struct.pack("<Q", amount_in)
            min_out_bytes = struct.pack("<Q", min_amount_out)
            swap_data = discriminator + amount_in_bytes + min_out_bytes
            
            # Build swap instruction with CORRECT account order
            swap_accounts = [
                AccountMeta(pubkey=TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
                AccountMeta(pubkey=Pubkey.from_string(pool["id"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool["authority"]), is_signer=False, is_writable=False),
                AccountMeta(pubkey=Pubkey.from_string(pool["openOrders"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool["targetOrders"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool["baseVault"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool["quoteVault"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool.get("marketProgramId", str(SERUM_PROGRAM_ID))), is_signer=False, is_writable=False),
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
                accounts=swap_accounts,
                data=swap_data
            )
            instructions.append(swap_ix)
            
            # If selling, unwrap WSOL back to SOL
            if not is_buy:
                close_ix = close_account(
                    CloseAccountParams(
                        account=wsol_account,
                        dest=owner,
                        owner=owner,
                        program_id=TOKEN_PROGRAM_ID
                    )
                )
                instructions.append(close_ix)
            
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
            
            logging.info(f"[Raydium] Swap transaction built:")
            logging.info(f"  Direction: {'BUY' if is_buy else 'SELL'}")
            logging.info(f"  Pool: {pool['id'][:8]}...")
            logging.info(f"  Amount: {amount_in / 10**9:.4f} SOL" if input_mint == sol_mint_str else f"  Amount: {amount_in}")
            logging.info(f"  Min output: {min_amount_out}")
            
            return tx
            
        except Exception as e:
            logging.error(f"[Raydium] Failed to build transaction: {e}")
            import traceback
            logging.error(traceback.format_exc())
            return None

    def send_transaction(self, tx: VersionedTransaction, keypair: Keypair = None) -> Optional[str]:
        """Send transaction with retry logic."""
        max_retries = 3
        
        for attempt in range(max_retries):
            try:
                # Send transaction
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
                    try:
                        confirmed = self.client.confirm_transaction(sig, commitment=Confirmed)
                        if confirmed:
                            logging.info(f"[Raydium] Transaction confirmed!")
                    except:
                        pass  # Still return sig even if confirmation times out
                    
                    return sig
                    
            except Exception as e:
                logging.error(f"[Raydium] Send attempt {attempt + 1} failed: {e}")
                if attempt < max_retries - 1:
                    time.sleep(1)
                    
                    # Get new blockhash and rebuild if needed
                    if "blockhash" in str(e).lower():
                        return None  # Caller should rebuild transaction
                    
        return None
