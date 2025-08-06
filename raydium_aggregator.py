# raydium_aggregator.py - ELITE VERSION - Real-time pool detection
import os
import json
import logging
import httpx
import struct
from typing import Optional, Dict, Any
import base64
import time

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import ID as SYS_PROGRAM_ID
from solders.instruction import Instruction, AccountMeta
from solana.rpc.api import Client
from solana.transaction import Transaction
from spl.token.constants import TOKEN_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID
from spl.token.instructions import get_associated_token_address, create_associated_token_account

# Raydium Program IDs
RAYDIUM_AMM_PROGRAM_ID = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
RAYDIUM_AUTHORITY = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
SERUM_PROGRAM_ID = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"

# Compute Budget Program
COMPUTE_BUDGET_PROGRAM_ID = "ComputeBudget111111111111111111111111111111"

class RaydiumAggregatorClient:
    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url
        self.client = Client(rpc_url)
        self.pool_cache = {}  # Cache found pools
        self.cache_duration = 300  # 5 minutes
        
    def find_pool_realtime(self, token_mint: str) -> Optional[Dict[str, Any]]:
        """Find Raydium pool in real-time by scanning recent transactions."""
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
            
            # Get recent signatures for the token
            signatures = self.client.get_signatures_for_address(
                Pubkey.from_string(token_mint),
                limit=100
            )
            
            if not signatures.get("result"):
                logging.warning(f"[Raydium] No transactions found for {token_mint[:8]}...")
                return None
            
            # Look through recent transactions
            for sig_info in signatures["result"]:
                try:
                    # Get transaction details
                    tx = self.client.get_transaction(
                        sig_info["signature"],
                        encoding="jsonParsed"
                    )
                    
                    if not tx.get("result"):
                        continue
                    
                    # Check if this is a Raydium transaction
                    account_keys = tx["result"]["transaction"]["message"]["accountKeys"]
                    
                    # Look for Raydium program in the transaction
                    raydium_found = False
                    for key in account_keys:
                        pubkey = key if isinstance(key, str) else key.get("pubkey", "")
                        if pubkey == RAYDIUM_AMM_PROGRAM_ID:
                            raydium_found = True
                            break
                    
                    if not raydium_found:
                        continue
                    
                    # Parse transaction to find pool info
                    instructions = tx["result"]["transaction"]["message"]["instructions"]
                    
                    for ix in instructions:
                        if ix.get("programId") == RAYDIUM_AMM_PROGRAM_ID:
                            # Check if this is an initialize or swap instruction
                            if "data" in ix:
                                data = ix["data"]
                                if isinstance(data, str) and len(data) > 0:
                                    # Decode the instruction
                                    decoded = base64.b64decode(data)
                                    if decoded[0] == 0:  # Initialize instruction
                                        # This transaction created a pool
                                        accounts = ix["accounts"]
                                        if len(accounts) >= 17:
                                            pool_info = {
                                                "id": accounts[4],  # AMM ID
                                                "authority": RAYDIUM_AUTHORITY,
                                                "openOrders": accounts[6],
                                                "targetOrders": accounts[7],
                                                "baseVault": accounts[10],
                                                "quoteVault": accounts[11],
                                                "marketId": accounts[16],
                                                "baseMint": token_mint if token_mint != sol_mint else sol_mint,
                                                "quoteMint": sol_mint if token_mint != sol_mint else token_mint,
                                                "version": 4,
                                                "programId": RAYDIUM_AMM_PROGRAM_ID
                                            }
                                            
                                            # Get market info
                                            market_info = self._get_market_info_from_tx(tx, pool_info["marketId"])
                                            pool_info.update(market_info)
                                            
                                            # Cache the pool
                                            self.pool_cache[cache_key] = {
                                                'pool': pool_info,
                                                'timestamp': time.time()
                                            }
                                            
                                            logging.info(f"[Raydium] Found pool from transaction: {pool_info['id']}")
                                            return pool_info
                                    
                                    elif decoded[0] == 9:  # Swap instruction
                                        # This is a swap, we can extract pool ID
                                        accounts = ix["accounts"]
                                        if len(accounts) >= 18:
                                            pool_id = accounts[1]
                                            
                                            # Get pool details
                                            pool_account = self.client.get_account_info(
                                                Pubkey.from_string(pool_id)
                                            )
                                            
                                            if pool_account.get("result", {}).get("value"):
                                                pool_data = self._parse_pool_account(
                                                    pool_account["result"]["value"]["data"][0],
                                                    pool_id
                                                )
                                                
                                                if pool_data and token_mint in [pool_data["baseMint"], pool_data["quoteMint"]]:
                                                    # Cache the pool
                                                    self.pool_cache[cache_key] = {
                                                        'pool': pool_data,
                                                        'timestamp': time.time()
                                                    }
                                                    
                                                    logging.info(f"[Raydium] Found pool from swap: {pool_id}")
                                                    return pool_data
                    
                except Exception as e:
                    continue
            
            logging.warning(f"[Raydium] No pool found for {token_mint[:8]}... after checking {len(signatures['result'])} transactions")
            return None
            
        except Exception as e:
            logging.error(f"[Raydium] Pool search error: {e}")
            return None
    
    def _parse_pool_account(self, data: str, pool_id: str) -> Optional[Dict[str, Any]]:
        """Parse Raydium pool account data."""
        try:
            account_data = base64.b64decode(data)
            
            # Raydium V4 pool layout
            offset = 208  # Start of pubkey section
            
            base_mint = str(Pubkey(account_data[offset:offset+32]))
            quote_mint = str(Pubkey(account_data[offset+32:offset+64]))
            base_vault = str(Pubkey(account_data[offset+64:offset+96]))
            quote_vault = str(Pubkey(account_data[offset+96:offset+128]))
            open_orders = str(Pubkey(account_data[offset+128:offset+160]))
            market_id = str(Pubkey(account_data[offset+160:offset+192]))
            target_orders = str(Pubkey(account_data[offset+224:offset+256]))
            
            # Get market info
            market_info = self._get_market_info(market_id)
            
            return {
                "id": pool_id,
                "baseMint": base_mint,
                "quoteMint": quote_mint,
                "baseVault": base_vault,
                "quoteVault": quote_vault,
                "openOrders": open_orders,
                "targetOrders": target_orders,
                "marketId": market_id,
                "authority": RAYDIUM_AUTHORITY,
                "version": 4,
                "programId": RAYDIUM_AMM_PROGRAM_ID,
                **market_info
            }
            
        except Exception as e:
            logging.error(f"[Raydium] Failed to parse pool account: {e}")
            return None
    
    def _get_market_info(self, market_id: str) -> Dict[str, Any]:
        """Get Serum market info."""
        try:
            market_account = self.client.get_account_info(Pubkey.from_string(market_id))
            if market_account.get("result", {}).get("value"):
                data = base64.b64decode(market_account["result"]["value"]["data"][0])
                
                # Serum market layout
                offset = 53  # Start of pubkey section
                
                base_vault = str(Pubkey(data[offset:offset+32]))
                quote_vault = str(Pubkey(data[offset+32:offset+64]))
                
                # Skip request queue and event queue
                offset += 96
                
                bids = str(Pubkey(data[offset:offset+32]))
                asks = str(Pubkey(data[offset+32:offset+64]))
                
                offset += 64
                base_lot = int.from_bytes(data[offset:offset+8], 'little')
                quote_lot = int.from_bytes(data[offset+8:offset+16], 'little')
                
                offset += 32
                event_queue = str(Pubkey(data[offset:offset+32]))
                
                return {
                    "marketAuthority": SERUM_PROGRAM_ID,
                    "marketBaseVault": base_vault,
                    "marketQuoteVault": quote_vault,
                    "marketBids": bids,
                    "marketAsks": asks,
                    "marketEventQueue": event_queue,
                }
        except:
            # Return defaults if market info fails
            return {
                "marketAuthority": SERUM_PROGRAM_ID,
                "marketBaseVault": "11111111111111111111111111111111",
                "marketQuoteVault": "11111111111111111111111111111111",
                "marketBids": "11111111111111111111111111111111",
                "marketAsks": "11111111111111111111111111111111",
                "marketEventQueue": "11111111111111111111111111111111",
            }
    
    def _get_market_info_from_tx(self, tx: Dict, market_id: str) -> Dict[str, Any]:
        """Extract market info from transaction."""
        # For now, get it from the market account
        return self._get_market_info(market_id)
    
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
    
    def create_ata_if_needed(self, owner: Pubkey, mint: Pubkey, keypair: Keypair) -> Pubkey:
        """Create ATA if it doesn't exist."""
        ata = get_associated_token_address(owner, mint)
        
        try:
            account_info = self.client.get_account_info(ata)
            # Check if account exists
            if not account_info or account_info.get("result", {}).get("value") is None:
                logging.info(f"[Raydium] Creating ATA for {mint}")
                
                # Create ATA
                ix = create_associated_token_account(payer=owner, owner=owner, mint=mint)
                
                # Build and send transaction
                tx = Transaction()
                tx.fee_payer = owner
                tx.add(ix)
                
                # Get recent blockhash
                recent_blockhash = self.client.get_recent_blockhash()["result"]["value"]["blockhash"]
                tx.recent_blockhash = recent_blockhash
                
                # Sign and send
                tx.sign(keypair)
                sig = self.client.send_raw_transaction(tx.serialize())["result"]
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
        slippage: float = 0.01
    ) -> Optional[Transaction]:
        """Build Raydium swap transaction."""
        try:
            pool = self.find_pool(input_mint, output_mint)
            if not pool:
                logging.error(f"[Raydium] No pool found for {input_mint} <-> {output_mint}")
                return None
            
            owner = keypair.pubkey()
            
            # Determine swap direction and accounts
            if input_mint == "So11111111111111111111111111111111111111112":
                # SOL to Token
                user_source_token = owner  # Native SOL account
                user_dest_token = self.create_ata_if_needed(
                    owner, 
                    Pubkey.from_string(output_mint), 
                    keypair
                )
            else:
                # Token to SOL
                user_source_token = self.create_ata_if_needed(
                    owner,
                    Pubkey.from_string(input_mint),
                    keypair
                )
                user_dest_token = owner  # Native SOL account
            
            # Build transaction
            tx = Transaction()
            tx.fee_payer = owner
            
            # Add compute budget for priority
            compute_limit_data = struct.pack("<BI", 2, 400000)  # Higher limit for safety
            compute_limit_ix = Instruction(
                program_id=Pubkey.from_string(COMPUTE_BUDGET_PROGRAM_ID),
                accounts=[],
                data=compute_limit_data
            )
            
            compute_price_data = struct.pack("<BQ", 3, 50000)  # Higher priority for sniping
            compute_price_ix = Instruction(
                program_id=Pubkey.from_string(COMPUTE_BUDGET_PROGRAM_ID),
                accounts=[],
                data=compute_price_data
            )
            
            tx.add(compute_limit_ix)
            tx.add(compute_price_ix)
            
            # Calculate minimum output with slippage
            min_amount_out = int(amount_in * (1 - slippage))
            
            # Build swap instruction data
            # Raydium swap instruction: [discriminator(1)] + [amountIn(8)] + [minAmountOut(8)]
            data = bytes([9]) + amount_in.to_bytes(8, 'little') + min_amount_out.to_bytes(8, 'little')
            
            # Build swap instruction accounts
            keys = [
                AccountMeta(pubkey=Pubkey.from_string(TOKEN_PROGRAM_ID), is_signer=False, is_writable=False),
                AccountMeta(pubkey=Pubkey.from_string(pool["id"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool.get("authority", RAYDIUM_AUTHORITY)), is_signer=False, is_writable=False),
                AccountMeta(pubkey=Pubkey.from_string(pool["openOrders"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool["targetOrders"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool["baseVault"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool["quoteVault"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(SERUM_PROGRAM_ID), is_signer=False, is_writable=False),
                AccountMeta(pubkey=Pubkey.from_string(pool["marketId"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool["marketBids"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool["marketAsks"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool["marketEventQueue"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool["marketBaseVault"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool["marketQuoteVault"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool.get("marketAuthority", SERUM_PROGRAM_ID)), is_signer=False, is_writable=False),
                AccountMeta(pubkey=user_source_token, is_signer=False, is_writable=True),
                AccountMeta(pubkey=user_dest_token, is_signer=False, is_writable=True),
                AccountMeta(pubkey=owner, is_signer=True, is_writable=False),
            ]
            
            swap_ix = Instruction(
                program_id=Pubkey.from_string(RAYDIUM_AMM_PROGRAM_ID),
                accounts=keys,
                data=data
            )
            tx.add(swap_ix)
            
            # Get recent blockhash
            recent_blockhash = self.client.get_recent_blockhash()["result"]["value"]["blockhash"]
            tx.recent_blockhash = recent_blockhash
            
            logging.info(f"[Raydium] Swap transaction built for {input_mint[:8]}... -> {output_mint[:8]}...")
            return tx
            
        except Exception as e:
            logging.error(f"[Raydium] Failed to build transaction: {e}")
            import traceback
            logging.error(traceback.format_exc())
            return None

    def send_transaction(self, tx: Transaction, keypair: Keypair) -> Optional[str]:
        """Send transaction with retry logic."""
        max_retries = 3
        
        for attempt in range(max_retries):
            try:
                # Sign transaction
                tx.sign(keypair)
                
                # Send with high commitment
                result = self.client.send_raw_transaction(
                    tx.serialize(),
                    opts={
                        "skipPreflight": True,
                        "preflightCommitment": "processed",
                        "maxRetries": 3
                    }
                )
                
                if "result" in result:
                    sig = result["result"]
                    logging.info(f"[Raydium] Transaction sent: {sig}")
                    return sig
                else:
                    error = result.get("error", {})
                    logging.error(f"[Raydium] Transaction failed: {error}")
                    
                    # If blockhash expired, get new one and retry
                    if "blockhash" in str(error).lower():
                        recent_blockhash = self.client.get_recent_blockhash()["result"]["value"]["blockhash"]
                        tx.recent_blockhash = recent_blockhash
                        continue
                    
                    return None
                    
            except Exception as e:
                logging.error(f"[Raydium] Send attempt {attempt + 1} failed: {e}")
                if attempt < max_retries - 1:
                    time.sleep(1)
                    continue
                    
        return None
