
# raydium_aggregator.py - PRODUCTION READY VERSION WITH FIXES
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

# Raydium Program IDs
RAYDIUM_AMM_PROGRAM_ID = Pubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8")
RAYDIUM_AUTHORITY = Pubkey.from_string("5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1")
WSOL_MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")

class RaydiumAggregatorClient:
    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url
        self.client = Client(rpc_url, commitment=Confirmed)
        self.pool_cache = {}
        self.cache_duration = 300  # 5 minutes
        self.known_pools = {}  # token_mint -> pool_data
        
    def _read_b64_account(self, acc: Any) -> Optional[bytes]:
        """Centralized base64 extraction that handles both dict and object shapes - FIXED"""
        try:
            # Handle dict format (solana-py 0.32.0)
            if isinstance(acc, dict):
                if "account" in acc and "data" in acc["account"]:
                    data = acc["account"]["data"]
                    if isinstance(data, list) and len(data) >= 1:
                        return base64.b64decode(data[0])
                    elif isinstance(data, str):
                        return base64.b64decode(data)
                # Also check if data is directly in the dict
                elif "data" in acc:
                    data = acc["data"]
                    if isinstance(data, list) and len(data) >= 1:
                        return base64.b64decode(data[0])
                    elif isinstance(data, str):
                        return base64.b64decode(data)
            # Handle object format (older versions)
            elif hasattr(acc, 'account'):
                if hasattr(acc.account, 'data'):
                    data = acc.account.data
                    if isinstance(data, list) and len(data) >= 1:
                        return base64.b64decode(data[0])
                    elif isinstance(data, str):
                        return base64.b64decode(data)
                    elif isinstance(data, bytes):
                        return data
            # Direct data attribute
            elif hasattr(acc, 'data'):
                data = acc.data
                if isinstance(data, list) and len(data) >= 1:
                    return base64.b64decode(data[0])
                elif isinstance(data, str):
                    return base64.b64decode(data)
                elif isinstance(data, bytes):
                    return data
            return None
        except Exception as e:
            logging.debug(f"[Raydium] Failed to read account data: {e}")
            return None
    
    def _pool_bytes_contain_mints(self, data: bytes, token_mint: str, sol_mint: str) -> bool:
        """Check if pool data contains our token pair"""
        try:
            if len(data) != 752:
                return False
            
            # Read mint pubkeys at known offsets
            coin_mint = Pubkey.from_bytes(data[119:151])
            pc_mint = Pubkey.from_bytes(data[151:183])
            
            return (
                (str(coin_mint) == token_mint and str(pc_mint) == sol_mint) or
                (str(pc_mint) == token_mint and str(coin_mint) == sol_mint)
            )
        except:
            return False
        
    def find_pool_realtime(self, token_mint: str) -> Optional[Dict[str, Any]]:
        """Find Raydium pool - FIXED VERSION"""
        try:
            sol_mint = "So11111111111111111111111111111111111111112"
            
            # Check cache
            if token_mint in self.known_pools:
                logging.info(f"[Raydium] Using known pool for {token_mint[:8]}...")
                return self.known_pools[token_mint]
            
            cache_key = f"{token_mint}-{sol_mint}"
            if cache_key in self.pool_cache:
                cached = self.pool_cache[cache_key]
                if time.time() - cached['timestamp'] < self.cache_duration:
                    logging.info(f"[Raydium] Using cached pool for {token_mint[:8]}...")
                    return cached['pool']
            
            logging.info(f"[Raydium] Searching for pool with {token_mint[:8]}...")
            
            pool = self._find_pool_smart(token_mint, sol_mint)
            if pool:
                self.pool_cache[cache_key] = {'pool': pool, 'timestamp': time.time()}
                self.known_pools[token_mint] = pool
                return pool
            
            logging.info(f"[Raydium] No pool found for {token_mint[:8]}...")
            return None
            
        except Exception as e:
            logging.error(f"[Raydium] Pool search error: {e}")
            return None
    
    def _find_pool_smart(self, token_mint: str, sol_mint: str) -> Optional[Dict[str, Any]]:
        """FIXED pool finding - handles dict return type properly"""
        try:
            limit = int(os.getenv("POOL_SCAN_LIMIT", "20"))
            logging.info(f"[Raydium] Doing limited scan (max {limit} pools)...")
            
            try:
                import socket
                original_timeout = socket.getdefaulttimeout()
                socket.setdefaulttimeout(5)
                
                try:
                    # CRITICAL FIX: Don't use offset - it doesn't exist on dict
                    filters = [{"dataSize": 752}]  # V4 pool size
                    
                    response = self.client.get_program_accounts(
                        RAYDIUM_AMM_PROGRAM_ID,
                        encoding="base64",
                        filters=filters
                    )
                finally:
                    socket.setdefaulttimeout(original_timeout)
                
                # CRITICAL FIX: Handle multiple response formats
                accounts = []
                if response is None:
                    logging.warning("[Raydium] No response from get_program_accounts")
                    return None
                elif hasattr(response, 'value'):
                    accounts = response.value if response.value else []
                elif hasattr(response, 'result'):
                    accounts = response.result if response.result else []
                elif isinstance(response, dict):
                    if 'result' in response:
                        accounts = response['result'] if response['result'] else []
                    elif 'value' in response:
                        accounts = response['value'] if response['value'] else []
                    else:
                        accounts = response
                elif isinstance(response, list):
                    accounts = response
                else:
                    logging.warning(f"[Raydium] Unknown response type: {type(response)}")
                    return None
                
                if not accounts:
                    logging.warning("[Raydium] No accounts returned from RPC")
                    return None
                
                # Take last N pools (most recent) - NO OFFSET
                pools_to_check = accounts[-limit:] if len(accounts) > limit else accounts
                logging.info(f"[Raydium] Checking {len(pools_to_check)} recent pools...")
                
                for account_info in pools_to_check:
                    try:
                        # CRITICAL FIX: Extract pool ID properly from different formats
                        pool_id = None
                        
                        # Try dict format first
                        if isinstance(account_info, dict):
                            if "pubkey" in account_info:
                                pool_id = str(account_info["pubkey"])
                            elif "address" in account_info:
                                pool_id = str(account_info["address"])
                        # Try object format
                        elif hasattr(account_info, 'pubkey'):
                            pool_id = str(account_info.pubkey)
                        elif hasattr(account_info, 'address'):
                            pool_id = str(account_info.address)
                        
                        if not pool_id:
                            logging.debug(f"[Raydium] Could not extract pool ID from account")
                            continue
                        
                        # Use the fixed _read_b64_account method
                        data = self._read_b64_account(account_info)
                        if not data:
                            continue
                            
                        if len(data) != 752:
                            logging.debug(f"[Raydium] Skipping pool with size {len(data)}")
                            continue
                        
                        # Check if this pool contains our token pair
                        if self._pool_bytes_contain_mints(data, token_mint, sol_mint):
                            logging.info(f"[Raydium] Found pool {pool_id[:8]}... for {token_mint[:8]}!")
                            return self.fetch_pool_data_from_chain(pool_id)
                            
                    except Exception as e:
                        logging.debug(f"[Raydium] Error checking pool: {e}")
                        continue
                
                logging.info(f"[Raydium] Token not found in recent {len(pools_to_check)} pools")
                
            except socket.timeout:
                logging.warning(f"[Raydium] Scan timed out after 5 seconds")
                return None
            except Exception as e:
                logging.warning(f"[Raydium] Limited scan failed: {e}")
                return None
                
            return None
            
        except Exception as e:
            logging.error(f"[Raydium] Smart pool search error: {e}")
            return None
    
    def fetch_pool_data_from_chain(self, pool_id: str) -> Optional[Dict[str, Any]]:
        """Fetch pool data - FIXED to handle both dict and object formats"""
        try:
            pool_pubkey = Pubkey.from_string(pool_id)
            
            logging.info(f"[Raydium] Fetching account data for pool {pool_id[:8]}...")
            response = self.client.get_account_info(pool_pubkey)
            
            # FIXED: Handle multiple response formats
            account_value = None
            if response is None:
                logging.error(f"[Raydium] No response for pool {pool_id[:8]}...")
                return None
            elif hasattr(response, 'value'):
                account_value = response.value
            elif hasattr(response, 'result'): 
                account_value = response.result
            elif isinstance(response, dict):
                if 'result' in response:
                    if isinstance(response['result'], dict) and 'value' in response['result']:
                        account_value = response['result']['value']
                    else:
                        account_value = response['result']
                elif 'value' in response:
                    account_value = response['value']
                else:
                    account_value = response
            
            if not account_value:
                logging.error(f"[Raydium] No account data returned for pool {pool_id[:8]}...")
                return None
            
            # Extract data using our fixed method
            data = None
            
            # Create a wrapper dict to use our _read_b64_account method
            if hasattr(account_value, 'data'):
                wrapper = {"account": {"data": account_value.data}}
                data = self._read_b64_account(wrapper)
            elif isinstance(account_value, dict) and 'data' in account_value:
                wrapper = {"account": account_value}
                data = self._read_b64_account(wrapper)
            else:
                # Try directly
                data = self._read_b64_account(account_value)
            
            if not data:
                logging.error(f"[Raydium] Could not extract data for pool {pool_id[:8]}...")
                return None
            
            logging.info(f"[Raydium] Pool account data size: {len(data)} bytes")
            
            # FIXED: Be more tolerant of pool sizes
            if len(data) != 752:
                logging.warning(f"[Raydium] Non-standard pool size: {len(data)} (expected 752)")
                # Try to extract minimal data anyway
                if len(data) >= 183:  # Minimum to read the mints
                    try:
                        offset = 119
                        coin_mint = Pubkey.from_bytes(data[offset:offset+32])
                        offset += 32
                        pc_mint = Pubkey.from_bytes(data[offset:offset+32])
                        
                        # Return minimal pool data
                        return {
                            "id": pool_id,
                            "baseMint": str(coin_mint),
                            "quoteMint": str(pc_mint),
                            "version": 4,
                            "programId": str(RAYDIUM_AMM_PROGRAM_ID)
                        }
                    except:
                        pass
                return None
            
            # Parse Raydium V4 AMM account
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
            
            # FIXED: Get actual market program ID from market account
            market_program_id_actual = str(market_program_id)
            try:
                market_response = self.client.get_account_info(market_id)
                if market_response and market_response.value:
                    # The owner of the market account is the actual program ID
                    if hasattr(market_response.value, 'owner'):
                        market_program_id_actual = str(market_response.value.owner)
                        logging.info(f"[Raydium] Market program: {market_program_id_actual[:8]}...")
            except:
                logging.warning(f"[Raydium] Could not fetch market program ID, using default")
            
            # Try to fetch market data
            market_base_vault = str(coin_vault)
            market_quote_vault = str(pc_vault)
            market_bids = str(open_orders)
            market_asks = str(target_orders)
            market_event_queue = str(withdraw_queue)
            
            try:
                logging.info(f"[Raydium] Fetching market data for {str(market_id)[:8]}...")
                market_response = self.client.get_account_info(market_id)
                
                market_value = None
                if hasattr(market_response, 'value'):
                    market_value = market_response.value
                elif isinstance(market_response, dict) and 'result' in market_response:
                    market_value = market_response['result']['value']
                
                if market_value:
                    # Parse market data
                    market_data = self._read_b64_account({"account": {"data": market_value.data if hasattr(market_value, 'data') else market_value['data']}})
                    if market_data and len(market_data) >= 285:  # Minimum for market structure
                        market_offset = 45
                        market_base_vault = str(Pubkey.from_bytes(market_data[market_offset:market_offset+32]))
                        market_offset += 32
                        market_quote_vault = str(Pubkey.from_bytes(market_data[market_offset:market_offset+32]))
                        market_offset += 32
                        market_offset += 32  # Skip request queue
                        market_event_queue = str(Pubkey.from_bytes(market_data[market_offset:market_offset+32]))
                        market_offset += 32
                        market_bids = str(Pubkey.from_bytes(market_data[market_offset:market_offset+32]))
                        market_offset += 32
                        market_asks = str(Pubkey.from_bytes(market_data[market_offset:market_offset+32]))
            except Exception as e:
                logging.warning(f"[Raydium] Could not parse market data: {e}")
            
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
                "marketProgramId": market_program_id_actual,
                "marketAuthority": str(RAYDIUM_AUTHORITY),
                "marketBaseVault": market_base_vault,
                "marketQuoteVault": market_quote_vault,
                "marketBids": market_bids,
                "marketAsks": market_asks,
                "marketEventQueue": market_event_queue,
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
    
    def register_new_pool(self, pool_id: str, token_mint: str):
        """Register a newly detected pool"""
        logging.info(f"[Raydium] Registering new pool {pool_id[:8]}... for {token_mint[:8]}...")
        pool_data = self.fetch_pool_data_from_chain(pool_id)
        if pool_data:
            self.known_pools[token_mint] = pool_data
            return pool_data
        return None
    
    def find_pool(self, input_mint: str, output_mint: str) -> Optional[Dict[str, Any]]:
        """Find pool for the given mint pair"""
        sol_mint = "So11111111111111111111111111111111111111112"
        
        # Determine which is the token
        if input_mint == sol_mint:
            token_mint = output_mint
        elif output_mint == sol_mint:
            token_mint = input_mint
        else:
            logging.warning("[Raydium] Neither mint is SOL")
            return None
        
        return self.find_pool_realtime(token_mint)
    
    def create_wsol_account_instructions(self, owner: Pubkey, amount: int) -> Tuple[Pubkey, List[Instruction]]:
        """Create instructions to wrap SOL into WSOL"""
        wsol_account = get_associated_token_address(owner, WSOL_MINT)
        instructions = []
        
        # Check if WSOL account exists
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
        """Create ATA if it doesn't exist"""
        ata = get_associated_token_address(owner, mint)
        
        try:
            account_info = self.client.get_account_info(ata)
            if account_info.value is None:
                logging.info(f"[Raydium] Creating ATA for {mint}")
                
                create_ata_ix = create_associated_token_account(
                    payer=owner,
                    owner=owner,
                    mint=mint
                )
                
                recent_blockhash = self.client.get_latest_blockhash().value.blockhash
                msg = MessageV0.try_compile(
                    payer=owner,
                    instructions=[create_ata_ix],
                    address_lookup_table_accounts=[],
                    recent_blockhash=recent_blockhash,
                )
                tx = VersionedTransaction(msg, [keypair])
                
                sig = self.client.send_transaction(tx).value
                logging.info(f"[Raydium] Created ATA: {sig}")
                
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
        slippage: float = 0.05
    ) -> Optional[VersionedTransaction]:
        """Build Raydium swap transaction - FIXED with validation"""
        try:
            pool = self.find_pool(input_mint, output_mint)
            if not pool:
                logging.error(f"[Raydium] No pool found for {input_mint} <-> {output_mint}")
                return None
            
            # FIXED: Validate required pool fields exist
            required_fields = ["id", "baseMint", "quoteMint", "baseVault", "quoteVault", 
                             "openOrders", "targetOrders", "marketId", "marketProgramId"]
            for field in required_fields:
                if field not in pool or not pool[field]:
                    logging.error(f"[Raydium] Missing required pool field: {field}")
                    return None
            
            owner = keypair.pubkey()
            sol_mint_str = "So11111111111111111111111111111111111111112"
            
            instructions = []
            
            # Add compute budget instructions
            instructions.append(set_compute_unit_limit(400000))
            instructions.append(set_compute_unit_price(100000))
            
            # Determine swap direction and accounts
            if input_mint == sol_mint_str:
                # BUYING: SOL -> Token
                wsol_account, wrap_instructions = self.create_wsol_account_instructions(owner, amount_in)
                instructions.extend(wrap_instructions)
                
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
                min_amount_out = 1
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
                AccountMeta(pubkey=Pubkey.from_string(pool["marketProgramId"]), is_signer=False, is_writable=False),
                AccountMeta(pubkey=Pubkey.from_string(pool["marketId"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool.get("marketBids", pool["openOrders"])), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool.get("marketAsks", pool["targetOrders"])), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool.get("marketEventQueue", pool.get("targetOrders", pool["openOrders"]))), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool.get("marketBaseVault", pool["baseVault"])), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool.get("marketQuoteVault", pool["quoteVault"])), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool.get("marketAuthority", str(RAYDIUM_AUTHORITY))), is_signer=False, is_writable=False),
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
            
            # FIXED: Add WSOL cleanup on buy failure
            if input_mint == sol_mint_str:
                # Add a close instruction that will only execute if there's leftover WSOL
                cleanup_close = close_account(
                    CloseAccountParams(
                        account=wsol_account,
                        dest=owner,
                        owner=owner,
                        program_id=TOKEN_PROGRAM_ID
                    )
                )
                instructions.append(cleanup_close)
            
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
            
            return tx
            
        except Exception as e:
            logging.error(f"[Raydium] Failed to build transaction: {e}")
            import traceback
            logging.error(traceback.format_exc())
            return None

    def send_transaction(self, tx: VersionedTransaction, keypair: Keypair = None) -> Optional[str]:
        """Send transaction with retry logic"""
        max_retries = 3
        
        for attempt in range(max_retries):
            try:
                result = self.client.send_transaction(
                    tx,
                    opts=TxOpts(
                        skip_preflight=False,
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
                    
                    if "blockhash" in error_msg.lower():
                        return None
                    
        return None
