"""
PumpPortal WebSocket Monitor - Phase 1 Profitability Tweaks
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
        
        # UPDATED: Tighter quality filters for Phase 1 profitability
        self.filters = {
            'min_creator_sol': 0.5,      # Updated from 0.3
            'max_creator_sol': 3.0,      # Updated from 5.0
            'min_curve_sol': 2.0,        # Updated from 1.5
            'max_curve_sol': 50,         # Updated from 60
            'min_v_tokens': 500_000_000,
            'min_name_length': 3,
            'name_blacklist': [
                'test', 'rug', 'airdrop', 'claim', 'scam', 'fake',
                'elon', 'pepe', 'trump', 'doge', 'bonk', 'pump', 
                'moon', 'ai', 'safe', 'baby', 'inu', 'meta', 'grok'
            ],
            'filters_enabled': True
        }
        
        # Statistics
        self.tokens_seen = 0
        self.tokens_filtered = 0
        self.tokens_passed = 0
        
    def _apply_quality_filters(self, data: dict) -> bool:
        """
        Apply quality filters to token data.
        Returns True if token passes all filters, False otherwise.
        """
        if not self.filters['filters_enabled']:
            return True
            
        # Extract the actual token data
        token_data = data.get('data', data)
        
        # Filter 1: Creator initial buy amount
        creator_sol = float(token_data.get('solAmount', 0))
        if creator_sol < self.filters['min_creator_sol']:
            logger.debug(f"Filtered: Creator buy too low ({creator_sol:.3f} SOL)")
            return False
        if creator_sol > self.filters['max_creator_sol']:
            logger.debug(f"Filtered: Creator buy too high ({creator_sol:.1f} SOL)")
            return False
        
        # Filter 2: Name and symbol quality
        name = str(token_data.get('name', '')).strip()
        symbol = str(token_data.get('symbol', '')).strip()
        
        # Check minimum length
        if len(name) < self.filters['min_name_length']:
            logger.debug(f"Filtered: Name too short ({name})")
            return False
        
        # Check for ASCII only (avoid emojis and special chars)
        if not name.isascii() or not symbol.isascii():
            logger.debug(f"Filtered: Non-ASCII characters in name/symbol")
            return False
        
        # NEW: Check symbol is uppercase
        if not symbol.isupper():
            logger.debug(f"Filtered: Symbol not uppercase ({symbol})")
            return False
        
        # Check blacklist (case-insensitive)
        name_lower = name.lower()
        for blacklisted in self.filters['name_blacklist']:
            if blacklisted in name_lower:
                logger.debug(f"Filtered: Blacklisted word '{blacklisted}' in name")
                return False
        
        # Filter 3: Bonding curve SOL window
        v_sol = float(token_data.get('vSolInBondingCurve', 0))
        if v_sol < self.filters['min_curve_sol']:
            logger.debug(f"Filtered: Too early ({v_sol:.2f} SOL in curve)")
            return False
        if v_sol > self.filters['max_curve_sol']:
            logger.debug(f"Filtered: Too late ({v_sol:.2f} SOL in curve)")
            return False
        
        # Filter 4: Virtual token reserves sanity check
        v_tokens = float(token_data.get('vTokensInBondingCurve', 0))
        if v_tokens < self.filters['min_v_tokens']:
            logger.debug(f"Filtered: Insufficient virtual tokens ({v_tokens:,.0f})")
            return False
        
        # Filter 5: URI/Description blacklist (if available)
        uri = str(token_data.get('uri', '')).lower()
        description = str(token_data.get('description', '')).lower()
        for blacklisted in self.filters['name_blacklist']:
            if blacklisted in uri or blacklisted in description:
                logger.debug(f"Filtered: Blacklisted word in URI/description")
                return False
        
        # All filters passed
        logger.info(f"✅ Token passed all filters: {name} ({symbol}) | Creator: {creator_sol:.2f} SOL | Curve: {v_sol:.2f} SOL")
        return True
        
    async def start(self):
        """Connect to PumpPortal WebSocket"""
        self.running = True
        logger.info("🔍 Connecting to PumpPortal WebSocket...")
        logger.info(f"Quality filters: PHASE 1 TWEAKS (creator: {self.filters['min_creator_sol']}-{self.filters['max_creator_sol']} SOL, curve: {self.filters['min_curve_sol']}-{self.filters['max_curve_sol']} SOL)")
        
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
                                    self.tokens_seen += 1
                                    
                                    # Apply quality filters
                                    if not self._apply_quality_filters(data):
                                        self.tokens_filtered += 1
                                        logger.info(f"🚫 Token {mint[:8]}... filtered out ({self.tokens_filtered}/{self.tokens_seen} filtered)")
                                        continue
                                    
                                    self.tokens_passed += 1
                                    
                                    logger.info("=" * 60)
                                    logger.info("🚀 NEW QUALITY TOKEN DETECTED!")
                                    logger.info(f"📜 Mint: {mint}")
                                    logger.info(f"📊 Stats: {self.tokens_passed} passed / {self.tokens_filtered} filtered / {self.tokens_seen} total")
                                    
                                    # Extract key metrics for logging
                                    token_data = data.get('data', data)
                                    logger.info(f"💰 Creator buy: {token_data.get('solAmount', 0):.2f} SOL")
                                    logger.info(f"📈 Curve SOL: {token_data.get('vSolInBondingCurve', 0):.2f}")
                                    logger.info(f"📝 Name: {token_data.get('name', 'Unknown')}")
                                    logger.info(f"🔤 Symbol: {token_data.get('symbol', 'Unknown')}")
                                    logger.info("=" * 60)
                                    
                                    if self.callback:
                                        await self.callback({
                                            'mint': mint,
                                            'signature': data.get('signature', 'unknown'),
                                            'type': 'pumpfun_launch',
                                            'timestamp': datetime.now().isoformat(),
                                            'data': data,
                                            'source': 'pumpportal',
                                            'passed_filters': True
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
                    # Clear seen tokens on reconnect to catch any we missed
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
    
    def update_filter_config(self, new_filters: dict):
        """Update filter configuration dynamically"""
        self.filters.update(new_filters)
        logger.info(f"Updated filters: {self.filters}")
    
    def get_stats(self) -> dict:
        """Get filter statistics"""
        return {
            'tokens_seen': self.tokens_seen,
            'tokens_filtered': self.tokens_filtered,
            'tokens_passed': self.tokens_passed,
            'filter_rate': (self.tokens_filtered / self.tokens_seen * 100) if self.tokens_seen > 0 else 0
        }
    
    def stop(self):
        self.running = False
        stats = self.get_stats()
        logger.info(f"PumpPortal monitor stopped - Stats: {stats['tokens_passed']} passed, {stats['tokens_filtered']} filtered (rate: {stats['filter_rate']:.1f}%)")
