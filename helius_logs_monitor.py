"""
Helius Logs Monitor - Direct PumpFun program log subscription
Detects new tokens in 0.2-0.8s vs 8-12s for PumpPortal
FIXED: Transaction-based detection instead of log string matching
"""

import asyncio
import json
import logging
import time
import websockets
from datetime import datetime
from typing import Optional, Dict
from solders.pubkey import Pubkey
from solders.signature import Signature as SoldersSignature

from config import HELIUS_API_KEY, PUMPFUN_PROGRAM_ID

logger = logging.getLogger(__name__)

class HeliusLogsMonitor:
    """Subscribe to PumpFun program logs via Helius WebSocket"""
    
    def __init__(self, callback, rpc_client):
        self.callback = callback
        self.rpc_client = rpc_client
        self.running = False
        self.seen_tokens = set()
        self.reconnect_count = 0
        
        # Verify Helius API key
        if not HELIUS_API_KEY:
            logger.error("❌ CRITICAL: HELIUS_API_KEY not found!")
            raise ValueError("HELIUS_API_KEY is required for Helius Logs Monitor")
        else:
            logger.info(f"✅ Helius API key loaded: {HELIUS_API_KEY[:10]}...")
        
        # Statistics
        self.logs_received = 0
        self.tokens_detected = 0
        self.tokens_processed = 0
        
    async def start(self):
        """Connect to Helius WebSocket and subscribe to PumpFun logs"""
        self.running = True
        
        ws_url = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
        
        logger.info("🔍 Connecting to Helius WebSocket...")
        logger.info(f"   Strategy: ⚡ RPC LOG SUBSCRIPTION - ULTRA LOW LATENCY")
        logger.info(f"   PumpFun Program: {PUMPFUN_PROGRAM_ID}")
        logger.info(f"   Expected latency: 0.2-0.8s (vs 8-12s PumpPortal)")
        
        while self.running:
            try:
                async with websockets.connect(
                    ws_url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5
                ) as websocket:
                    logger.info("✅ Connected to Helius WebSocket!")
                    
                    # Subscribe to PumpFun program logs
                    subscribe_msg = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "logsSubscribe",
                        "params": [
                            {
                                "mentions": [str(PUMPFUN_PROGRAM_ID)]
                            },
                            {
                                "commitment": "confirmed"
                            }
                        ]
                    }
                    
                    await websocket.send(json.dumps(subscribe_msg))
                    logger.info("📡 Subscribed to PumpFun program logs")
                    
                    # Listen for log notifications
                    while self.running:
                        try:
                            message = await asyncio.wait_for(websocket.recv(), timeout=30)
                            data = json.loads(message)
                            
                            # Handle subscription confirmation
                            if 'result' in data and 'id' in data:
                                logger.info(f"✅ Subscription confirmed - ID: {data['result']}")
                                continue
                            
                            # Handle log notifications
                            if 'params' in data:
                                self.logs_received += 1
                                # Log every 10th event to show activity
                                if self.logs_received % 10 == 0:
                                    logger.debug(f"📊 Processed {self.logs_received} log events")
                                await self._process_log_notification(data['params'])
                                
                        except asyncio.TimeoutError:
                            # Send ping to keep connection alive
                            await websocket.ping()
                        except Exception as e:
                            logger.error(f"Error processing message: {e}")
                            break
                            
            except Exception as e:
                logger.error(f"WebSocket connection error: {e}")
                if self.running:
                    self.reconnect_count += 1
                    self.seen_tokens.clear()
                    logger.info(f"Reconnecting in 5s... (attempt #{self.reconnect_count})")
                    await asyncio.sleep(5)
    
    async def _process_log_notification(self, params: Dict):
        """
        Process incoming log notification from Helius
        FIXED: Use transaction-based detection instead of log strings
        """
        try:
            result = params.get('result', {})
            value = result.get('value', {})
            
            signature = value.get('signature', '')
            
            if not signature:
                return
            
            # Don't filter by log strings - check the actual transaction
            detection_time = time.time()
            
            # Extract mint from transaction instructions
            mint = await self._extract_mint_from_transaction(signature)
            
            if not mint:
                # Not a new token creation, just other PumpFun activity
                return
            
            if mint in self.seen_tokens:
                return
            
            # New token detected!
            self.seen_tokens.add(mint)
            self.tokens_detected += 1
            self.tokens_processed += 1
            
            detection_latency_ms = (time.time() - detection_time) * 1000
            
            logger.info(f"🚀 NEW TOKEN DETECTED: {mint}")
            logger.info(f"   Signature: {signature[:16]}...")
            logger.info(f"   Detection #{self.tokens_detected}")
            logger.info(f"   Processing time: {detection_latency_ms:.0f}ms")
            
            # Pass to callback (existing on_token_found logic)
            if self.callback:
                await self.callback({
                    'mint': mint,
                    'signature': signature,
                    'type': 'pumpfun_launch',
                    'timestamp': datetime.now().isoformat(),
                    'source': 'helius_logs',
                    'detection_latency_ms': detection_latency_ms
                })
                
        except Exception as e:
            logger.error(f"Error processing log notification: {e}")
            import traceback
            logger.error(traceback.format_exc())
    
    async def _extract_mint_from_transaction(self, signature: str) -> Optional[str]:
        """
        Fetch transaction and check if it's an InitializeBondingCurve instruction
        Returns mint address if it's a new token, None otherwise
        """
        try:
            tx_sig = SoldersSignature.from_string(signature)
            
            # Fetch transaction details
            tx_response = self.rpc_client.get_transaction(
                tx_sig,
                encoding="jsonParsed",
                max_supported_transaction_version=0
            )
            
            if not tx_response or not tx_response.value:
                return None
            
            tx = tx_response.value
            message = tx.transaction.transaction.message
            
            # Check each instruction to find InitializeBondingCurve
            instructions = message.instructions
            
            for idx, instruction in enumerate(instructions):
                # Get program ID for this instruction
                program_id_index = instruction.program_id_index
                program_id = str(message.account_keys[program_id_index])
                
                # Check if this instruction is from PumpFun program
                if program_id != str(PUMPFUN_PROGRAM_ID):
                    continue
                
                # This is a PumpFun instruction - check if it's InitializeBondingCurve
                # InitializeBondingCurve is typically the first instruction and has specific accounts
                
                # Get accounts for this instruction
                accounts = instruction.accounts if hasattr(instruction, 'accounts') else []
                
                # InitializeBondingCurve has these accounts (in order):
                # 0: mint (writable)
                # 1: bonding_curve (writable)
                # 2: associated_bonding_curve (writable)
                # 3: global
                # 4: mpl_token_metadata
                # 5: metadata
                # 6: user (signer)
                # 7: system_program
                # 8: token_program
                # 9: associated_token_program
                # 10: rent
                # 11: event_authority
                # 12: program
                
                # If this instruction has 10+ accounts, it's likely InitializeBondingCurve
                if len(accounts) >= 10:
                    # The first account is the mint
                    mint_index = accounts[0]
                    mint = str(message.account_keys[mint_index])
                    
                    # Verify it's not a known program ID
                    if mint not in [
                        str(PUMPFUN_PROGRAM_ID),
                        "11111111111111111111111111111111",  # System Program
                        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # Token Program
                        "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"  # Associated Token Program
                    ]:
                        logger.debug(f"Found mint in InitializeBondingCurve: {mint[:8]}...")
                        return mint
            
            # Not an InitializeBondingCurve instruction
            return None
            
        except Exception as e:
            logger.debug(f"Error extracting mint (likely not a new token): {e}")
            return None
    
    def get_stats(self) -> Dict:
        """Get monitor statistics"""
        return {
            'logs_received': self.logs_received,
            'tokens_detected': self.tokens_detected,
            'tokens_processed': self.tokens_processed,
            'reconnect_count': self.reconnect_count
        }
    
    def stop(self):
        """Stop the monitor"""
        self.running = False
        stats = self.get_stats()
        logger.info(f"Helius logs monitor stopped")
        logger.info(f"Stats: {stats['tokens_processed']} processed / {stats['tokens_detected']} detected / {stats['logs_received']} logs")
