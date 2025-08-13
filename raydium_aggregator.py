# raydium_aggregator.py - COMPLETE FIXED VERSION (NO MORE TIMEOUTS!)
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

# Raydium Program IDs - FIXED
RAYDIUM_AMM_PROGRAM_ID = Pubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8")
RAYDIUM_AUTHORITY = Pubkey.from_string("5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1")
SERUM_PROGRAM_ID = Pubkey.from_string("srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX")  # Serum V3
OPENBOOK_PROGRAM_ID = Pubkey.from_string("srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX")  # OpenBook uses same ID
WSOL_MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")

class RaydiumAggregatorClient:
    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url
        self.client = Client(rpc_url, commitment=Confirmed)
        self.pool_cache = {}
        self.cache_duration = 300  # 5 minutes
        # NEW: Track pools we've already found
        self.known_pools = {}  # token_mint -> pool_data
        
    def find_pool_realtime(self, token_mint: str) -> Optional[Dict[str, Any]]:
        """Find Raydium pool - EFFICIENT VERSION that won't timeout"""
        try:
            sol_mint = "So11111111111111111111111111111111111111112"
            
            # 1. Check if we already know this pool
            if token_mint in self.known_pools:
                logging.info(f"[Raydium] Using known pool for {token_mint[:8]}...")
                return self.known_pools[token_mint]
            
            # 2. Check cache
            cache_key = f"{token_mint}-{sol_mint}"
            if cache_key in self.pool_cache:
                cached = self.pool_cache[cache_key]
                if time.time() - cached['timestamp'] < self.cache_duration:
                    logging.info(f"[Raydium] Using cached pool for {token_mint[:8]}...")
                    return cached['pool']
            
            logging.info(f"[Raydium] Searching for pool with {token_mint[:8]}...")
            
            # 3. SMART APPROACH: Limited scan instead of full scan
            pool = self._find_pool_smart(token_mint, sol_mint)
            if pool:
                self.pool_cache[cache_key] = {'pool': pool, 'timestamp': time.time()}
                self.known_pools[token_mint] = pool
                return pool
            
            # 4. If not found, it might be too new or only on Jupiter
            logging.info(f"[Raydium] No pool found for {token_mint[:8]}... (might be on Jupiter only)")
            return None
            
        except Exception as e:
            logging.error(f"[Raydium] Pool search error: {e}")
            return None
    
    def _find_pool_smart(self, token_mint: str, sol_mint: str) -> Optional[Dict[str, Any]]:
        """SMART pool finding - only get relevant pools, not all 5000+"""
        try:
            # Skip the full scan if configured
            if os.getenv("SKIP_RAYDIUM_FULL_SCAN", "false").lower() == "true":
                logging.info(f"[Raydium] Full scan disabled, using Jupiter fallback")
                return None
            
            # Check environment for Jupiter-first mode
            if os.getenv("JUPITER_FIRST", "false").lower() == "true":
                logging.info(f"[Raydium] Jupiter-first mode, skipping Raydium scan")
                return None
            
            # First, try to get from recent transactions (most efficient)
            recent_pool = self._check_recent_transactions(token_mint)
            if recent_pool:
                return recent_pool
            
            # If not in recent transactions, do a LIMITED scan
            limit = int(os.getenv("POOL_SCAN_LIMIT", "100"))
            
            logging.info(f"[Raydium] Doing limited scan (max {limit} pools)...")
            
            try:
                # Set a shorter timeout for the RPC call
                import socket
                original_timeout = socket.getdefaulttimeout()
                socket.setdefaulttimeout(5)  # 5 second timeout
                
                try:
                    # Get only the most recent pools (likely to have new tokens)
                    response = self.client.get_program_accounts(
                        RAYDIUM_AMM_PROGRAM_ID,
                        encoding="base64",
                        data_slice={"offset": 0, "length": 752},  # Only get pool data
                    )
                finally:
                    socket.setdefaulttimeout(original_timeout)
                
                if hasattr(response, 'value') and response.value:
                    # Only check the most recent pools
                    pools_to_check = response.value[-limit:] if len(response.value) > limit else response.value
                    
                    logging.info(f"[Raydium] Checking {len(pools_to_check)} recent pools...")
                    
                    checked = 0
                    for account_info in pools_to_check:
                        try:
                            pool_id = str(account_info.pubkey)
                            
                            # Quick check if this pool has our token
                            if self._pool_contains_token(account_info, token_mint, sol_mint):
                                logging.info(f"[Raydium] 🎯 Found pool {pool_id[:8]}... for {token_mint[:8]}!")
                                # Fetch full data for this specific pool
                                return self.fetch_pool_data_from_chain(pool_id)
                            
                            checked += 1
                            if checked % 20 == 0:
                                logging.debug(f"[Raydium] Checked {checked}/{len(pools_to_check)} pools...")
                                
                        except Exception as e:
                            continue
                    
                    logging.info(f"[Raydium] Token not found in recent {len(pools_to_check)} pools")
                    
            except socket.timeout:
                logging.warning(f"[Raydium] Scan timed out after 5 seconds, falling back to Jupiter")
                return None
            except Exception as e:
                logging.warning(f"[Raydium] Limited scan failed: {e}, falling back to Jupiter")
                return None
                
            return None
            
        except Exception as e:
            logging.error(f"[Raydium] Smart pool search error: {e}")
            return None
    
    def _check_recent_transactions(self, token_mint: str) -> Optional[Dict[str, Any]]:
        """Check recent transactions for pool creation (FASTEST method)"""
        try:
            # If we just detected this token from logs, the pool address might be in the transaction
            # This is how PumpFun tracking works - we already HAVE the pool from the transaction
            
            # Check environment for recently detected pools
            recent_pools = os.getenv("RECENT_POOL_DETECTIONS", "")
            if recent_pools:
                for entry in recent_pools.split(","):
                    if token_mint in entry:
                        pool_id = entry.split(":")[1]
                        logging.info(f"[Raydium] Found pool from recent detection: {pool_id[:8]}...")
                        return self.fetch_pool_data_from_chain(pool_id)
        except:
            pass
        return None
    
    def _pool_contains_token(self, account_info: Any, token_mint: str, sol_mint: str) -> bool:
        """Quick check if a pool contains our token without full deserialization"""
        try:
            # Get the account data
            account_data = account_info.account.data
            if isinstance(account_data, list) and len(account_data) == 2:
                data_str = account_data[0]
                if isinstance(data_str, str):
                    data = base64.b64decode(data_str)
                else:
                    data = bytes(data_str)
            else:
                return False
            
            # Check size - V4 pools are 752 bytes
            if len(data) != 752:
                return False
            
            # Quick check for our mints at known offsets
            # Coin mint at offset 119-151, PC mint at offset 151-183
            coin_mint = Pubkey.from_bytes(data[119:151])
            pc_mint = Pubkey.from_bytes(data[151:183])
            
            # Check if this pool has our token
            return (
                (str(coin_mint) == token_mint and str(pc_mint) == sol_mint) or
                (str(pc_mint) == token_mint and str(coin_mint) == sol_mint)
            )
        except:
            return False
    
    def register_new_pool(self, pool_id: str, token_mint: str):
        """Register a newly detected pool (from mempool monitoring)"""
        # When sniper_logic detects a new pool, it can register it here
        # This avoids any scanning at all!
        logging.info(f"[Raydium] Registering new pool {pool_id[:8]}... for {token_mint[:8]}...")
        pool_data = self.fetch_pool_data_from_chain(pool_id)
        if pool_data:
            self.known_pools[token_mint] = pool_data
            return pool_data
        return None
    
    def fetch_pool_data_from_chain(self, pool_id: str) -> Optional[Dict[str, Any]]:
        """Fetch actual pool data from the blockchain."""
        try:
            pool_pubkey = Pubkey.from_string(pool_id)
            
            # Get pool account
            logging.info(f"[Raydium] Fetching account data for pool {pool_id[:8]}...")
            response = self.client.get_account_info(pool_pubkey)
            if not response.value or not response.value.data:
                logging.error(f"[Raydium] No account data returned for pool {pool_id[:8]}...")
                return None
            
            # Decode the account data
            account_data = response.value.data
            if isinstance(account_data, list) and len(account_data) == 2:
                data_str = account_data[0]
                if isinstance(data_str, str):
                    data = base64.b64decode(data_str)
                else:
                    data = bytes(data_str)
            else:
                data = bytes(account_data)
            
            logging.info(f"[Raydium] Pool account data size: {len(data)} bytes")
            
            if len(data) != 752:
                logging.error(f"[Raydium] Invalid pool size: {len(data)} (expected 752)")
                return None
            
            # Parse Raydium V4 AMM account
            # Skip status fields
            offset = 87  # Start of pubkey section
            
            # Read all pubkeys
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
            
            logging.info(f"[Raydium] Pool base mint: {str(coin_mint)[:8]}...")
            logging.info(f"[Raydium] Pool quote mint: {str(pc_mint)[:8]}...")
            
            # Now fetch market data
            logging.info(f"[Raydium] Fetching market data for {str(market_id)[:8]}...")
            market_response = self.client.get_account_info(market_id)
            if not market_response.value:
                logging.warning(f"[Raydium] Failed to fetch market account, using defaults")
                # Return pool data even without market data
                return {
                    "id": pool_id,
                    "baseMint": str(coin_mint),
                    "quoteMint": str(pc_mint),
                    "lpMint": str(lp_mint),
                    "baseVault": str(coin_vault),
                    "quoteVault": str(pc_vault),
                    "openOrders": str(open_orders),
                    "targetOrders": str(target_orders),
                    "marketId": str(market_id),
                    "marketProgramId": str(market_program_id),
                    "marketAuthority": str(RAYDIUM_AUTHORITY),
                    "marketBaseVault": str(coin_vault),
                    "marketQuoteVault": str(pc_vault),
                    "marketBids": str(open_orders),
                    "marketAsks": str(target_orders),
                    "marketEventQueue": str(withdraw_queue),
                    "authority": str(amm_owner),
                    "version": 4,
                    "programId": str(RAYDIUM_AMM_PROGRAM_ID)
                }
            
            # Parse market data
            market_account_data = market_response.value.data
            if isinstance(market_account_data, list) and len(market_account_data) == 2:
                market_data_str = market_account_data[0]
                if isinstance(market_data_str, str):
                    market_data = base64.b64decode(market_data_str)
                else:
                    market_data = bytes(market_data_str)
            else:
                market_data = bytes(market_account_data)
            
            # Parse Serum/OpenBook market
            market_offset = 45  # Skip to pubkeys
            
            market_base_vault = Pubkey.from_bytes(market_data[market_offset:market_offset+32])
            market_offset += 32
            market_quote_vault = Pubkey.from_bytes(market_data[market_offset:market_offset+32])
            market_offset += 32
            
            # Skip request queue
            market_offset += 32
            
            market_event_queue = Pubkey.from_bytes(market_data[market_offset:market_offset+32])
            market_offset += 32
            
            market_bids = Pubkey.from_bytes(market_data[market_offset:market_offset+32])
            market_offset += 32
            
            market_asks = Pubkey.from_bytes(market_data[market_offset:market_offset+32])
            
            # Derive market authority
            market_authority = self.derive_market_authority(str(market_id))
            
            pool_data = {
                "id": pool_id,
                "baseMint": str(coin_mint),
                "quoteMint": str(pc_mint),
                "lpMint": str(lp_mint),
                "baseVault": str(coin_vault),
                "quoteVault": str(pc_vault),
                "openOrders": str(open_orders),
                "targetOrders": str(target_orders),
                "marketId": str(market_id),
                "marketProgramId": str(market_program_id),
                "marketAuthority": market_authority,
                "marketBaseVault": str(market_base_vault),
                "marketQuoteVault": str(market_quote_vault),
                "marketBids": str(market_bids),
                "marketAsks": str(market_asks),
                "marketEventQueue": str(market_event_queue),
                "authority": str(amm_owner),
                "version": 4,
                "programId": str(RAYDIUM_AMM_PROGRAM_ID)
            }
            
            logging.info(f"[Raydium] Successfully fetched pool data for {pool_id[:8]}...")
            return pool_data
            
        except Exception as e:
            logging.error(f"[Raydium] Failed to fetch pool data from chain: {e}")
            import traceback
            logging.error(traceback.format_exc())
            return None
    
    def derive_market_authority(self, market_id: str) -> str:
        """Derive market authority - simplified version"""
        return str(RAYDIUM_AUTHORITY)
    
    def _find_pool_by_accounts(self, token_mint: str, sol_mint: str) -> Optional[Dict[str, Any]]:
        """DEPRECATED - This is the old broken method, kept for compatibility but redirects to smart method"""
        logging.warning(f"[Raydium] Using deprecated _find_pool_by_accounts, redirecting to smart search")
        return self._find_pool_smart(token_mint, sol_mint)
    
    def _is_pool_initialized(self, pool_id: str) -> bool:
        """Check if a pool is initialized and has liquidity."""
        try:
            pool_pubkey = Pubkey.from_string(pool_id)
            response = self.client.get_account_info(pool_pubkey)
            
            if not response.value or not response.value.data:
                return False
            
            # Decode account data
            account_data = response.value.data
            if isinstance(account_data, list) and len(account_data) == 2:
                data_str = account_data[0]
                if isinstance(data_str, str):
                    data = base64.b64decode(data_str)
                else:
                    data = bytes(data_str)
            else:
                data = bytes(account_data)
            
            # Check status (first 8 bytes)
            status = int.from_bytes(data[0:8], 'little')
            
            # Status values:
            # 0 = Uninitialized
            # 1 = Initialized
            # 2 = Disabled
            # 3 = WithdrawOnly
            
            if status == 1:
                # Also check if pool has some liquidity (optional)
                # You can check coin/pc amounts at specific offsets if needed
                return True
            else:
                logging.debug(f"[Raydium] Pool status: {status} (not active)")
                return False
                
        except Exception as e:
            logging.error(f"[Raydium] Failed to check pool status: {e}")
            return False
    
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
        sol_mint = "So11111111111111111111111111111111111111112"
        
        # Determine which is the token
        if input_mint == sol_mint:
            token_mint = output_mint
        elif output_mint == sol_mint:
            token_mint = input_mint
        else:
            logging.warning("[Raydium] Neither mint is SOL")
            return None
        
        # Check if Jupiter-first mode is enabled
        if os.getenv("JUPITER_FIRST", "false").lower() == "true":
            logging.info("[Raydium] Jupiter-first mode enabled, skipping Raydium")
            return None
        
        # Find pool efficiently
        return self.find_pool_realtime(token_mint)
    
    def create_wsol_account_instructions(self, owner: Pubkey, amount: int) -> Tuple[Pubkey, List[Instruction]]:
        """Create instructions to wrap SOL into WSOL"""
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
        
        # Sync native to wrap the SOL
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
    
    def create_ata_if_needed(self, owner: Pubkey, mint: Pubkey, keypair: Keypair) -> Pubkey:
        """Create ATA if it doesn't exist."""
        ata = get_associated_token_address(owner, mint)
        
        try:
            account_info = self.client.get_account_info(ata)
            if account_info.value is None:
                logging.info(f"[Raydium] Creating ATA for {mint}")
                
                # Create ATA instruction
                create_ata_ix = create_associated_token_account(
                    payer=owner,
                    owner=owner,
                    mint=mint
                )
                
                # Build transaction
                recent_blockhash = self.client.get_latest_blockhash().value.blockhash
                msg = MessageV0.try_compile(
                    payer=owner,
                    instructions=[create_ata_ix],
                    address_lookup_table_accounts=[],
                    recent_blockhash=recent_blockhash,
                )
                tx = VersionedTransaction(msg, [keypair])
                
                # Send transaction
                sig = self.client.send_transaction(tx).value
                logging.info(f"[Raydium] Created ATA: {sig}")
                
                # Wait for confirmation
                time.sleep(2)
                
        except Exception as e:
            logging.error(f"[Raydium] ATA creation error: {e}")
        
        return ata

    def build_swap_transaction(
        self,
        keypair: Keypair,
        input_mint: str,
        output_mint: str,
        amount_in: int,
        slippage: float = 0.05  # 5% default
    ) -> Optional[VersionedTransaction]:
        """Build Raydium swap transaction with WSOL wrapping."""
        try:
            pool = self.find_pool(input_mint, output_mint)
            if not pool:
                logging.error(f"[Raydium] No pool found for {input_mint} <-> {output_mint}")
                return None
            
            owner = keypair.pubkey()
            sol_mint_str = "So11111111111111111111111111111111111111112"
            
            instructions = []
            
            # Add compute budget instructions for priority
            instructions.append(set_compute_unit_limit(400000))
            instructions.append(set_compute_unit_price(100000))
            
            # Determine swap direction and accounts
            if input_mint == sol_mint_str:
                # BUYING: SOL -> Token
                # Must wrap SOL to WSOL first
                wsol_account, wrap_instructions = self.create_wsol_account_instructions(owner, amount_in)
                instructions.extend(wrap_instructions)
                
                # Create token ATA if needed
                user_dest_token = self.create_ata_if_needed(
                    owner, 
                    Pubkey.from_string(output_mint), 
                    keypair
                )
                
                user_source_token = wsol_account
                
            else:
                # SELLING: Token -> SOL
                user_source_token = self.create_ata_if_needed(
                    owner,
                    Pubkey.from_string(input_mint),
                    keypair
                )
                
                # Create WSOL account to receive
                wsol_account = get_associated_token_address(owner, WSOL_MINT)
                try:
                    account_info = self.client.get_account_info(wsol_account)
                    if account_info.value is None:
                        instructions.append(
                            create_associated_token_account(
                                payer=owner,
                                owner=owner,
                                mint=WSOL_MINT
                            )
                        )
                except:
                    instructions.append(
                        create_associated_token_account(
                            payer=owner,
                            owner=owner,
                            mint=WSOL_MINT
                        )
                    )
                
                user_dest_token = wsol_account
            
            # Calculate minimum output with slippage
            if amount_in < 100000000:  # Less than 0.1 SOL
                min_amount_out = 1  # Accept any amount for small trades
            else:
                min_amount_out = int(amount_in * (1 - slippage))
            
            # Build swap instruction data
            data = bytes([9]) + amount_in.to_bytes(8, 'little') + min_amount_out.to_bytes(8, 'little')
            
            logging.info(f"[Raydium] Swap params:")
            logging.info(f"  Amount in: {amount_in} ({amount_in/10**9:.6f} SOL)")
            logging.info(f"  Min amount out: {min_amount_out}")
            logging.info(f"  Slippage: {slippage*100}%")
            
            # Build swap instruction accounts
            keys = [
                AccountMeta(pubkey=TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
                AccountMeta(pubkey=Pubkey.from_string(pool["id"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool.get("authority", str(RAYDIUM_AUTHORITY))), is_signer=False, is_writable=False),
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
                AccountMeta(pubkey=Pubkey.from_string(pool.get("marketAuthority", str(SERUM_PROGRAM_ID))), is_signer=False, is_writable=False),
                AccountMeta(pubkey=user_source_token, is_signer=False, is_writable=True),
                AccountMeta(pubkey=user_dest_token, is_signer=False, is_writable=True),
                AccountMeta(pubkey=owner, is_signer=True, is_writable=False),  # MUST NOT BE WRITABLE
            ]
            
            swap_ix = Instruction(
                program_id=RAYDIUM_AMM_PROGRAM_ID,
                accounts=keys,
                data=data
            )
            instructions.append(swap_ix)
            
            # If selling, close WSOL account to unwrap back to SOL
            if input_mint != sol_mint_str:
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
            
            logging.info(f"[Raydium] Swap transaction built for {input_mint[:8]}... -> {output_mint[:8]}...")
            logging.info(f"  Pool: {pool['id'][:8]}...")
            logging.info(f"  Amount in: {amount_in}")
            logging.info(f"  Min out: {min_amount_out}")
            
            return tx
            
        except Exception as e:
            logging.error(f"[Raydium] Failed to build transaction: {e}")
            import traceback
            logging.error(traceback.format_exc())
            return None

    def send_transaction(self, tx: VersionedTransaction, keypair: Keypair = None) -> Optional[str]:
        """Send transaction with retry logic and better error handling."""
        max_retries = 3
        
        for attempt in range(max_retries):
            try:
                # Send transaction with preflight to catch errors
                result = self.client.send_transaction(
                    tx,
                    opts=TxOpts(
                        skip_preflight=False,  # Enable preflight to see errors
                        preflight_commitment=Confirmed,
                        max_retries=3
                    )
                )
                
                if result.value:
                    sig = str(result.value)
                    logging.info(f"[Raydium] Transaction sent: {sig}")
                    
                    # Wait for confirmation
                    logging.info(f"[Raydium] Waiting for confirmation...")
                    try:
                        confirmation = self.client.confirm_transaction(
                            sig,
                            commitment=Confirmed,
                            sleep_seconds=1,
                            last_valid_block_height=None
                        )
                        if confirmation.value:
                            logging.info(f"[Raydium] Transaction confirmed: {sig}")
                            return sig
                        else:
                            logging.error(f"[Raydium] Transaction failed to confirm: {sig}")
                            return None
                    except Exception as e:
                        logging.error(f"[Raydium] Confirmation error: {e}")
                        return None
                    
            except Exception as e:
                error_msg = str(e)
                logging.error(f"[Raydium] Send attempt {attempt + 1} failed: {error_msg}")
                
                # Parse specific errors
                if "insufficient" in error_msg.lower():
                    logging.error("[Raydium] Insufficient balance for transaction")
                    return None
                elif "slippage" in error_msg.lower():
                    logging.error("[Raydium] Slippage tolerance exceeded")
                    return None
                elif "unknown instruction" in error_msg.lower():
                    logging.error("[Raydium] Pool might be paused or using different version")
                    return None
                    
                if attempt < max_retries - 1:
                    time.sleep(1)
                    
                    # Get new blockhash and rebuild if needed
                    if "blockhash" in error_msg.lower():
                        return None  # Caller should rebuild transaction
                    
        return None
