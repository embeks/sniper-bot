"""
Helius Logs Monitor - Direct PumpFun program log subscription
Detects new tokens in 0.2-0.8s vs 8-12s for PumpPortal
FIXED: Extract mint directly from Program data - NO RPC CALL NEEDED
"""

import asyncio
import json
import logging
import time
import base64
import base58
import websockets
from datetime import datetime
from typing import Optional, Dict

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
        logger.info(f"   Strategy: ⚡ INSTANT MINT EXTRACTION FROM LOGS")
        logger.info(f"   PumpFun Program: {PUMPFUN_PROGRAM_ID}")
        logger.info(f"   Expected latency: <100ms (no RPC calls needed)")
        
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
            logs = value.get('logs', [])

            if not signature:
                logger.warning("⚠️ Log event missing signature")
                return

            # Only process CreateV2 events - skip all Buy/Sell/other
            has_create_v2 = any('Instruction: CreateV2' in log for log in logs)

            if not has_create_v2:
                return

            # Log counter for CreateV2 events only
            self.logs_received += 1

            if self.logs_received <= 20:
                logger.info(f"📩 CreateV2 EVENT #{self.logs_received} DETECTED")
                logger.info(f"   Signature: {signature[:16]}...")
            elif self.logs_received % 50 == 0:
                logger.info(f"📊 Processed {self.logs_received} CreateV2 events (detected: {self.tokens_detected})")

            # Extract mint directly from logs - NO RPC CALL NEEDED
            detection_time = time.time()
            mint = self._extract_mint_from_logs(logs)
            
            if not mint:
                self.parse_failures += 1
                if self.logs_received <= 20:
                    logger.info(f"   ❌ Failed to parse mint from logs")
                return
            
            self.parse_successes += 1
            
            if mint in self.seen_tokens:
                if self.logs_received <= 20:
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
            logger.info("=" * 60)
            
            # Trigger the trading callback
            if self.callback:
                try:
                    await self.callback({
                        'mint': mint,
                        'signature': signature,
                        'type': 'pumpfun_launch',
                        'timestamp': datetime.now().isoformat(),
                        'source': 'helius_logs',
                        'detection_latency_ms': detection_latency_ms,
                        'age': 0,
                        'token_age': 0,
                        'data': {}
                    })
                except Exception as e:
                    logger.error(f"Callback error: {e}")
                    import traceback
                    logger.error(traceback.format_exc())
                
        except Exception as e:
            logger.error(f"Error processing log notification: {e}")
            import traceback
            logger.error(traceback.format_exc())
    
    def _extract_mint_from_logs(self, logs: list) -> Optional[str]:
        """
        Extract mint directly from Program data log - NO RPC CALL NEEDED
        The CreateV2 event contains: discriminator + name + symbol + uri + MINT
        
        This replaces the old _extract_mint_from_transaction method that needed
        7 retries over 13 seconds. Now it's instant.
        """
        CREATE_V2_DISCRIMINATOR = "1b72a94ddeeb6376"
        
        try:
            # Find the Program data line that contains CreateV2 event
            program_data = None
            for log in logs:
                if log.startswith("Program data:"):
                    data_b64 = log.replace("Program data:", "").strip()
                    
                    # Fix base64 padding
                    padding = 4 - len(data_b64) % 4
                    if padding != 4:
                        data_b64 += '=' * padding
                    
                    try:
                        decoded = base64.b64decode(data_b64)
                        # Check if this is a CreateV2 event by discriminator
                        if len(decoded) >= 8 and decoded[:8].hex() == CREATE_V2_DISCRIMINATOR:
                            program_data = decoded
                            break
                    except:
                        continue
            
            if not program_data:
                return None
            
            # Parse the structure: discriminator(8) + name + symbol + uri + mint(32)
            pos = 8  # Skip discriminator
            
            # Skip name: length(4) + string
            if pos + 4 > len(program_data):
                return None
            name_len = int.from_bytes(program_data[pos:pos+4], 'little')
            pos += 4 + name_len
            
            # Skip symbol: length(4) + string
            if pos + 4 > len(program_data):
                return None
            symbol_len = int.from_bytes(program_data[pos:pos+4], 'little')
            pos += 4 + symbol_len
            
            # Skip URI: length(4) + string
            if pos + 4 > len(program_data):
                return None
            uri_len = int.from_bytes(program_data[pos:pos+4], 'little')
            pos += 4 + uri_len
            
            # Next 32 bytes = MINT
            if pos + 32 > len(program_data):
                logger.warning(f"   ⚠️ Not enough bytes for mint (need {pos+32}, have {len(program_data)})")
                return None
            
            mint_bytes = program_data[pos:pos+32]
            mint = base58.b58encode(mint_bytes).decode()
            
            return mint
            
        except Exception as e:
            logger.error(f"   ❌ Error parsing mint from logs: {e}")
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
