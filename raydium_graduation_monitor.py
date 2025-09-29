"""
Raydium Graduation Monitor - Detects PumpFun tokens graduating to Raydium
"""

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Callable, Dict, Optional, Set
from solders.pubkey import Pubkey
from solana.rpc.api import Client

logger = logging.getLogger(__name__)

class RadiumGraduationMonitor:
    """Monitor for PumpFun graduations to Raydium"""
    
    def __init__(self, callback: Callable):
        self.callback = callback
        self.running = False
        self.seen_graduations: Set[str] = set()
        self.recent_pools = {}  # Track pools created in last 5 minutes
        
        # Import config here to avoid circular imports
        from config import RPC_ENDPOINT
        self.client = Client(RPC_ENDPOINT)
        
        # Program IDs
        self.RAYDIUM_PROGRAM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
        self.PUMPFUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
        
        # Statistics
        self.graduations_detected = 0
        self.graduations_processed = 0
        self.last_check_time = time.time()
        
    async def start(self):
        """Start monitoring for graduations using polling approach"""
        self.running = True
        logger.info("🎓 Starting Raydium Graduation Monitor")
        logger.info(f"📊 Watching for PumpFun → Raydium migrations")
        logger.info(f"💰 Target: $69k market cap graduations")
        logger.info("⚡ Using transaction polling for reliability")
        
        while self.running:
            try:
                # Poll for recent transactions
                await self._check_recent_transactions()
                
                # Clean old entries from recent_pools
                current_time = time.time()
                self.recent_pools = {
                    k: v for k, v in self.recent_pools.items() 
                    if current_time - v['timestamp'] < 300  # Keep 5 minutes
                }
                
                # Wait before next check
                await asyncio.sleep(2)  # Check every 2 seconds
                
            except Exception as e:
                logger.error(f"Monitor error: {e}")
                await asyncio.sleep(5)
    
    async def _check_recent_transactions(self):
        """Check recent transactions for graduations"""
        try:
            # Get recent signatures for Raydium program
            response = self.client.get_signatures_for_address(
                Pubkey.from_string(self.RAYDIUM_PROGRAM),
                limit=20
            )
            
            if not response.value:
                return
            
            for sig_info in response.value:
                signature = sig_info.signature
                
                # Skip if already processed
                if signature in self.seen_graduations:
                    continue
                
                # Get transaction details
                tx_response = self.client.get_transaction(
                    signature,
                    encoding="json",
                    max_supported_transaction_version=0
                )
                
                if not tx_response.value:
                    continue
                
                # Check if this is a graduation
                if self._is_graduation_transaction(tx_response.value):
                    await self._process_graduation(signature, tx_response.value)
                    
        except Exception as e:
            logger.debug(f"Transaction check error: {e}")
    
    def _is_graduation_transaction(self, tx_data: Dict) -> bool:
        """Check if transaction is a PumpFun graduation"""
        try:
            # Check if PumpFun program is involved
            if not tx_data or 'transaction' not in tx_data:
                return False
            
            # Get account keys
            account_keys = []
            message = tx_data.get('transaction', {}).get('message', {})
            
            # Handle different message formats
            if isinstance(message, dict):
                account_keys = message.get('accountKeys', [])
            
            # Check for PumpFun program
            pumpfun_involved = any(
                self.PUMPFUN_PROGRAM in str(key) if isinstance(key, str) 
                else self.PUMPFUN_PROGRAM in str(key.get('pubkey', ''))
                for key in account_keys
            )
            
            if not pumpfun_involved:
                return False
            
            # Check for pool initialization in logs
            meta = tx_data.get('meta', {})
            if meta and not meta.get('err'):
                log_messages = meta.get('logMessages', [])
                
                # Look for Raydium pool initialization
                for log in log_messages:
                    if 'initialize' in log.lower() and 'pool' in log.lower():
                        return True
                    if 'raydium' in log.lower():
                        return True
            
            return False
            
        except Exception as e:
            logger.debug(f"Error checking graduation: {e}")
            return False
    
    async def _process_graduation(self, signature: str, tx_data: Dict):
        """Process a detected graduation"""
        try:
            # Mark as seen
            self.seen_graduations.add(signature)
            self.graduations_detected += 1
            
            # Extract token mint
            mint = self._extract_mint_from_transaction(tx_data)
            if not mint:
                logger.warning(f"Could not extract mint from graduation tx {signature[:8]}...")
                return
            
            # Skip if we've seen this mint graduate before
            if mint in self.recent_pools:
                return
            
            # Track this graduation
            self.recent_pools[mint] = {
                'timestamp': time.time(),
                'signature': signature
            }
            
            logger.info("=" * 60)
            logger.info("🎓 GRADUATION DETECTED!")
            logger.info(f"Mint: {mint}")
            logger.info(f"Transaction: {signature[:16]}...")
            logger.info(f"Initial Liquidity: ~$69,000 (85 SOL)")
            logger.info("=" * 60)
            
            # Prepare callback data
            graduation_data = {
                'mint': mint,
                'name': 'Graduated Token',  # Would need metadata lookup
                'symbol': 'GRAD',
                'initial_liquidity_sol': 85,
                'initial_liquidity_usd': 69000,
                'graduation_signature': signature,
                'timestamp': datetime.now().isoformat(),
                'type': 'raydium_graduation',
                'source': 'pumpfun_migration',
                'solAmount': 0.02,  # Our buy amount
                'vSolInBondingCurve': 85,  # Graduation always at 85 SOL
                'data': {
                    'mint': mint,
                    'name': 'Graduated Token',
                    'symbol': 'GRAD',
                    'vSolInBondingCurve': 85,
                    'solAmount': 0.02
                }
            }
            
            # Trigger callback
            if self.callback:
                await self.callback(graduation_data)
                self.graduations_processed += 1
                
        except Exception as e:
            logger.error(f"Failed to process graduation: {e}")
    
    def _extract_mint_from_transaction(self, tx_data: Dict) -> Optional[str]:
        """Extract token mint from transaction"""
        try:
            # Get account keys
            message = tx_data.get('transaction', {}).get('message', {})
            account_keys = message.get('accountKeys', [])
            
            # For Raydium pool creation, token mint is typically at index 8-9
            # This is a simplified extraction - you might need to parse the actual instruction data
            if len(account_keys) > 9:
                for i in range(8, min(12, len(account_keys))):
                    key = account_keys[i]
                    if isinstance(key, dict):
                        potential_mint = key.get('pubkey')
                    else:
                        potential_mint = str(key)
                    
                    # Basic validation - not system program, not Raydium, not PumpFun
                    if (potential_mint and 
                        '11111111' not in potential_mint and
                        self.RAYDIUM_PROGRAM not in potential_mint and
                        self.PUMPFUN_PROGRAM not in potential_mint):
                        return potential_mint
            
            # Fallback: look in instruction data
            instructions = message.get('instructions', [])
            for instruction in instructions:
                if instruction.get('programId') == self.RAYDIUM_PROGRAM:
                    accounts = instruction.get('accounts', [])
                    if len(accounts) > 8:
                        return str(account_keys[accounts[8]])
            
            return None
            
        except Exception as e:
            logger.error(f"Failed to extract mint: {e}")
            return None
    
    def get_stats(self) -> Dict:
        """Get monitor statistics"""
        return {
            'graduations_detected': self.graduations_detected,
            'graduations_processed': self.graduations_processed,
            'unique_graduations': len(self.recent_pools),
            'monitoring_active': self.running
        }
    
    def stop(self):
        """Stop the monitor"""
        self.running = False
        logger.info(f"Raydium monitor stopped. Graduations detected: {self.graduations_detected}")
