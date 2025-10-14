"""
PumpPortal WebSocket Monitor - ENHANCED with Recent Velocity Check
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
            logger.error("❌ CRITICAL: HELIUS_API_KEY not found!")
            raise ValueError("HELIUS_API_KEY is required for holder checks")
        else:
            logger.info(f"✅ Helius API key loaded: {HELIUS_API_KEY[:10]}...")
        
        # Token velocity tracking
        self.token_history = {}
        self.filter_reasons = {}
        
        # NEW: Recent velocity snapshots (for 1-second checks)
        self.recent_velocity_snapshots = {}
        
        # NEW: Creator spam tracking
        self.creator_token_launches = {}  # {creator_address: [timestamps]}
        
        # Token tracking for age and MC history
        self.token_first_seen = {}
        self.token_mc_history = {}
        
        # SOL price caching for MC calculations
        self.sol_price_usd = 250
        self.last_sol_price_update = 0
        
        # PATH B FILTERS: Enhanced with rug keywords
        self.filters = {
            'min_creator_sol': 0.1,
            'max_creator_sol': 5.0,
            'min_curve_sol': 15.0,
            'max_curve_sol': 45.0,
            'min_v_tokens': 500_000_000,
            'min_name_length': 3,
            'min_holders': 10,
            'check_concentration': False,  # DISABLED FOR TESTING
            'max_top10_concentration': 85,
            'max_velocity_sol_per_sec': 1.5,
            'min_token_age_seconds': 150,
            'min_market_cap': 4000,
            'max_market_cap': 35000,
            'min_mc_gain_2min': 15,
            'max_token_age_minutes': 8,
            # ENHANCED: Expanded rug keyword list
            'name_blacklist': [
                'test', 'rug', 'airdrop', 'claim', 'scam', 'fake',
                'stealth', 'fair', 'liquidity', 'burned', 'renounced', 'safu', 
                'dev', 'team', 'official',
                # NEW: Common rug patterns
                'pepe', 'elon', 'trump', 'inu', 'doge', 'shib', 'floki',
                'moon', 'safe', 'baby', 'mini', 'rocket', 'gem'
            ],
            # NEW: Recent velocity check (last 1 second must be ≥ this)
            'min_recent_velocity_sol_per_sec': 1.0,
            # NEW: Creator spam limit (max tokens per creator in 24h)
            'max_tokens_per_creator_24h': 3,
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
    
    def _store_recent_velocity_snapshot(self, mint: str, sol_raised: float):
        """Store snapshot for recent velocity checking"""
        now = time.time()
        
        if mint not in self.recent_velocity_snapshots:
            self.recent_velocity_snapshots[mint] = []
        
        self.recent_velocity_snapshots[mint].append({
            'timestamp': now,
            'sol_raised': sol_raised
        })
        
        # Keep only last 10 snapshots (last ~10 seconds)
        if len(self.recent_velocity_snapshots[mint]) > 10:
            self.recent_velocity_snapshots[mint] = self.recent_velocity_snapshots[mint][-10:]
    
    def _check_recent_velocity(self, mint: str, current_sol: float) -> tuple:
        """
        NEW: Check velocity in the LAST 1 SECOND (not average over lifetime).
        This catches tokens that are flattening/dying.
        
        Returns: (passed, sol_per_sec, reason)
        """
        try:
            # Need at least one previous snapshot
            if mint not in self.recent_velocity_snapshots or len(self.recent_velocity_snapshots[mint]) == 0:
                # First time seeing, can't check yet - PASS for now
                logger.debug(f"Recent velocity: First snapshot for {mint[:8]}, skipping check")
                return (True, None, "first_snapshot")
            
            history = self.recent_velocity_snapshots[mint]
            
            # Need at least 2 snapshots to compare
            if len(history) < 2:
                logger.debug(f"Recent velocity: Only 1 snapshot for {mint[:8]}, skipping check")
                return (True, None, "insufficient_history")
            
            now = time.time()
            
            # Find snapshot from ~1 second ago
            one_sec_ago = now - 1.0
            closest_snapshot = None
            min_time_diff = float('inf')
            
            for snap in history[:-1]:  # Don't compare with the current (last) snapshot
                time_diff = abs(snap['timestamp'] - one_sec_ago)
                if time_diff < min_time_diff:
                    min_time_diff = time_diff
                    closest_snapshot = snap
            
            # If we found a snapshot within 2s, use it
            if closest_snapshot and min_time_diff < 2.0:
                time_delta = now - closest_snapshot['timestamp']
                sol_delta = current_sol - closest_snapshot['sol_raised']
                
                if time_delta > 0:
                    recent_velocity = max(0, sol_delta / time_delta)
                    
                    min_required = self.filters['min_recent_velocity_sol_per_sec']
                    
                    if recent_velocity < min_required:
                        logger.info(
                            f"❌ RECENT VELOCITY TOO LOW: {recent_velocity:.2f} SOL/s in last {time_delta:.1f}s "
                            f"(need ≥{min_required} SOL/s) - pump is dying"
                        )
                        return (False, recent_velocity, f"recent_velocity_low: {recent_velocity:.2f} SOL/s")
                    
                    logger.info(f"✅ Recent velocity OK: {recent_velocity:.2f} SOL/s in last {time_delta:.1f}s")
                    return (True, recent_velocity, "ok")
            
            # Not enough time elapsed yet
            logger.debug(f"Recent velocity: Not enough time elapsed for {mint[:8]}")
            return (True, None, "insufficient_time")
            
        except Exception as e:
            logger.error(f"Error checking recent velocity: {e}")
            # On error, let it pass (don't block good tokens due to bugs)
            return (True, None, f"error: {e}")
    
    def _check_velocity(self, mint: str, v_sol: float) -> bool:
        """Original velocity check - growth rate over time"""
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
        
        # Calculate average growth rate (SOL per minute)
        growth_per_minute = (sol_growth / time_elapsed) * 60 if time_elapsed > 0 else 999
        
        max_sol_per_minute = 20
        
        if growth_per_minute > max_sol_per_minute:
            logger.info(f"Velocity REJECT: {growth_per_minute:.1f} SOL/min (max {max_sol_per_minute})")
            return False
        
        logger.debug(f"Velocity OK: {growth_per_minute:.1f} SOL/min over {time_elapsed:.0f}s")
        return True
    
    def _check_creator_spam(self, creator_address: str) -> tuple:
        """
        NEW: Check if creator is spamming tokens.
        Returns: (passed, token_count, reason)
        """
        try:
            now = time.time()
            
            # Clean up old entries (> 24h)
            if creator_address in self.creator_token_launches:
                self.creator_token_launches[creator_address] = [
                    ts for ts in self.creator_token_launches[creator_address]
                    if now - ts < 86400  # 24 hours
                ]
            
            # Check how many tokens this creator launched in 24h
            token_count = len(self.creator_token_launches.get(creator_address, []))
            max_allowed = self.filters['max_tokens_per_creator_24h']
            
            if token_count >= max_allowed:
                logger.info(
                    f"❌ CREATOR SPAM: {creator_address[:8]}... launched "
                    f"{token_count} tokens in 24h (max {max_allowed})"
                )
                return (False, token_count, f"creator_spam: {token_count}_tokens")
            
            # Record this token launch
            if creator_address not in self.creator_token_launches:
                self.creator_token_launches[creator_address] = []
            self.creator_token_launches[creator_address].append(now)
            
            logger.debug(f"✅ Creator OK: {creator_address[:8]}... ({token_count + 1} tokens/24h)")
            return (True, token_count + 1, "ok")
            
        except Exception as e:
            logger.error(f"Error checking creator spam: {e}")
            # On error, let it pass
            return (True, 0, f"error: {e}")
    
    async def _check_holders_helius(self, mint: str, retry: bool = True) -> dict:
        """Use Helius to verify holder distribution with retry logic"""
        try:
            logger.info(f"🔍 Checking holders for {mint[:8]}... via Helius")
            
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
                        logger.warning(f"❌ Helius HTTP error: {resp.status}")
                        return {'passed': False, 'reason': f'HTTP {resp.status}'}
                    
                    data = await resp.json()
                    
                    if 'error' in data:
                        error_msg = data['error'].get('message', '')
                        
                        if 'not a Token mint' in error_msg and retry:
                            logger.info(f"⏳ Token not indexed yet, waiting 10s and retrying...")
                            await asyncio.sleep(10)
                            return await self._check_holders_helius(mint, retry=False)
                        
                        logger.warning(f"❌ Helius RPC error: {data['error']}")
                        return {'passed': False, 'reason': error_msg}
                    
                    if 'result' not in data or 'value' not in data['result']:
                        logger.warning(f"❌ Helius malformed response")
                        return {'passed': False, 'reason': 'malformed response'}
                    
                    accounts = data['result']['value']
                    account_count = len(accounts)
                    
                    logger.info(f"📊 Received {account_count} top holder accounts")
                    
                    if account_count < self.filters['min_holders']:
                        logger.warning(f"❌ REJECT: Only {account_count} holders (need {self.filters['min_holders']}+)")
                        return {
                            'passed': False,
                            'holder_count': account_count,
                            'reason': f'only {account_count} holders'
                        }
                    
                    total_supply = sum(float(acc.get('amount', 0)) for acc in accounts)
                    if total_supply == 0:
                        logger.warning(f"❌ REJECT: Zero total supply")
                        return {'passed': False, 'reason': 'zero supply'}
                    
                    top_10_count = min(10, account_count)
                    top_10_supply = sum(float(acc.get('amount', 0)) for acc in accounts[:top_10_count])
                    concentration = (top_10_supply / total_supply * 100)
                    
                    logger.info(f"📊 Top {top_10_count} concentration: {concentration:.1f}%")
                    
                    if self.filters.get('check_concentration', True):
                        if concentration > self.filters['max_top10_concentration']:
                            logger.warning(f"❌ REJECT: Top {top_10_count} hold {concentration:.1f}% (max {self.filters['max_top10_concentration']}%)")
                            return {
                                'passed': False,
                                'holder_count': account_count,
                                'concentration': concentration,
                                'reason': f'concentration {concentration:.1f}%'
                            }
                    else:
                        logger.warning(f"⚠️  CONCENTRATION CHECK DISABLED: Top {top_10_count} = {concentration:.1f}%")
                    
                    logger.info(f"✅ Holder check PASSED: {account_count} holders")
                    return {
                        'passed': True,
                        'holder_count': account_count,
                        'concentration': concentration
                    }
                    
        except Exception as e:
            logger.error(f"❌ Helius exception: {e}")
            return {'passed': False, 'reason': str(e)}
    
    def _log_filter(self, reason: str, detail: str):
        """Track why tokens are filtered"""
        if reason not in self.filter_reasons:
            self.filter_reasons[reason] = 0
        self.filter_reasons[reason] += 1
        logger.debug(f"Filtered ({reason}): {detail}")
    
    async def _apply_quality_filters(self, data: dict) -> bool:
        """Apply all quality filters including recent velocity check"""
        if not self.filters['filters_enabled']:
            return True
            
        token_data = data.get('data', data)
        mint = self._extract_mint(data)
        
        now = time.time()
        v_sol = float(token_data.get('vSolInBondingCurve', 0))
        
        # Track when we first see this token
        if mint not in self.token_first_seen:
            self.token_first_seen[mint]= now
        
        # Store snapshot for recent velocity
        self._store_recent_velocity_snapshot(mint, v_sol)
        
        # Age proxy
        if v_sol < 30:
            self._log_filter("too_young", f"only {v_sol:.1f} SOL in curve")
            return False
        
        token_age_minutes = (now - self.token_first_seen[mint]) / 60
        if token_age_minutes > self.filters['max_token_age_minutes']:
            self._log_filter("too_old", f"{token_age_minutes:.1f} minutes")
            return False
        
        logger.info(f"✓ Token {mint[:8]}... has {v_sol:.1f} SOL in curve, age: {token_age_minutes:.1f}m")
        
        # Creator buy amount
        creator_sol = float(token_data.get('solAmount', 0))
        creator_address = str(token_data.get('traderPublicKey', 'unknown'))
        
        if creator_sol < self.filters['min_creator_sol']:
            self._log_filter("creator_buy_low", f"{creator_sol:.3f} SOL")
            return False
        if creator_sol > self.filters['max_creator_sol']:
            self._log_filter("creator_buy_high", f"{creator_sol:.1f} SOL")
            return False
        
        # NEW: Creator spam check
        creator_passed, creator_token_count, creator_reason = self._check_creator_spam(creator_address)
        if not creator_passed:
            self._log_filter("creator_spam", creator_reason)
            return False
        
        # Name quality
        name = str(token_data.get('name', '')).strip()
        symbol = str(token_data.get('symbol', '')).strip()
        
        if len(name) < self.filters['min_name_length']:
            self._log_filter("name_short", name)
            return False
        
        if not name.isascii() or not symbol.isascii():
            self._log_filter("non_ascii", f"{name}/{symbol}")
            return False
        
        # Check ENHANCED blacklist (now includes rug keywords)
        name_lower = name.lower()
        symbol_lower = symbol.lower()
        for blacklisted in self.filters['name_blacklist']:
            if blacklisted in name_lower or blacklisted in symbol_lower:
                self._log_filter("blacklist", f"'{blacklisted}' in {name}")
                return False
        
        # Bonding curve SOL window
        if v_sol < self.filters['min_curve_sol']:
            self._log_filter("curve_low", f"{v_sol:.2f} SOL")
            return False
        if v_sol > self.filters['max_curve_sol']:
            self._log_filter("curve_high", f"{v_sol:.2f} SOL")
            return False
        
        # Momentum check
        if v_sol < 35:
            required_multiplier = 8
        elif v_sol < 50:
            required_multiplier = 5
        else:
            required_multiplier = 3
        
        required_sol = creator_sol * required_multiplier
        if v_sol < required_sol:
            self._log_filter("momentum", f"{v_sol:.2f} vs {required_sol:.2f} needed")
            return False
        
        # Virtual tokens
        v_tokens = float(token_data.get('vTokensInBondingCurve', 0))
        if v_tokens < self.filters['min_v_tokens']:
            self._log_filter("tokens_low", f"{v_tokens:,.0f}")
            return False
        
        # Metadata blacklist
        uri = str(token_data.get('uri', '')).lower()
        description = str(token_data.get('description', '')).lower()
        for blacklisted in self.filters['name_blacklist']:
            if blacklisted in uri or blacklisted in description:
                self._log_filter("metadata_blacklist", blacklisted)
                return False
        
        # Original velocity check
        if not self._check_velocity(mint, v_sol):
            self._log_filter("velocity", "too fast or too young")
            return False
        
        # NEW: Recent velocity check (last 1 second)
        # Only check if we have history (token has been seen before)
        if mint in self.recent_velocity_snapshots and len(self.recent_velocity_snapshots[mint]) >= 2:
            recent_velocity_passed, recent_velocity_value, recent_velocity_reason = self._check_recent_velocity(mint, v_sol)
            if not recent_velocity_passed:
                self._log_filter("recent_velocity_low", recent_velocity_reason)
                return False
        else:
            # First or second time seeing this token, skip recent velocity check
            logger.debug(f"Skipping recent velocity check for {mint[:8]} (not enough snapshots yet)")
            recent_velocity_value = None
        
        # Market cap
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
        
        logger.info(f"✓ MC check passed: ${market_cap:,.0f}")
        
        # Holder check
        holder_result = None
        try:
            logger.info(f"🔍 Starting holder check for {mint[:8]}...")
            await asyncio.sleep(3)
            
            holder_result = await self._check_holders_helius(mint)
            
            if not holder_result['passed']:
                self._log_filter("holder_distribution", holder_result.get('reason', 'unknown'))
                logger.warning(f"❌ Token {mint[:8]}... REJECTED by holder check")
                return False
            
            logger.info(f"✅ Token {mint[:8]}... passed holder check")
            self._last_holder_result = holder_result
            
        except Exception as e:
            logger.error(f"❌ Holder check exception: {e}")
            self._log_filter("holder_distribution", f"exception: {e}")
            return False
        
        # All filters passed
        momentum = v_sol / creator_sol if creator_sol > 0 else 0
        holder_count = holder_result.get('holder_count', 0) if holder_result else 0
        concentration = holder_result.get('concentration', 0) if holder_result else 0
        
        logger.info(f"✅ PASSED ALL FILTERS: {name} ({symbol})")
        logger.info(f"   Creator: {creator_sol:.2f} SOL | Curve: {v_sol:.2f} SOL | MC: ${market_cap:,.0f}")
        logger.info(f"   Momentum: {momentum:.1f}x | Recent velocity: {recent_velocity_value:.2f} SOL/s" if recent_velocity_value else "")
        logger.info(f"   Holders: {holder_count} | Concentration: {concentration:.1f}%")
        return True
        
    async def start(self):
        """Connect to PumpPortal WebSocket"""
        self.running = True
        logger.info("🔍 Connecting to PumpPortal WebSocket...")
        logger.info(f"Strategy: ENHANCED with Recent Velocity Check")
        logger.info(f"  Recent Velocity: ≥{self.filters['min_recent_velocity_sol_per_sec']} SOL/s in last 1s")
        logger.info(f"  Creator Spam: Max {self.filters['max_tokens_per_creator_24h']} tokens/24h")
        logger.info(f"  Enhanced Blacklist: {len(self.filters['name_blacklist'])} keywords")
        
        uri = "wss://pumpportal.fun/api/data"
        
        while self.running:
            try:
                async with websockets.connect(uri) as websocket:
                    logger.info("✅ Connected to PumpPortal WebSocket!")
                    
                    subscribe_msg = {"method": "subscribeNewToken"}
                    await websocket.send(json.dumps(subscribe_msg))
                    logger.info("📡 Subscribed to new token events")
                    
                    while self.running:
                        try:
                            message = await asyncio.wait_for(websocket.recv(), timeout=30)
                            data = json.loads(message)
                            
                            if self._is_new_token(data):
                                mint = self._extract_mint(data)
                                
                                if mint and mint not in self.seen_tokens:
                                    self.seen_tokens.add(mint)
                                    self.tokens_seen += 1
                                    
                                    if not await self._apply_quality_filters(data):
                                        self.tokens_filtered += 1
                                        
                                        if self.tokens_seen % 10 == 0:
                                            filter_rate = (self.tokens_filtered / self.tokens_seen * 100)
                                            logger.info(f"📊 Filter stats: {self.tokens_filtered}/{self.tokens_seen} filtered ({filter_rate:.1f}%)")
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
                                    logger.info("🚀 TOKEN PASSED ALL FILTERS!")
                                    logger.info(f"📜 Mint: {mint}")
                                    logger.info(f"📊 {self.tokens_passed} passed / {self.tokens_filtered} filtered / {self.tokens_seen} total")
                                    logger.info(f"💰 Creator: {creator_sol:.2f} SOL | Curve: {v_sol:.2f} SOL")
                                    logger.info(f"💵 Market Cap: ${market_cap:,.0f}")
                                    logger.info(f"🔥 Momentum: {v_sol/creator_sol if creator_sol > 0 else 0:.1f}x")
                                    logger.info(f"👥 Holders: {holder_data.get('holder_count', 0)} | Concentration: {holder_data.get('concentration', 0):.1f}%")
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
                                            'market_cap': market_cap,
                                            'holder_data': holder_data
                                        })
                        
                        except asyncio.TimeoutError:
                            await websocket.ping()
                        except Exception as e:
                            logger.error(f"Message processing error: {e}")
                            break
                            
            except Exception as e:
                logger.error(f"WebSocket connection error: {e}")
                if self.running:
                    self.seen_tokens.clear()
                    self.reconnect_count += 1
                    logger.info(f"Reconnecting in 5s... (attempt #{self.reconnect_count + 1})")
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
