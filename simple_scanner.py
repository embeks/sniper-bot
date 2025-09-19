"""
Simple Scanner - Direct approach with fallback options
"""

import asyncio
import logging
import os
from datetime import datetime
from solana.rpc.api import Client
from solders.pubkey import Pubkey

logger = logging.getLogger(__name__)

# PumpFun program ID
PUMPFUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwkvq"

class SimpleScanner:
    def __init__(self, callback):
        self.callback = callback
        self.seen = set()
        self.running = False
        
        # Try multiple RPC endpoints
        helius_key = os.getenv('HELIUS_API') or os.getenv('HELIUS_API_KEY')
        
        endpoints = []
        
        # Add Helius if we have a key
        if helius_key:
            endpoints.append(f"https://mainnet.helius-rpc.com/?api-key={helius_key}")
        
        # Add RPC_URL from env if exists
        if os.getenv('RPC_URL'):
            url = os.getenv('RPC_URL')
            if url.startswith('wss://'):
                url = url.replace('wss://', 'https://')
            endpoints.append(url)
        
        # Add public endpoints as fallback
        endpoints.extend([
            "https://api.mainnet-beta.solana.com",
            "https://solana-api.projectserum.com"
        ])
        
        # Try each endpoint until one works
        self.client = None
        for endpoint in endpoints:
            try:
                logger.info(f"Trying RPC: {endpoint[:40]}...")
                client = Client(endpoint)
                # Test it
                version = client.get_version()
                logger.info(f"✅ Connected to RPC: {endpoint[:40]}...")
                self.client = client
                break
            except Exception as e:
                logger.warning(f"Failed: {e}")
                continue
        
        if not self.client:
            logger.error("❌ No working RPC endpoint found!")
            raise Exception("Cannot connect to Solana RPC")
    
    async def start(self):
        """Start scanning"""
        self.running = True
        logger.info("🔍 Starting simple scanner...")
        scan_count = 0
        
        while self.running:
            try:
                scan_count += 1
                if scan_count % 10 == 1:  # Log every 10 scans
                    logger.info(f"📊 Scan #{scan_count} - Checking PumpFun transactions...")
                
                # Get PumpFun transactions
                program_id = Pubkey.from_string(PUMPFUN_PROGRAM)
                sigs = self.client.get_signatures_for_address(program_id, limit=10)
                
                if sigs and sigs.value:
                    logger.info(f"Found {len(sigs.value)} PumpFun transactions")
                    
                    for sig_info in sigs.value:
                        sig = sig_info.signature
                        
                        if sig in self.seen:
                            continue
                        
                        self.seen.add(sig)
                        
                        # Log EVERY new transaction we see
                        logger.info(f"📍 New PumpFun TX: {sig[:30]}...")
                        
                        # Every 10th transaction, trigger a test buy
                        if len(self.seen) % 10 == 0:
                            logger.info("=" * 60)
                            logger.info("🚀 TEST: Triggering callback on 10th transaction")
                            logger.info(f"📜 Using signature as test mint: {sig[:44]}")
                            logger.info("=" * 60)
                            
                            if self.callback:
                                await self.callback({
                                    'mint': sig[:44],  # Use sig as fake mint for testing
                                    'signature': sig,
                                    'type': 'test_trigger',
                                    'timestamp': datetime.now().isoformat()
                                })
                else:
                    if scan_count % 10 == 1:
                        logger.info("No transactions returned from RPC")
                
                await asyncio.sleep(3)  # Check every 3 seconds
                
            except Exception as e:
                logger.error(f"Scan error: {e}")
                import traceback
                logger.error(traceback.format_exc())
                await asyncio.sleep(5)
    
    def stop(self):
        self.running = False
        logger.info("Scanner stopped")
