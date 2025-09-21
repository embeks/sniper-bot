"""
PumpPortal WebSocket Monitor - Direct feed from PumpFun
This connects to the actual PumpFun WebSocket for real-time token launches
FIXED: Clears seen_tokens on reconnect to avoid missing launches
"""

import asyncio
import json
import logging
import websockets
from datetime import datetime

logger = logging.getLogger(__name__)

class PumpPortalMonitor:
    def __init__(self, callback):
        self.callback = callback
        self.running = False
        self.seen_tokens = set()
        self.reconnect_count = 0
        
    async def start(self):
        """Connect to PumpPortal WebSocket"""
        self.running = True
        logger.info("🔍 Connecting to PumpPortal WebSocket...")
        
        uri = "wss://pumpportal.fun/api/data"
        
        while self.running:
            try:
                async with websockets.connect(uri) as websocket:
                    logger.info("✅ Connected to PumpPortal WebSocket!")
                    
                    # Log reconnect if this isn't first connection
                    if self.reconnect_count > 0:
                        logger.info(f"Reconnection #{self.reconnect_count} successful")
                    
                    # Subscribe to new tokens
                    subscribe_msg = {
                        "method": "subscribeNewToken"
                    }
                    await websocket.send(json.dumps(subscribe_msg))
                    logger.info("📡 Subscribed to new token events")
                    
                    # Listen for messages
                    while self.running:
                        try:
                            message = await asyncio.wait_for(websocket.recv(), timeout=30)
                            data = json.loads(message)
                            
                            # Log what we receive
                            logger.debug(f"Received: {str(data)[:200]}...")
                            
                            # Check for new token
                            if self._is_new_token(data):
                                mint = self._extract_mint(data)
                                
                                if mint and mint not in self.seen_tokens:
                                    self.seen_tokens.add(mint)
                                    
                                    logger.info("=" * 60)
                                    logger.info("🚀 NEW PUMPFUN TOKEN DETECTED!")
                                    logger.info(f"📜 Mint: {mint}")
                                    logger.info(f"📊 Data: {data}")
                                    logger.info("=" * 60)
                                    
                                    if self.callback:
                                        await self.callback({
                                            'mint': mint,
                                            'signature': data.get('signature', 'unknown'),
                                            'type': 'pumpfun_launch',
                                            'timestamp': datetime.now().isoformat(),
                                            'data': data,
                                            'source': 'pumpportal'  # Added source field
                                        })
                        
                        except asyncio.TimeoutError:
                            # Send ping to keep alive
                            await websocket.ping()
                            logger.debug("Sent ping to keep connection alive")
                        
                        except Exception as e:
                            logger.error(f"Message processing error: {e}")
                            break
                            
            except Exception as e:
                logger.error(f"WebSocket connection error: {e}")
                if self.running:
                    # FIXED: Clear seen tokens on reconnect to catch any we missed
                    self.seen_tokens.clear()
                    self.reconnect_count += 1
                    logger.info(f"Cleared seen tokens cache for fresh start after disconnect")
                    logger.info(f"Reconnecting in 5 seconds... (attempt #{self.reconnect_count + 1})")
                    await asyncio.sleep(5)
    
    def _is_new_token(self, data: dict) -> bool:
        """Check if message is a new token event"""
        # Different possible formats from PumpPortal
        if 'mint' in data:
            return True
        if 'token' in data and isinstance(data['token'], dict):
            return 'mint' in data['token']
        if 'type' in data and data['type'] in ['new_token', 'newToken', 'token_created']:
            return True
        # Check for nested data structure
        if 'data' in data and isinstance(data['data'], dict):
            if 'mint' in data['data']:
                return True
        return False
    
    def _extract_mint(self, data: dict) -> str:
        """Extract mint address from message"""
        # Try different fields
        if 'mint' in data:
            return data['mint']
        if 'token' in data and isinstance(data['token'], dict):
            if 'mint' in data['token']:
                return data['token']['mint']
            if 'address' in data['token']:
                return data['token']['address']
        if 'address' in data:
            return data['address']
        if 'tokenAddress' in data:
            return data['tokenAddress']
        # Check nested data structure
        if 'data' in data and isinstance(data['data'], dict):
            if 'mint' in data['data']:
                return data['data']['mint']
            if 'address' in data['data']:
                return data['data']['address']
        return None
    
    def stop(self):
        self.running = False
        logger.info(f"PumpPortal monitor stopped (processed {len(self.seen_tokens)} unique tokens)")
