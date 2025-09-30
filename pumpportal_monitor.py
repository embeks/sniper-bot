"""
PumpPortal WebSocket Monitor - Enhanced Anti-Rug Filters
"""

import asyncio
import json
import logging
import time
import websockets
import aiohttp
from datetime import datetime
from config import HELIUS_API_KEY

logger = logging.getLogger(__name__)

class PumpPortalMonitor:
    def __init__(self, callback):
        self.callback = callback
        self.running = False
        self.seen_tokens = set()
        self.reconnect_count = 0
        
        # Token velocity tracking
        self.token_history = {}  # {mint: [(timestamp, sol_amount), ...]}
        self.filter_reasons = {}  # Track why tokens were filtered
        
        # 30-85 SOL Strategy with enhanced filters
        self.filters = {
            'min_creator_sol': 0.7,
            'max_creator_sol': 2.0,
            'min_curve_sol': 15.0,  # LOWERED from 30 - catch earlier with more holders       
            'max_curve_sol': 60.0,  # LOWERED from 75 - more safety margin
            'min_v_tokens': 500_000_000,
            'min_name_length': 3,
            'min_holders': 20,  # INCREASED from 15 - need more distribution
            'max_top5_concentration': 55,  # TIGHTENED from 65 - more strict
            'max_velocity_sol_per_sec': 1.5,  # TIGHTENED from 2.0
            'min_time_to_target': 30,  # NEW - must take at least 30 seconds to reach min SOL
            'name_blacklist': [
                'test', 'rug', 'airdrop', 'claim', 'scam', 'fake',
                'elon', 'pepe', 'trump', 'doge', 'bonk', 'pump', 
                'moon', 'ai', 'safe', 'baby', 'inu', 'meta', 'grok',
                'token', 'coin', 'gem', 'launch', 'stealth', 'fair',
                'liquidity', 'burned', 'renounced', 'safu', 'based',
                'dev', 'team', 'official', 'meme', 'shib', 'floki',
                'cat', 'dog'  # NEW - common low-effort tokens
            ],
            'filters_enabled': True
        }
        
        # Statistics
        self.tokens_seen = 0
        self.tokens_filtered = 0
        self.tokens_passed = 0
        
    def _check_velocity(self, mint: str, v_sol: float) -> bool:
        """Reject instant pumps - require minimum time and reasonable growth rate"""
        now = time.time()
        
        if mint not in self.token_history:
            self.token_history[mint] = [(now, v_sol)]
            return True
        
        self.token_history[mint].append((now, v_sol))
        
        # Cleanup old data (keep last 10 minutes)
        cutoff = now - 600
        self.token_history[mint] = [(t, s) for t, s in self.token_history[mint] if t > cutoff]
        
        history = self.token_history[mint]
        if len(history) < 2:
            return True
        
        # Calculate total time elapsed and growth
        time_elapsed = history[-1][0] - history[0][0]
        sol_growth = history[-1][1] - history[0][1]
        
        # Minimum age requirement: 60 seconds
        if time_elapsed < 60:
            logger.debug(f"Velocity: only {time_elapsed:.0f}s old, need 60s minimum")
            return False
        
        # Calculate average growth rate (SOL per minute)
        growth_per_minute = (sol_growth / time_elapsed) * 60 if time_elapsed > 0 else 999
        
        # Organic tokens: 5-15 SOL/minute
        # Coordinated pumps: 30-60+ SOL/minute
        max_sol_per_minute = 20
        
        if growth_per_minute > max_sol_per_minute:
            logger.info(f"Velocity REJECT: {growth_per_minute:.1f} SOL/min (max {max_sol_per_minute})")
            return False
        
        logger.debug(f"Velocity OK: {growth_per_minute:.1f} SOL/min over {time_elapsed:.0f}s")
        return True
    
    async def _check_holders_helius(self, mint: str) -> bool:
        """Use Helius to verify holder distribution - reject concentrated supply"""
        try:
            async with aiohttp.ClientSession() as session:
                url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
                
                # Use getProgramAccounts to get token holders
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTokenLargestAccounts",
                    "params": [mint]
                }
                
                timeout = aiohttp.ClientTimeout(total=3)
                async with session.post(url, json=payload, timeout=timeout) as resp:
                    if resp.status != 200:
                        logger.debug(f"Helius check failed: HTTP {resp.status}")
                        return True
                    
                    data = await resp.json()
                    
                    # Check for RPC errors
                    if 'error' in data:
                        logger.debug(f"Helius RPC error: {data['error']}")
                        return True  # Fail open - token might be too new
                    
                    if 'result' not in data or 'value' not in data['result']:
                        logger.debug("No result/value in Helius response")
                        return True
                    
                    accounts = data['result']['value']
                    
                    # Minimum holder requirement
                    if len(accounts) < self.filters['min_holders']:
                        logger.info(f"Holder REJECT: only {len(accounts)} holders")
                        return False
                    
                    # Calculate concentration using amounts from largest accounts
                    total_supply = sum(float(acc.get('amount', 0)) for acc in accounts[:20])
                    if total_supply == 0:
                        logger.debug("Zero total supply, fail open")
                        return True
                    
                    top_5_supply = sum(float(acc.get('amount', 0)) for acc in accounts[:5])
                    concentration = (top_5_supply / total_supply * 100)
                    
                    if concentration > self.filters['max_top5_concentration']:
                        logger.info(f"Concentration REJECT: top 5 hold {concentration:.1f}%")
                        return False
                    
                    logger.debug(f"Holder check PASS: {len(accounts)} holders, top 5: {concentration:.1f}%")
                    return True
                    
        except asyncio.TimeoutError:
            logger.debug("Holder check timeout - fail open")
            return True
        except Exception as e:
            logger.error(f"Holder check error: {e}")
            return True
    
    def _log_filter(self, reason: str, detail: str):
        """Track why tokens are filtered"""
        if reason not in self.filter_reasons:
            self.filter_reasons[reason] = 0
        self.filter_reasons[reason] += 1
        logger.debug(f"Filtered ({reason}): {detail}")
    
    async def _apply_quality_filters(self, data: dict) -> bool:
        """
        Apply all quality filters including async Helius checks.
        Returns True if token passes all filters, False otherwise.
        """
        if not self.filters['filters_enabled']:
            return True
            
        # Extract the actual token data
        token_data = data.get('data', data)
        mint = self._extract_mint(data)
        
        # Filter 1: Creator initial buy amount
        creator_sol = float(token_data.get('solAmount', 0))
        if creator_sol < self.filters['min_creator_sol']:
            self._log_filter("creator_buy_low", f"{creator_sol:.3f} SOL")
            return False
        if creator_sol > self.filters['max_creator_sol']:
            self._log_filter("creator_buy_high", f"{creator_sol:.1f} SOL")
            return False
        
        # Filter 2: Name and symbol quality
        name = str(token_data.get('name', '')).strip()
        symbol = str(token_data.get('symbol', '')).strip()
        
        if len(name) < self.filters['min_name_length']:
            self._log_filter("name_short", name)
            return False
        
        if not name.isascii() or not symbol.isascii():
            self._log_filter("non_ascii", f"{name}/{symbol}")
            return False
        
        if not symbol.isupper():
            self._log_filter("symbol_lowercase", symbol)
            return False
        
        # Check blacklist
        name_lower = name.lower()
        for blacklisted in self.filters['name_blacklist']:
            if blacklisted in name_lower:
                self._log_filter("blacklist", f"'{blacklisted}' in {name}")
                return False
        
        # Filter 3: Bonding curve SOL window
        v_sol = float(token_data.get('vSolInBondingCurve', 0))
        if v_sol < self.filters['min_curve_sol']:
            self._log_filter("curve_low", f"{v_sol:.2f} SOL")
            return False
        if v_sol > self.filters['max_curve_sol']:
            self._log_filter("curve_high", f"{v_sol:.2f} SOL")
            return False
        
        # Filter 4: ENHANCED momentum check
        if v_sol < 35:
            required_multiplier = 8
        elif v_sol < 50:
            required_multiplier = 5
        else:
            required_multiplier = 3
        
        required_sol = creator_sol * required_multiplier
        if v_sol < required_sol:
            self._log_filter("momentum", f"{v_sol:.2f} vs {required_sol:.2f} needed ({required_multiplier}x)")
            return False
        
        # Filter 5: Virtual token reserves sanity check
        v_tokens = float(token_data.get('vTokensInBondingCurve', 0))
        if v_tokens < self.filters['min_v_tokens']:
            self._log_filter("tokens_low", f"{v_tokens:,.0f}")
            return False
        
        # Filter 6: URI/Description blacklist
        uri = str(token_data.get('uri', '')).lower()
        description = str(token_data.get('description', '')).lower()
        for blacklisted in self.filters['name_blacklist']:
            if blacklisted in uri or blacklisted in description:
                self._log_filter("metadata_blacklist", blacklisted)
                return False
        
        # Filter 7: Velocity check - must be at least 60 seconds old
        if not self._check_velocity(mint, v_sol):
            self._log_filter("velocity", "too fast or too young")
            return False
        
        # Filter 8: Helius holder distribution check - CRITICAL
        try:
            logger.info(f"🔍 Starting holder check for {mint[:8]}...")
            
            # Give network 3 seconds to propagate
            await asyncio.sleep(3)
            
            logger.info(f"🔍 About to call _check_holders_helius...")
            holder_check_result = await self._check_holders_helius(mint)
            logger.info(f"🔍 Holder check returned: {holder_check_result}")
            
            if not holder_check_result:
                self._log_filter("holder_distribution", "failed holder check")
                logger.warning(f"❌ Token {mint[:8]}... REJECTED by holder check")
                return False
            
            logger.info(f"✅ Token {mint[:8]}... passed holder check")
            
        except Exception as e:
            logger.error(f"❌ CRITICAL: Holder check exception for {mint[:8]}...: {e}")
            import traceback
            logger.error(traceback.format_exc())
            # Fail closed on exceptions
            self._log_filter("holder_distribution", f"exception: {e}")
            return False
        
        # All filters passed
        momentum = v_sol / creator_sol if creator_sol > 0 else 0
        logger.info(f"✅ PASSED ALL FILTERS: {name} ({symbol})")
        logger.info(f"   Creator: {creator_sol:.2f} SOL | Curve: {v_sol:.2f} SOL | Momentum: {momentum:.1f}x")
        return True
        
    async def start(self):
        """Connect to PumpPortal WebSocket"""
        self.running = True
        logger.info("🔍 Connecting to PumpPortal WebSocket...")
        logger.info(f"Strategy: 15-60 SOL with STRICT HOLDER CHECKS")
        logger.info(f"  Momentum: 8x@<35 SOL, 5x@<50 SOL, 3x@50+ SOL")
        logger.info(f"  Velocity: Max {self.filters['max_velocity_sol_per_sec']} SOL/sec")
        logger.info(f"  Holders: Min {self.filters['min_holders']}, top 5 <{self.filters['max_top5_concentration']}%")
        logger.info(f"  Min time: {self.filters['min_time_to_target']}s to reach target SOL")
        
        uri = "wss://pumpportal.fun/api/data"
        
        while self.running:
            try:
                async with websockets.connect(uri) as websocket:
                    logger.info("✅ Connected to PumpPortal WebSocket!")
                    
                    if self.reconnect_count > 0:
                        logger.info(f"Reconnection #{self.reconnect_count} successful")
                    
                    # Subscribe to new tokens
                    subscribe_msg = {"method": "subscribeNewToken"}
                    await websocket.send(json.dumps(subscribe_msg))
                    logger.info("📡 Subscribed to new token events")
                    
                    # Listen for messages
                    while self.running:
                        try:
                            message = await asyncio.wait_for(websocket.recv(), timeout=30)
                            data = json.loads(message)
                            
                            logger.debug(f"Received: {str(data)[:200]}...")
                            
                            # Check for new token
                            if self._is_new_token(data):
                                mint = self._extract_mint(data)
                                
                                if mint and mint not in self.seen_tokens:
                                    self.seen_tokens.add(mint)
                                    self.tokens_seen += 1
                                    
                                    # Apply quality filters (async)
                                    if not await self._apply_quality_filters(data):
                                        self.tokens_filtered += 1
                                        
                                        # Log filter stats periodically
                                        if self.tokens_seen % 10 == 0:
                                            filter_rate = (self.tokens_filtered / self.tokens_seen * 100)
                                            logger.info(f"📊 Filter stats: {self.tokens_filtered}/{self.tokens_seen} filtered ({filter_rate:.1f}%)")
                                            if self.filter_reasons:
                                                top_reasons = sorted(self.filter_reasons.items(), key=lambda x: x[1], reverse=True)[:3]
                                                logger.info(f"   Top reasons: {top_reasons}")
                                        continue
                                    
                                    self.tokens_passed += 1
                                    
                                    # Extract metrics
                                    token_data = data.get('data', data)
                                    v_sol = token_data.get('vSolInBondingCurve', 0)
                                    creator_sol = token_data.get('solAmount', 0)
                                    
                                    logger.info("=" * 60)
                                    logger.info("🚀 TOKEN PASSED ALL FILTERS!")
                                    logger.info(f"📜 Mint: {mint}")
                                    logger.info(f"📊 {self.tokens_passed} passed / {self.tokens_filtered} filtered / {self.tokens_seen} total")
                                    logger.info(f"💰 Creator: {creator_sol:.2f} SOL | Curve: {v_sol:.2f} SOL")
                                    logger.info(f"🔥 Momentum: {v_sol/creator_sol if creator_sol > 0 else 0:.1f}x")
                                    logger.info(f"📝 {token_data.get('name', 'Unknown')} ({token_data.get('symbol', 'Unknown')})")
                                    logger.info("=" * 60)
                                    
                                    if self.callback:
                                        await self.callback({
                                            'mint': mint,
                                            'signature': data.get('signature', 'unknown'),
                                            'type': 'pumpfun_launch',
                                            'timestamp': datetime.now().isoformat(),
                                            'data': data,
                                            'source': 'pumpportal',
                                            'passed_filters': True,
                                            'strategy': '30-85-enhanced'
                                        })
                        
                        except asyncio.TimeoutError:
                            await websocket.ping()
                            logger.debug("Sent ping")
                        
                        except Exception as e:
                            logger.error(f"Message processing error: {e}")
                            break
                            
            except Exception as e:
                logger.error(f"WebSocket connection error: {e}")
                if self.running:
                    self.seen_tokens.clear()
                    self.reconnect_count += 1
                    logger.info(f"Reconnecting in 5 seconds... (attempt #{self.reconnect_count + 1})")
                    await asyncio.sleep(5)
    
    def _is_new_token(self, data: dict) -> bool:
        """Check if message is a new token event"""
        if 'mint' in data:
            return True
        if 'token' in data and isinstance(data['token'], dict):
            return 'mint' in data['token']
        if 'type' in data and data['type'] in ['new_token', 'newToken', 'token_created']:
            return True
        if 'data' in data and isinstance(data['data'], dict):
            if 'mint' in data['data']:
                return True
        return False
    
    def _extract_mint(self, data: dict) -> str:
        """Extract mint address from message"""
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
            'filter_rate': (self.tokens_filtered / self.tokens_seen * 100) if self.tokens_seen > 0 else 0,
            'filter_reasons': self.filter_reasons
        }
    
    def stop(self):
        self.running = False
        stats = self.get_stats()
        logger.info(f"PumpPortal monitor stopped")
        logger.info(f"Stats: {stats['tokens_passed']} passed, {stats['tokens_filtered']} filtered ({stats['filter_rate']:.1f}%)")
        if self.filter_reasons:
            logger.info(f"Filter breakdown: {self.filter_reasons}")
