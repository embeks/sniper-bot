"""
PumpFun Scanner - Direct polling approach for catching launches
This replaces the WebSocket approach with active scanning
"""

import asyncio
import logging
import time
from typing import Dict, Set, Optional
from datetime import datetime
from solana.rpc.api import Client
from solders.pubkey import Pubkey
import base64

from config import RPC_ENDPOINT, PUMPFUN_PROGRAM_ID

logger = logging.getLogger(__name__)

class PumpFunScanner:
    """Actively scan for PumpFun launches"""
    
    def __init__(self, callback):
        """Initialize scanner"""
        self.callback = callback
        self.client = Client(RPC_ENDPOINT.replace('wss://', 'https://').replace('ws://', 'http://'))
        self.seen_signatures = set()
        self.seen_mints = set()
        self.running = False
        self.launches_found = 0
        
    async def start(self):
        """Start scanning"""
        self.running = True
        logger.info("🔍 Starting PumpFun scanner...")
        
        while self.running:
            try:
                # Get recent PumpFun transactions
                signatures = self.client.get_signatures_for_address(
                    PUMPFUN_PROGRAM_ID,
                    limit=50  # Check last 50 transactions
                )
                
                if signatures.value:
                    for sig_info in signatures.value:
                        sig = sig_info.signature
                        
                        # Skip if already processed
                        if sig in self.seen_signatures:
                            continue
                        
                        self.seen_signatures.add(sig)
                        
                        # Get full transaction
                        try:
                            tx = self.client.get_transaction(
                                sig,
                                max_supported_transaction_version=0
                            )
                            
                            if tx.value and not tx.value.transaction.meta.err:
                                # Check logs for buy instruction
                                logs = tx.value.transaction.meta.log_messages if tx.value.transaction.meta else []
                                
                                is_buy = False
                                for log in logs:
                                    if 'Instruction: Buy' in log or 'Instruction: buy' in log:
                                        is_buy = True
                                        break
                                
                                if is_buy:
                                    # Extract mint from accounts
                                    mint = self._extract_mint(tx.value)
                                    
                                    if mint and mint not in self.seen_mints:
                                        self.seen_mints.add(mint)
                                        
                                        # Check if this is a new token (bonding curve exists)
                                        if await self._check_if_new_token(mint):
                                            self.launches_found += 1
                                            
                                            logger.info("=" * 60)
                                            logger.info(f"🚀 NEW PUMPFUN TOKEN FOUND!")
                                            logger.info(f"📜 Mint: {mint}")
                                            logger.info(f"📝 Signature: {sig[:40]}...")
                                            logger.info(f"📊 Total found: {self.launches_found}")
                                            logger.info("=" * 60)
                                            
                                            # Trigger buy
                                            if self.callback:
                                                await self.callback({
                                                    'mint': mint,
                                                    'signature': sig,
                                                    'type': 'pumpfun_launch',
                                                    'timestamp': datetime.now().isoformat()
                                                })
                                
                        except Exception as e:
                            logger.debug(f"Error processing tx {sig[:8]}: {e}")
                
                # Wait before next scan
                await asyncio.sleep(1)  # Scan every second
                
            except Exception as e:
                logger.error(f"Scanner error: {e}")
                await asyncio.sleep(5)
    
    def _extract_mint(self, tx) -> Optional[str]:
        """Extract mint address from transaction"""
        try:
            # Get account keys
            account_keys = tx.transaction.transaction.message.account_keys
            
            # The mint is usually in positions 1-5 (after program ID)
            for i in range(1, min(6, len(account_keys))):
                key_str = str(account_keys[i])
                
                # Skip known programs
                if key_str not in [
                    "11111111111111111111111111111111",
                    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
                    "SysvarRent111111111111111111111111111111111",
                    "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s",
                    str(PUMPFUN_PROGRAM_ID)
                ]:
                    # This could be the mint
                    return key_str
            
            return None
            
        except Exception as e:
            logger.debug(f"Failed to extract mint: {e}")
            return None
    
    async def _check_if_new_token(self, mint: str) -> bool:
        """Check if token is new (has active bonding curve)"""
        try:
            mint_pubkey = Pubkey.from_string(mint)
            
            # Derive bonding curve PDA
            seeds = [b"bonding-curve", bytes(mint_pubkey)]
            bonding_curve, _ = Pubkey.find_program_address(seeds, PUMPFUN_PROGRAM_ID)
            
            # Check if bonding curve exists
            response = self.client.get_account_info(bonding_curve)
            
            if response.value:
                # Parse bonding curve data
                data = response.value.data
                if isinstance(data, list) and len(data) > 0:
                    if isinstance(data[0], str):
                        decoded = base64.b64decode(data[0])
                    else:
                        decoded = bytes(data)
                    
                    # Check if it has SOL (active bonding curve)
                    if len(decoded) >= 40:
                        real_sol_reserves = int.from_bytes(decoded[32:40], 'little')
                        sol_amount = real_sol_reserves / 1e9
                        
                        # New token if bonding curve has less than 10 SOL
                        if sol_amount < 10:
                            logger.info(f"Found new token with {sol_amount:.2f} SOL in curve")
                            return True
            
            return False
            
        except Exception as e:
            logger.debug(f"Error checking token {mint[:8]}: {e}")
            return False
    
    def stop(self):
        """Stop scanning"""
        self.running = False
        logger.info(f"Scanner stopped. Found {self.launches_found} launches")
