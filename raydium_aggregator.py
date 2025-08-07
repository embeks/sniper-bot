# raydium_aggregator.py - FIXED VERSION (KEEPING YOUR ORIGINAL POOL LOGIC + WSOL FIX)
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
        
    def find_pool_realtime(self, token_mint: str) -> Optional[Dict[str, Any]]:
        """Find Raydium pool using multiple methods - FETCH REAL DATA."""
        try:
            sol_mint = "So11111111111111111111111111111111111111112"
            
            # Check cache first
            cache_key = f"{token_mint}-{sol_mint}"
            if cache_key in self.pool_cache:
                cached = self.pool_cache[cache_key]
                if time.time() - cached['timestamp'] < self.cache_duration:
                    logging.info(f"[Raydium] Using cached pool for {token_mint[:8]}...")
                    return cached['pool']
            
            logging.info(f"[Raydium] Searching for pool with {token_mint[:8]}...")
            
            # Known pool IDs - use correct, active pools
            known_pool_ids = {
                "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R": "AVs9TA4nWDzfPJE9gGVNJMVhcQy3V9PGazuz33BfG2RA",  # RAY
                "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2",  # USDC V2
                "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263": "Ew1pSB7JDT5HJe1NKza9Qa8nBksH2SDEsH3w4uRUAnJP",  # BONK
                "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm": "2QdhepnKRTLjjSqPL1PtKNwqrUkoLee5Gqs8bvZhRdMv",  # WIF
            }
            
            # For known tokens, use hardcoded working configurations
            working_pools = {
                # USDC-SOL (FINAL CORRECT CONFIG)
                "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": {
                    "id": "58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2",
                    "baseMint": "So11111111111111111111111111111111111111112",
                    "quoteMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                    "baseVault": "DQyrAcCrDXQ7NeoqGgDCZwBvWDcYmFCjSb9JtteuvPpz",  # SOL vault (coin)
                    "quoteVault": "HLmqeL62xR1QoZ1HKKbXRrdN1p3phKpxRMb2VVopvBBz",  # USDC vault (pc) - CORRECTED
                    "openOrders": "6w5hF2hceQRZbaxjPJutiWSPAFWDkp3YbY2Aq3RpCSKe",
                    "targetOrders": "8VuvrSWfQP8vdbuMAP9AkfgLxU9hbRR6BmTJ8Gfas9aK",
                    "marketId": "9wFFyRfZBsuAha4YcuxcXLKwMxJR43S7fPfQLusDBzvT",
                    "marketProgramId": "srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX",
                    "marketAuthority": "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
                    "marketBaseVault": "CZza3Ej4Mc58MnxWA385itCC9jCo3L1D7zc3LKy1bZMR",
                    "marketQuoteVault": "9vYWHBPz817wJdQpE8u3h8UoY3sZ16ZXdCcvLB7jY4Dj",
                    "marketBids": "14ivtgssEBoBjuZJtSAPKYgpUK7DmnSwuPMqJoVTSgKJ",
                    "marketAsks": "CEQdAFKdycHugujQg9k2wbmxjcpdYZyVLfV9WerTnafJ",
                    "marketEventQueue": "5KKsLVU6TcbVDK4BS6K1DGDxnh4Q9xjYJ8XaDCG5t8ht",
                    "authority": "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
                    "version": 4,
                    "programId": str(RAYDIUM_AMM_PROGRAM_ID)
                },
                # BONK-SOL (CORRECTED)
                "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263": {
                    "id": "DSUvc5qf5LJHHV5e2tD184ixotSnCnwj7i4jJa4Xsrmt",
                    "baseMint": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
                    "quoteMint": "So11111111111111111111111111111111111111112",
                    "baseVault": "FBba2XsQVhkoQDMfbNLVmo7dsvssdT39BMzVc2eFfE21",  # CORRECTED
                    "quoteVault": "BBqvdVM9B9BB9vr2tBvP6ySx9u49uH5xDNfiBLJiUqjM",
                    "openOrders": "9SfTaCQeBwnvKpMTpFZ3w7vCqDBxCYH3FbiCw1kB1NXh",
                    "targetOrders": "BLgyHcBFBJLcgX3DvCkPYmtFj6CJVVzxaXBTbABiQHNa",
                    "marketId": "Hs97TCZeuYiJxooo3U73qEHXg3dKpRL4uYKYRryEK9CF",
                    "marketProgramId": "srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX",
                    "marketAuthority": "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
                    "marketBaseVault": "AVnL1McPFDDmtEmLKffoUkfB8fPjCmKYJe5kQp52GATv",
                    "marketQuoteVault": "EkanLFwgmsrT2HcQhLsJtJLnzYi2pDNzquCnNhz8MF7w",
                    "marketBids": "GxS3nyEUtF9LaCNpLRgR8pFhMFUwEJbB2vE8wWZBJ6YR",
                    "marketAsks": "EWLJVLjJAX3xZrer3S5hkLMPpmGPdbELNEMLfWa8pz6C",
                    "marketEventQueue": "48be8v5vMKRgLmfPUUnTjUkvAMPXvEjVbVqk4bUPDDWM",
                    "authority": "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
                    "version": 4,
                    "programId": str(RAYDIUM_AMM_PROGRAM_ID)
                }
            }
            
            if token_mint in working_pools:
                pool = working_pools[token_mint]
                self.pool_cache[cache_key] = {'pool': pool, 'timestamp': time.time()}
                logging.info(f"[Raydium] Using working pool config for {token_mint[:8]}...")
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
    
    def _find_pool_by_accounts(self, token_mint: str, sol_mint: str) -> Optional[Dict[str, Any]]:
        """Find pool by searching program accounts - DYNAMIC DISCOVERY."""
        try:
            logging.info(f"[Raydium] Searching for pools with token {token_mint[:8]}...")
            
            # Since we're having issues with filters, let's use known pool IDs for popular tokens
            # and fetch their data dynamically
            known_pool_ids = {
                # Remove JUP for now since the pool ID was wrong
                "WENWENvqqNya429ubCdR81ZmD69brwQaaBYY6p3LCpk": "7RVTPyhj3bSK7b5rtPx6r9aqvKZDKnEVe8wJbWjfJrGf",
                "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263": "DSUvc5qf5LJHHV5e2tD184ixotSnCnwj7i4jJa4Xsrmt",  # BONK
                "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So": "2hBVyoYnbGkdMPbGPKM7E2p5wmACtLwZwAWnj5ejfQqy",  # mSOL
            }
            
            if token_mint in known_pool_ids:
                pool_id = known_pool_ids[token_mint]
                logging.info(f"[Raydium] Using known pool ID {pool_id[:8]}... for {token_mint[:8]}...")
                pool = self.fetch_pool_data_from_chain(pool_id)
                if pool:
                    logging.info(f"[Raydium] Successfully fetched pool data for {token_mint[:8]}!")
                    return pool
                else:
                    logging.warning(f"[Raydium] Failed to fetch data for known pool {pool_id[:8]}...")
            
            # For unknown tokens, try to get all pools
            logging.info(f"[Raydium] Token not in known list, attempting full scan...")
            
            try:
                # Get all program accounts
                logging.info(f"[Raydium] Fetching all Raydium pools (this may take 10-30 seconds)...")
                accounts = self.client.get_program_accounts(
                    RAYDIUM_AMM_PROGRAM_ID,
                    encoding="base64"
                )
                
                if hasattr(accounts, 'value') and accounts.value:
                    total_pools = len(accounts.value)
                    logging.info(f"[Raydium] Found {total_pools} total Raydium pools, scanning for {token_mint[:8]}...")
                    
                    # Check ALL pools - no limit
                    checked = 0
                    v4_pools = 0
                    
                    for i, account_info in enumerate(accounts.value):
                        try:
                            pool_id = str(account_info.pubkey)
                            
                            # Get the account data
                            account_data = account_info.account.data
                            if isinstance(account_data, list) and len(account_data) == 2:
                                data_str = account_data[0]
                                if isinstance(data_str, str):
                                    data = base64.b64decode(data_str)
                                else:
                                    data = bytes(data_str)
                            else:
                                continue
                            
                            # Check size - V4 pools are 752 bytes
                            if len(data) != 752:
                                continue
                            
                            v4_pools += 1
                            
                            # Check if this pool contains our token
                            # Coin mint at offset 119-151, PC mint at offset 151-183
                            coin_mint = Pubkey.from_bytes(data[119:151])
                            pc_mint = Pubkey.from_bytes(data[151:183])
                            
                            checked += 1
                            
                            # Log progress every 100 pools
                            if checked % 100 == 0:
                                logging.info(f"[Raydium] Checked {checked}/{v4_pools} V4 pools...")
                            
                            # Check if this pool has our token and SOL
                            is_match = False
                            if (str(coin_mint) == token_mint and str(pc_mint) == sol_mint):
                                is_match = True
                                logging.info(f"[Raydium] 🎯 Found pool {pool_id[:8]}... with {token_mint[:8]}... as base!")
                            elif (str(pc_mint) == token_mint and str(coin_mint) == sol_mint):
                                is_match = True
                                logging.info(f"[Raydium] 🎯 Found pool {pool_id[:8]}... with {token_mint[:8]}... as quote!")
                            
                            if is_match:
                                # Fetch complete pool data
                                logging.info(f"[Raydium] Fetching complete pool data...")
                                pool = self.fetch_pool_data_from_chain(pool_id)
                                if pool:
                                    logging.info(f"[Raydium] ✅ Successfully found and fetched pool for {token_mint[:8]}!")
                                    return pool
                                else:
                                    logging.warning(f"[Raydium] Found pool but failed to fetch data")
                                    
                        except Exception as e:
                            continue
                    
                    logging.info(f"[Raydium] Scanned all {checked} V4 pools, none matched {token_mint[:8]}")
                else:
                    logging.error(f"[Raydium] Failed to get program accounts")
                    
            except Exception as e:
                logging.error(f"[Raydium] Error scanning pools: {e}")
            
            return None
            
        except Exception as e:
            logging.error(f"[Raydium] Pool search error: {e}")
            import traceback
            logging.error(traceback.format_exc())
            return None
    
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
        
        # Find pool in real-time
        return self.find_pool_realtime(token_mint)
    
    def create_wsol_account_instructions(self, owner: Pubkey, amount: int) -> Tuple[Pubkey, List[Instruction]]:
        """Create instructions to wrap SOL into WSOL - THIS IS THE CRITICAL FIX"""
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
        
        # Sync native to wrap the SOL - CRITICAL INSTRUCTION
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
        """Create ATA if it doesn't exist - YOUR ORIGINAL METHOD."""
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
        slippage: float = 0.05  # Increased to 5% default
    ) -> Optional[VersionedTransaction]:
        """Build Raydium swap transaction - FIXED WITH WSOL WRAPPING."""
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
                # CRITICAL FIX: Must wrap SOL to WSOL first!
                wsol_account, wrap_instructions = self.create_wsol_account_instructions(owner, amount_in)
                instructions.extend(wrap_instructions)
                
                # Create token ATA if needed
                user_dest_token = self.create_ata_if_needed(
                    owner, 
                    Pubkey.from_string(output_mint), 
                    keypair
                )
                
                # For SOL -> Token swaps:
                # If pool has SOL as quote (RAY-SOL pool), we're swapping quote to base
                # Source is WSOL (quote), destination is Token (base)
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
            # For very small amounts, use even more permissive slippage
            if amount_in < 100000000:  # Less than 0.1 SOL
                min_amount_out = 1  # Accept any amount for small trades
            else:
                min_amount_out = int(amount_in * (1 - slippage))
            
            # Build swap instruction data
            # Try using swap discriminator 9 for swapBaseIn
            data = bytes([9]) + amount_in.to_bytes(8, 'little') + min_amount_out.to_bytes(8, 'little')
            
            logging.info(f"[Raydium] Swap params:")
            logging.info(f"  Amount in: {amount_in} ({amount_in/10**9:.6f} SOL)")
            logging.info(f"  Min amount out: {min_amount_out}")
            logging.info(f"  Slippage: {slippage*100}%")
            
            # Build swap instruction accounts - EXACT ORDER MATTERS!
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
                # Send transaction with preflight to get better errors
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
                    return sig
                    
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
