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
        """Find Raydium pool using multiple methods - YOUR ORIGINAL LOGIC."""
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
            
            # For well-known tokens, use hardcoded pools first - YOUR ORIGINAL DATA
            known_pools = {
                # RAY-SOL
                "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R": {
                    "id": "AVs9TA4nWDzfPJE9gGVNJMVhcQy3V9PGazuz33BfG2RA",
                    "baseMint": "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
                    "quoteMint": "So11111111111111111111111111111111111111112",
                    "baseVault": "Em6rHi68trYgBFyJ5261A2nhwuQWfLcirgzZZYoRcrkX",
                    "quoteVault": "3mEFzHsJyu2Cpjrz6zPmTzP7uoLFj9SbbecGVzzkL1mJ",
                    "openOrders": "6Su6Ea97dBxecd5W92KcVvv6SzCurE2BXGgFe9LNGMpE",
                    "targetOrders": "5hATcCfvhVwAjNExvrg8rRkXmYyksHhVajWLa46iRsmE",
                    "marketId": "C6tp2RVZnxBPFbnAsfTjis8BN9tycESAT4SgDQgbbrsA",
                    "marketProgramId": "srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX",  # Correct Serum V3
                    "marketAuthority": "7SdieGqwPJo5rMmSQM9JmntSEMoimM4dQn7NkGbNFcrd",
                    "marketBaseVault": "6U6U59zmFWrPSzm9sLX7kVkaK78Kz7XJYkrhP1DjF3uF",
                    "marketQuoteVault": "4YEx21yeUAZxUL9Fs7YU9Gm3u45GWoPFs8vcJiHga2eQ",
                    "marketBids": "C1nEbACFaHMUiKAUsXVYPWZsuxunJeBkqXHPFr8QgSj9",
                    "marketAsks": "4DNBdnTw6wmrK4NmdSTTxs1kEz47yjqLGuoqsMeHvkMF",
                    "marketEventQueue": "4HGvdTqhYadgZ1YKrPfEfUvKGMGDnaPSvpEMnJ8kwGNt",
                    "authority": str(RAYDIUM_AUTHORITY),
                    "version": 4,
                    "programId": str(RAYDIUM_AMM_PROGRAM_ID)
                },
                # USDC-SOL
                "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": {
                    "id": "6a1CsrpeZubDjEJE9s1CMVheB6HWM5d7m1cj2jkhyXhj",
                    "baseMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                    "quoteMint": "So11111111111111111111111111111111111111112",
                    "baseVault": "DQyrAcCrDXQ7NeoqGgDCZwBvWDcYmFCjSb9JtteuvPpz",
                    "quoteVault": "HLmqeL62xR1QoZ1HKKbXRrdN1p3phKpxRMb2VVopvBBz",
                    "openOrders": "75HKx8M5UBdp2wPLqLZqoWfPsqJnmhFCVFUe9yPg5FMa",
                    "targetOrders": "3K5bWdYQZKYLEWi655X8bNVFXmJfnVVsu3wFYomKVYsu",
                    "marketId": "8BnEgHoWFysVcuFFX7QztDmzuH8r5ZFvyP3sYwn1XTh6",
                    "marketProgramId": "srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX",  # Correct Serum V3
                    "marketAuthority": "F8Vyqk3unwxkXukZFQeYyGmFfTG3CAX4v24iyrjEYBJV",
                    "marketBaseVault": "9vYWHBPz817wJdQpE8u3h8UoY3sZ16ZXdCcvLB7jY4Dj",
                    "marketQuoteVault": "6mJqqT5TMgveDvxzBt3hrjGkPV5VAj7tacxFCT3GebXh",
                    "marketBids": "14ivtgssEBoBjuZJtSAPKYgpUK7DmnSwuPMqJoVTSgKJ",
                    "marketAsks": "CEQdAFKdycHugujQg9k2wbmxjcpdYZyVLfV9WerTnafJ",
                    "marketEventQueue": "5KKsLVU6TcbVDK4BS6K1DGDxnh4Q9xjYJ8XaDCG5t8ht",
                    "authority": str(RAYDIUM_AUTHORITY),
                    "version": 4,
                    "programId": str(RAYDIUM_AMM_PROGRAM_ID)
                }
            }
            
            if token_mint in known_pools:
                pool = known_pools[token_mint]
                self.pool_cache[cache_key] = {'pool': pool, 'timestamp': time.time()}
                logging.info(f"[Raydium] Found known pool for {token_mint[:8]}...")
                return pool
            
            # Try other methods for unknown tokens
            pool = self._find_pool_by_accounts(token_mint, sol_mint)
            if pool:
                self.pool_cache[cache_key] = {'pool': pool, 'timestamp': time.time()}
                return pool
            
            # Check if token exists via Jupiter
            if self._check_token_exists(token_mint):
                logging.info(f"[Raydium] Token {token_mint[:8]}... exists but pool not found yet")
                return None
            
            logging.warning(f"[Raydium] No pool found for {token_mint[:8]}...")
            return None
            
        except Exception as e:
            logging.error(f"[Raydium] Pool search error: {e}")
            import traceback
            logging.error(traceback.format_exc())
            return None
    
    def _find_pool_by_accounts(self, token_mint: str, sol_mint: str) -> Optional[Dict[str, Any]]:
        """Find pool by searching program accounts - YOUR ORIGINAL."""
        try:
            # For now, skip this method as it might be causing issues
            # Will rely on known pools and transaction history
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
