"""
PumpPortal WebSocket Monitor - Path B: FIXED age estimation
"""

import asyncio
import json
import logging
import time
import os
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
        
        # Verify Helius API key is available
        if not HELIUS_API_KEY:
            logger.error("CRITICAL: HELIUS_API_KEY not found!")
            raise ValueError("HELIUS_API_KEY is required for holder checks")
        else:
            logger.info(f"Helius API key loaded: {HELIUS_API_KEY[:10]}...")
        
        # Token velocity tracking
        self.token_history = {}
        self.filter_reasons = {}
        
        # SOL price caching for MC calculations
        self.sol_price_usd = 250
        self.last_sol_price_update = 0
        
        # PATH B FILTERS: Re-enable concentration after data collection
        self.filters = {
            'min_creator_sol': 0.1,
            'max_creator_sol': 5.0,
            'min_curve_sol': 25.0,
            'max_curve_sol': 60.0,
            'min_v_tokens': 500_000_000,
            'min_name_length': 3,
            'min_holders': 5,
            'check_concentration': False,  # Set to True after collecting 20-30 trades
            'max_top10_concentration': 85,
            'max_velocity_sol_per_sec': 1.5,
            'min_token_age_seconds': 150,
            'min_market_cap': 6000,
            'max_market_cap': 60000,
            'min_mc_gain_2min': 15,
            'max_token_age_minutes': 8,
            'name_blacklist': [
                'test', 'rug', 'airdrop', 'claim', 'scam', 'fake',
                'stealth', 'fair', 'liquidity', 'burned', 'renounced', 'safu', 
                'dev', 'team', 'official'
            ],
            'filters_enabled': True
        }
        
        # Statistics
        self.tokens_seen = 0
        self.tokens_filtered = 0
        self.tokens_passed = 0
    
    async def _get_sol_price(self) -> float:
        """Get current SOL price with caching (5 min cache)"""
        if time.time() - self.last_sol_price_update < 300:
            return self.sol_price_usd
        
        try:
            birdeye_key = os.getenv('BIRDEYE_API_KEY', '')
            if not birdeye_key:
                logger.debug("No Birdeye API key, using cached SOL price")
                return self.sol_price_usd
            
            async with aiohttp.ClientSession() as session:
                url = "https://public-api.birdeye.so/public/price?address=So11111111111111111111111111111111111111112"
                headers = {"X-API-KEY": birdeye_key}
                timeout = aiohttp.ClientTimeout(total=3)
                async with session.get(url, headers=headers, timeout=timeout) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self.sol_price_usd = float(data['data']['value'])
                        self.last_sol_price_update = time.time()
                        logger.debug(f"Updated SOL price: ${self.sol_price_usd:.2f}")
        except Exception as e:
            logger.debug(f"SOL price fetch failed: {e}, using cached ${self.sol_price_usd:.2f}")
        
        return self.sol_price_usd
    
    def _calculate_market_cap(self, token_data: dict) -> float:
        """Calculate market cap from bonding curve data"""
        try:
            v_sol = float(token_data.get('vSolInBondingCurve', 0))
            v_tokens = float(token_data.get('vTokensInBondingCurve', 0))
            
            if v_sol == 0 or v_tokens == 0:
                logger.debug("Cannot calculate MC - missing bonding curve data")
                return 0
            
            price_sol = v_sol / v_tokens
            total_supply = 1_000_000_000
            market_cap_usd = total_supply * price_sol * self.sol_price_usd
            
            return market_cap_usd
        except Exception as e:
            logger.error(f"MC calculation error: {e}")
            return 0
    
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
        
        time_elapsed = history[-1][0] - history[0][0]
        sol_growth = history[-1][1] - history[0][1]
        
        # Minimum age requirement: 60 seconds
        if time_elapsed < 60:
            logger.debug(f"Velocity: only {time_elapsed:.0f}s old, need 60s minimum")
            return False
        
        growth_per_minute = (sol_growth / time_elapsed) * 60 if time_elapsed > 0 else 999
        max_sol_per_minute = 20
        
        if growth_per_minute > max_sol_per_minute:
            logger.info(f"Velocity REJECT: {growth_per_minute:.1f} SOL/min (max {max_sol_per_minute})")
            return False
        
        logger.debug(f"Velocity OK: {growth_per_minute:.1f} SOL/min over {time_elapsed:.0f}s")
        return True
    
    async def _check_holders_helius(self, mint: str, retry: bool = True) -> dict:
        """
        Use Helius to verify holder distribution with retry logic.
        Returns dict with 'passed', 'holder_count', 'concentration', and 'reason'.
        """
        try:
            logger.info(f"Checking holders for {mint[:8]}... via Helius")
            
            async with aiohttp.ClientSession() as session:
                url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
                
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTokenLargestAccounts",
                    "params": [mint]
                }
                
                timeout = aiohttp.ClientTimeout(total=3)
                async with session.post(url, json=payload, timeout=timeout) as resp:
                    if resp.status != 200:
                        logger.warning(f"Helius HTTP error: {resp.status}")
                        return {'passed': False, 'reason': f'HTTP {resp.status}'}
                    
                    data = await resp.json()
                    logger.debug(f"Helius response: {data}")
                    
                    if 'error' in data:
                        error_msg = data['error'].get('message', '')
                        
                        # Handle "not a Token mint" error with retry
                        if 'not a Token mint' in error_msg and retry:
                            logger.info(f"Token not indexed yet, waiting 10s and retrying...")
                            await asyncio.sleep(10)
                            return await self._check_holders_helius(mint, retry=False)
                        
                        logger.warning(f"Helius RPC error: {data['error']}")
                        return {'passed': False, 'reason': error_msg}
                    
                    if 'result' not in data or 'value' not in data['result']:
                        logger.warning(f"Helius malformed response")
                        return {'passed': False, 'reason': 'malformed response'}
                    
                    accounts = data['result']['value']
                    account_count = len(accounts)
                    
                    logger.info(f"Received {account_count} top holder accounts (API limit: 20)")
                    
                    # CRITICAL CHECK 1: Minimum holder count
                    if account_count < self.filters['min_holders']:
                        logger.warning(f"REJECT: Only {account_count} holders (need {self.filters['min_holders']}+)")
                        return {
                            'passed': False,
                            'holder_count': account_count,
                            'reason': f'only {account_count} holders'
                        }
                    
                    # Calculate Top 10 concentration
                    total_supply = sum(float(acc.get('amount', 0)) for acc in accounts)
                    if total_supply == 0:
                        logger.warning(f"REJECT: Zero total supply")
                        return {'passed': False, 'reason': 'zero supply'}
                    
                    top_10_count = min(10, account_count)
                    top_10_supply = sum(float(acc.get('amount', 0)) for acc in accounts[:top_10_count])
                    concentration = (top_10_supply / total_supply * 100)
                    
                    logger.info(f"Top {top_10_count} concentration: {concentration:.1f}%")
                    
                    # CRITICAL CHECK 2: Concentration limit
                    if self.filters.get('check_concentration', True):
                        if concentration > self.filters['max_top10_concentration']:
                            logger.warning(f"REJECT: Top {top_10_count} hold {concentration:.1f}% (max {self.filters['max_top10_concentration']}%)")
                            return {
                                'passed': False,
                                'holder_count': account_count,
                                'concentration': concentration,
                                'reason': f'concentration {concentration:.1f}%'
                            }
                    else:
                        logger.warning(f"CONCENTRATION CHECK DISABLED: Top {top_10_count} = {concentration:.1f}% (normally max {self.filters['max_top10_concentration']}%)")
                    
                    logger.info(f"Holder check PASSED: {account_count} holders, Top {top_10_count} concentration: {concentration:.1f}%")
                    return {
                        'passed': True,
                        'holder_count': account_count,
                        'concentration': concentration
                    }
                    
        except asyncio.TimeoutError:
            logger.warning("Helius timeout (3s)")
            return {'passed': False, 'reason': 'timeout'}
        except Exception as e:
            logger.error(f"Helius exception: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return {'passed': False, 'reason': str(e)}
    
    def _log_filter(self, reason: str, detail: str):
        """Track why tokens are filtered"""
        if reason not in self.filter_reasons:
            self.filter_reasons[reason] = 0
        self.filter_reasons[reason] += 1
        logger.debug(f"Filtered ({reason}): {detail}")
    
    async def _apply_quality_filters(self, data: dict) -> bool:
        """Apply all quality filters including async Helius checks"""
        if not self.filters['filters_enabled']:
            return True
            
        # Extract the actual token data
        token_data = data.get('data', data)
        mint = self._extract_mint(data)
        
        now = time.time()
        v_sol = float(token_data.get('vSolInBondingCurve', 0))
        
        # FIXED: Age estimation based on SOL in curve (session-independent)
        # Assumption: Organic tokens grow at ~1.5 SOL/minute on average
        estimated_age_minutes = v_sol / 1.5
        
        # Age check 1: Minimum SOL requirement (proxy for age)
        if v_sol < 30:
            self._log_filter("too_young", f"only {v_sol:.1f} SOL in curve (need 30+ for age verification)")
            return False
        
        # FIXED: Max age check using SOL-based estimation (not session-based tracking)
        if estimated_age_minutes > self.filters['max_token_age_minutes']:
            self._log_filter("too_old", f"~{estimated_age_minutes:.1f} minutes old based on {v_sol:.1f} SOL (max {self.filters['max_token_age_minutes']})")
            return False
        
        logger.info(f"Token {mint[:8]}... has {v_sol:.1f} SOL in curve, estimated age: ~{estimated_age_minutes:.1f}m - proceeding with filters")
        
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
        
        # Check blacklist
        name_lower = name.lower()
        for blacklisted in self.filters['name_blacklist']:
            if blacklisted in name_lower:
                self._log_filter("blacklist", f"'{blacklisted}' in {name}")
                return False
        
        # Filter 3: Bonding curve SOL window
        if v_sol < self.filters['min_curve_sol']:
            self._log_filter("curve_low", f"{v_sol:.2f} SOL")
            return False
        if v_sol > self.filters['max_curve_sol']:
            self._log_filter("curve_high", f"{v_sol:.2f} SOL")
            return False
        
        # Filter 4: Enhanced momentum check
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
        
        # Filter 7: Velocity check
        if not self._check_velocity(mint, v_sol):
            self._log_filter("velocity", "too fast or too young")
            return False
        
        # Filter 7.5: Market Cap range check
        await self._get_sol_price()
        market_cap = self._calculate_market_cap(token_data)
        
        if market_cap == 0:
            self._log_filter("mc_calculation_failed", "Could not calculate MC")
            return False
        
        if market_cap < self.filters['min_market_cap']:
            self._log_filter("mc_too_low", f"${market_cap:,.0f}")
            return False
        
        if market_cap > self.filters['max_market_cap']:
            self._log_filter("mc_too_high", f"${market_cap:,.0f}")
            return False
        
        logger.info(f"MC check passed: ${market_cap:,.0f} (target: ${self.filters['min_market_cap']:,}-${self.filters['max_market_cap']:,})")
        
        # Filter 8: Helius holder distribution check
        holder_result = None
        try:
            logger.info(f"Starting holder check for {mint[:8]}... (SOL in curve: {v_sol:.1f})")
            
            await asyncio.sleep(3)
            
            logger.info(f"Calling Helius API...")
            holder_result = await self._check_holders_helius(mint)
            logger.info(f"Holder check returned: {holder_result}")
            
            if not holder_result['passed']:
                self._log_filter("holder_distribution", holder_result.get('reason', 'unknown'))
                logger.warning(f"Token {mint[:8]}... REJECTED by holder check: {holder_result.get('reason')}")
                return False
            
            logger.info(f"Token {mint[:8]}... passed holder check: {holder_result.get('holder_count')} holders, {holder_result.get('concentration', 0):.1f}% concentration")
            
            # Store for use in callback
            self._last_holder_result = holder_result
            
        except Exception as e:
            logger.error(f"CRITICAL: Holder check exception for {mint[:8]}...: {e}")
            import traceback
            logger.error(traceback.format_exc())
            self._log_filter("holder_distribution", f"exception: {e}")
            return False
        
        # All filters passed
        momentum = v_sol / creator_sol if creator_sol > 0 else 0
        holder_count = holder_result.get('holder_count', 0) if holder_result else 0
        concentration = holder_result.get('concentration', 0) if holder_result else 0
        
        logger.info(f"PASSED ALL FILTERS: {name} ({symbol})")
        logger.info(f"   Creator: {creator_sol:.2f} SOL | Curve: {v_sol:.2f} SOL | MC: ${market_cap:,.0f} | Momentum: {momentum:.1f}x")
        logger.info(f"   Holders: {holder_count} | Concentration: {concentration:.1f}%")
        return True
        
    async def start(self):
        """Connect to PumpPortal WebSocket"""
        self.running = True
        logger.info("Connecting to PumpPortal WebSocket...")
        concentration_status = "DISABLED" if not self.filters.get('check_concentration', True) else f"max {self.filters['max_top10_concentration']}%"
        logger.info(f"Strategy: PATH B - MC + Holder Verification")
        logger.info(f"  Bonding Curve: {self.filters['min_curve_sol']}-{self.filters['max_curve_sol']} SOL")
        logger.info(f"  Market Cap: ${self.filters['min_market_cap']:,}-${self.filters['max_market_cap']:,}")
        logger.info(f"  Age: ~2-8 minutes (based on SOL in curve)")
        logger.info(f"  Min Holders: {self.filters['min_holders']}")
        logger.info(f"  Concentration: {concentration_status}")
        logger.info(f"  Momentum: 8x@<35 SOL, 5x@<50 SOL, 3x@50+ SOL")
        
        uri = "wss://pumpportal.fun/api/data"
        
        while self.running:
            try:
                async with websockets.connect(uri) as websocket:
                    logger.info("Connected to PumpPortal WebSocket!")
                    
                    if self.reconnect_count > 0:
                        logger.info(f"Reconnection #{self.reconnect_count} successful")
                    
                    subscribe_msg = {"method": "subscribeNewToken"}
                    await websocket.send(json.dumps(subscribe_msg))
                    logger.info("Subscribed to new token events")
                    
                    while self.running:
                        try:
                            message = await asyncio.wait_for(websocket.recv(), timeout=30)
                            data = json.loads(message)
                            
                            logger.debug(f"Received: {str(data)[:200]}...")
                            
                            if self._is_new_token(data):
                                mint = self._extract_mint(data)
                                
                                if mint and mint not in self.seen_tokens:
                                    self.seen_tokens.add(mint)
                                    self.tokens_seen += 1
                                    
                                    if not await self._apply_quality_filters(data):
                                        self.tokens_filtered += 1
                                        
                                        if self.tokens_seen % 10 == 0:
                                            filter_rate = (self.tokens_filtered / self.tokens_seen * 100)
                                            logger.info(f"Filter stats: {self.tokens_filtered}/{self.tokens_seen} filtered ({filter_rate:.1f}%)")
                                            if self.filter_reasons:
                                                top_reasons = sorted(self.filter_reasons.items(), key=lambda x: x[1], reverse=True)[:3]
                                                logger.info(f"   Top reasons: {top_reasons}")
                                        continue
                                    
                                    self.tokens_passed += 1
                                    
                                    token_data = data.get('data', data)
                                    v_sol = token_data.get('vSolInBondingCurve', 0)
                                    creator_sol = token_data.get('solAmount', 0)
                                    market_cap = self._calculate_market_cap(token_data)
                                    
                                    holder_data = getattr(self, '_last_holder_result', {
                                        'holder_count': 0,
                                        'concentration': 0
                                    })
                                    
                                    logger.info("=" * 60)
                                    logger.info("TOKEN PASSED ALL FILTERS!")
                                    logger.info(f"Mint: {mint}")
                                    logger.info(f"{self.tokens_passed} passed / {self.tokens_filtered} filtered / {self.tokens_seen} total")
                                    logger.info(f"Creator: {creator_sol:.2f} SOL | Curve: {v_sol:.2f} SOL")
                                    logger.info(f"Market Cap: ${market_cap:,.0f}")
                                    logger.info(f"Momentum: {v_sol/creator_sol if creator_sol > 0 else 0:.1f}x")
                                    logger.info(f"Holders: {holder_data.get('holder_count', 0)} | Concentration: {holder_data.get('concentration', 0):.1f}%")
                                    logger.info(f"{token_data.get('name', 'Unknown')} ({token_data.get('symbol', 'Unknown')})")
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
                                            'strategy': 'path_b_fixed',
                                            'market_cap': market_cap,
                                            'holder_data': holder_data
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
