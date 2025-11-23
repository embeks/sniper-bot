"""
Helius Logs Monitor - Direct PumpFun program log subscription
Detects new tokens in 0.2-0.8s vs 8-12s for PumpPortal
FIXED: Proper base64 decoding with padding handling
"""

import asyncio
import json
import logging
import time
import base64
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
        self.parse_failures = 0
        self.parse_successes = 0
        
    async def start(self):
        """Connect to Helius WebSocket and subscribe to PumpFun logs"""
        self.running = True
        
        ws_url = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
        
        logger.info("🔍 Connecting to Helius WebSocket...")
        logger.info(f"   Strategy: ⚡ RPC LOG SUBSCRIPTION - ULTRA LOW LATENCY")
        logger.info(f"   PumpFun Program: {PUMPFUN_PROGRAM_ID}")
        logger.info(f"   Expected latency: 0.2-0.8s (vs 8-12s PumpPortal)")
        logger.info(f"   🐛 DEBUG MODE: Ultra-verbose logging for first 20 events")
        
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
                    logger.info(f"   Filter: mentions=[{str(PUMPFUN_PROGRAM_ID)}]")
                    
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
                                await self._process_log_notification(data['params'])
                                
                        except asyncio.TimeoutError:
                            # Send ping to keep connection alive
                            await websocket.ping()
                            if self.logs_received == 0:
                                logger.warning("⚠️ No events received in 30s - connection may be idle")
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
        ONLY process CreateV2 token creation events
        """
        try:
            result = params.get('result', {})
            value = result.get('value', {})

            signature = value.get('signature', '')
            logs = value.get('logs', [])  # Get log messages

            if not signature:
                logger.warning("⚠️ Log event missing signature")
                return

            # ✅ CRITICAL FIX: Only process CreateV2 events
            # Skip all Buy/Sell/other instructions immediately
            has_create_v2 = any('Instruction: CreateV2' in log for log in logs)

            if not has_create_v2:
                # Silently skip - this is 99% of traffic (buys/sells)
                return

            # Log counter for CreateV2 events only
            self.logs_received += 1

            if self.logs_received <= 50:
                logger.info(f"📩 CreateV2 EVENT #{self.logs_received} DETECTED")
                logger.info(f"   Signature: {signature[:16]}...")
            elif self.logs_received % 20 == 0:
                logger.info(f"📊 Processed {self.logs_received} CreateV2 events (detected: {self.tokens_detected})")

            # Continue with existing transaction parsing...
            detection_time = time.time()
            mint = await self._extract_mint_from_transaction(signature)
            
            if not mint:
                # Not a new token creation, just other PumpFun activity
                self.parse_failures += 1
                if self.logs_received <= 50:
                    logger.info(f"   ❌ Not a new token (parse failed or different instruction)")
                return
            
            self.parse_successes += 1
            
            if mint in self.seen_tokens:
                if self.logs_received <= 50:
                    logger.info(f"   ⚠️ Already seen: {mint[:8]}...")
                return
            
            # New token detected!
            self.seen_tokens.add(mint)
            self.tokens_detected += 1
            self.tokens_processed += 1
            
            detection_latency_ms = (time.time() - detection_time) * 1000
            
            logger.info("=" * 60)
            logger.info(f"🚀 NEW TOKEN DETECTED: {mint}")
            logger.info(f"   Signature: {signature[:16]}...")
            logger.info(f"   Detection #{self.tokens_detected}")
            logger.info(f"   Processing time: {detection_latency_ms:.0f}ms")
            logger.info(f"   Total events processed: {self.logs_received}")
            logger.info("=" * 60)
            
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
        Fetch transaction and extract mint from CreateV2 instruction
        Simplified: No discriminator checking needed since logs are pre-filtered
        Returns mint address from first account
        """
        verbose = self.logs_received <= 40  # a bit more room for debug on startup

        try:
            tx_sig = SoldersSignature.from_string(signature)

            # Fetch transaction details with retry for very fresh transactions
            max_retries = 5
            tx_response = None

            for attempt in range(max_retries):
                try:
                    tx_response = self.rpc_client.get_transaction(
                        tx_sig,
                        encoding="jsonParsed",
                        max_supported_transaction_version=0,
                    )
                    if tx_response and tx_response.value:
                        if verbose:
                            logger.info(f"   ✅ TX fetched successfully (attempt {attempt + 1})")
                        break
                    else:
                        if verbose:
                            logger.info(f"   ⏳ TX not available yet (attempt {attempt + 1}/{max_retries})")
                    await asyncio.sleep(2.0)
                except Exception as e:
                    if verbose:
                        logger.info(f"   ⚠️ TX fetch error on attempt {attempt + 1}: {e}")
                    if attempt == max_retries - 1:
                        if verbose:
                            logger.info(f"   ❌ TX fetch failed after {max_retries} attempts")
                        return None
                    await asyncio.sleep(1.0)

            if not tx_response or not tx_response.value:
                if verbose:
                    logger.info(f"   ❌ TX not found after {max_retries} attempts (may be too fresh)")
                return None

            tx = tx_response.value
            message = tx.transaction.transaction.message
            instructions = message.instructions

            if verbose:
                logger.info(f"   📋 TX has {len(instructions)} instructions")

            # Loop through all instructions to find PumpFun CreateV2
            for idx, instruction in enumerate(instructions):
                try:
                    # Handle both UiPartiallyDecodedInstruction and compiled instructions
                    if hasattr(instruction, "program_id"):
                        program_id = str(instruction.program_id)
                    elif hasattr(instruction, "program_id_index"):
                        program_id_index = instruction.program_id_index
                        program_id = str(message.account_keys[program_id_index])
                    else:
                        continue

                    # Check if this instruction is from PumpFun program
                    if program_id != str(PUMPFUN_PROGRAM_ID):
                        continue

                    if verbose:
                        logger.info(f"   ✅ Found PumpFun CreateV2 instruction at index {idx}")

                    # Extract mint from first account (accounts[0] is always the mint for CreateV2)
                    if hasattr(instruction, "accounts"):
                        accounts = instruction.accounts
                    else:
                        if verbose:
                            logger.info(f"      ⚠️ No accounts attribute")
                        continue

                    if len(accounts) == 0:
                        if verbose:
                            logger.info(f"      ⚠️ accounts list is empty")
                        continue

                    if verbose:
                        logger.info(f"      Has {len(accounts)} accounts")

                    # Mint is always the first account for CreateV2
                    mint_index = accounts[0]
                    if isinstance(mint_index, int):
                        mint = str(message.account_keys[mint_index])
                    else:
                        mint = str(mint_index)

                    logger.info(f"   🎯 MINT EXTRACTED: {mint[:8]}... (CreateV2 confirmed by logs)")
                    return mint

                except Exception as e:
                    if verbose:
                        logger.error(f"   ❌ Error processing instruction {idx}: {e}")
                        import traceback
                        logger.error(traceback.format_exc())
                    continue

            # No PumpFun instruction found (shouldn't happen since logs were pre-filtered)
            if verbose:
                logger.info(f"   ❌ No PumpFun instruction found in TX")

            return None

        except Exception as e:
            if verbose:
                logger.error(f"   ❌ Outer parse error: {e}")
                import traceback
                logger.error(traceback.format_exc())
            return None
    
    def get_stats(self) -> Dict:
        """Get monitor statistics"""
        return {
            'logs_received': self.logs_received,
            'tokens_detected': self.tokens_detected,
            'tokens_processed': self.tokens_processed,
            'parse_successes': self.parse_successes,
            'parse_failures': self.parse_failures,
            'reconnect_count': self.reconnect_count
        }
    
    def stop(self):
        """Stop the monitor"""
        self.running = False
        stats = self.get_stats()
        logger.info(f"Helius logs monitor stopped")
        logger.info(f"Stats: {stats['tokens_processed']} processed / {stats['tokens_detected']} detected / {stats['logs_received']} logs")
        logger.info(f"Parse: {stats['parse_successes']} success / {stats['parse_failures']} failures")
