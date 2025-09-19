"""
PumpFun Log Monitor - Based on your working old repo approach
This uses transaction log pattern matching like your old bot did
"""

import asyncio
import logging
import os
from datetime import datetime
from solana.rpc.api import Client
from solders.pubkey import Pubkey
from solana.rpc.commitment import Confirmed

logger = logging.getLogger(__name__)

PUMPFUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwkvq"

# Log patterns from your old repo that actually worked
PUMPFUN_CREATE_PATTERNS = [
    "Program log: Instruction: Create",
    "Program log: Instruction: Initialize", 
    "Program log: Instruction: InitializeBondingCurve",
    "create_mint",
    "InitializeMint",
    "create",
]

class PumpFunLogMonitor:
    def __init__(self, callback):
        self.callback = callback
        self.seen_sigs = set()
        self.running = False
        
        # Setup RPC like your old repo
        helius_key = os.getenv('HELIUS_API') or os.getenv('HELIUS_API_KEY')
        if helius_key:
            self.rpc_url = f"https://mainnet.helius-rpc.com/?api-key={helius_key}"
        else:
            self.rpc_url = "https://api.mainnet-beta.solana.com"
        
        self.client = Client(self.rpc_url, commitment=Confirmed)
        logger.info(f"PumpFun monitor using: {self.rpc_url[:50]}...")
    
    async def start(self):
        """Start monitoring PumpFun logs"""
        self.running = True
        logger.info("🔍 Starting PumpFun log monitor (like old repo)...")
        
        last_signatures = []
        
        while self.running:
            try:
                # Get recent PumpFun signatures (like your old mempool_listener)
                program_pubkey = Pubkey.from_string(PUMPFUN_PROGRAM)
                
                # Get signatures with before/after for pagination
                sigs_response = self.client.get_signatures_for_address(
                    program_pubkey,
                    limit=25,
                    commitment=Confirmed
                )
                
                if sigs_response and hasattr(sigs_response, 'value'):
                    signatures = sigs_response.value
                    
                    # Process new signatures
                    new_count = 0
                    for sig_data in signatures:
                        # Get signature string
                        if hasattr(sig_data, 'signature'):
                            sig = str(sig_data.signature)
                        else:
                            continue
                        
                        if sig in self.seen_sigs:
                            continue
                        
                        self.seen_sigs.add(sig)
                        new_count += 1
                        
                        # Get full transaction with logs
                        try:
                            tx_response = self.client.get_transaction(
                                sig,
                                max_supported_transaction_version=0
                            )
                            
                            if tx_response and tx_response.value:
                                tx = tx_response.value
                                
                                # Check logs like your old repo
                                if hasattr(tx.transaction, 'meta') and hasattr(tx.transaction.meta, 'log_messages'):
                                    logs = tx.transaction.meta.log_messages
                                    
                                    # Check for creation patterns
                                    is_creation = False
                                    for log in logs:
                                        for pattern in PUMPFUN_CREATE_PATTERNS:
                                            if pattern in log:
                                                is_creation = True
                                                logger.info(f"Found pattern '{pattern}' in transaction {sig[:20]}...")
                                                break
                                        if is_creation:
                                            break
                                    
                                    if is_creation:
                                        # Extract mint from accounts
                                        mint = self._extract_mint_from_tx(tx)
                                        
                                        if mint:
                                            logger.info("=" * 60)
                                            logger.info(f"🚀 PUMPFUN TOKEN LAUNCH DETECTED!")
                                            logger.info(f"📜 Mint: {mint}")
                                            logger.info(f"📝 Signature: {sig[:40]}...")
                                            logger.info("=" * 60)
                                            
                                            if self.callback:
                                                await self.callback({
                                                    'mint': mint,
                                                    'signature': sig,
                                                    'type': 'pumpfun_launch',
                                                    'timestamp': datetime.now().isoformat()
                                                })
                        
                        except Exception as e:
                            logger.debug(f"Error fetching tx {sig[:20]}: {e}")
                    
                    if new_count > 0:
                        logger.info(f"Processed {new_count} new PumpFun transactions")
                
                # Rate limit friendly
                await asyncio.sleep(2)
                
            except Exception as e:
                logger.error(f"Monitor error: {e}")
                await asyncio.sleep(5)
    
    def _extract_mint_from_tx(self, tx) -> str:
        """Extract mint address from transaction (like your old code)"""
        try:
            # Get account keys
            if hasattr(tx.transaction, 'transaction'):
                message = tx.transaction.transaction.message
                if hasattr(message, 'account_keys'):
                    accounts = message.account_keys
                    
                    # Mint is usually in first few accounts after program
                    for i in range(1, min(10, len(accounts))):
                        account = str(accounts[i])
                        
                        # Skip known system programs
                        if account not in [
                            "11111111111111111111111111111111",
                            "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                            "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
                            "SysvarRent111111111111111111111111111111111",
                            "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s",
                            PUMPFUN_PROGRAM,
                            "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwkvq",
                            "CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM",  # PumpFun fee account
                        ] and len(account) == 44:  # Valid pubkey length
                            # This could be the mint
                            return account
            
            return None
            
        except Exception as e:
            logger.debug(f"Failed to extract mint: {e}")
            return None
    
    def stop(self):
        self.running = False
        logger.info("PumpFun monitor stopped")
