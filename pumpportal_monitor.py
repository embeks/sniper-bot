
"""
PumpPortal WebSocket Monitor - PROBE-FIRST STRATEGY FIX
CRITICAL CHANGE: Removed 3s sleep + Helius check from monitor
- Tokens now pass to main.py immediately after basic filters
- Helius check moved to main.py AFTER probe entry
- This reduces detection-to-probe time from 19s → 4-5s
"""

import asyncio
import json
import logging
import time
import os
import random
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
        
        # Verify Helius API key (still needed for main.py)
        if not HELIUS_API_KEY:
            logger.error("❌ CRITICAL: HELIUS_API_KEY not found!")
            raise ValueError("HELIUS_API_KEY is required for holder checks")
        else:
            logger.info(f"✅ Helius API key loaded: {HELIUS_API_KEY[:10]}...")
        
        # Token velocity tracking
        self.token_history = {}
        self.filter_reasons = {}
        
        # Recent velocity snapshots (high-frequency during first 3s)
        self.recent_velocity_snapshots = {}
        
        # OPTIMIZATION: First-sighting cooldown with token data storage
        self.first_sighting_times = {}
        self.pending_tokens = {}  # Store token data for re-evaluation
        
        # Creator spam tracking
        self.creator_token_launches = {}
        
        # Token tracking for age
        self.token_first_seen = {}
        self.token_mc_history = {}
        
        # OPTIMIZATION: Cache decimals/metadata for 5s
        self.token_metadata_cache = {}
        
        # SOL price caching
        self.sol_price_usd = 250
        self.last_sol_price_update = 0
        
        # Filters - OPTIMIZED ORDER
        self.filters = {
            # CRITICAL: Age check FIRST (before expensive RPC calls)
            'max_token_age_seconds': 4.0,  # Only process tokens <4s old
            
            # OPTIMIZATION: Early curve prefilter
            'min_curve_sol_prefilter': 3.0,  # Skip tokens with <3 SOL (likely duds)
            
            # FIX #2: Lowered from 0.1 to 0.095 to handle rounding edge cases
            'min_creator_sol': 0.095,  # Allow 0.095-0.099 SOL range
            'max_creator_sol': 5.0,
            'min_curve_sol': 15.0,
            'max_curve_sol': 45.0,
            'min_v_tokens': 500_000_000,
            'min_name_length': 3,
            'min_holders': 10,  # NOTE: Not checked here anymore, moved to main.py
            'check_concentration': False,
            'max_top10_concentration': 85,
            'max_velocity_sol_per_sec': 1.5,  # Unused - kept for compatibility
            'min_market_cap': 4000,
            'max_market_cap': 35000,
            'max_token_age_minutes': 8,  # Unused - kept for compatibility
            'name_blacklist': [
                'test', 'rug', 'airdrop', 'claim', 'scam', 'fake',
                'stealth', 'fair', 'liquidity', 'burned', 'renounced', 'safu', 
                'dev', 'team', 'official',
                'pepe', 'elon', 'trump', 'inu', 'doge', 'shib', 'floki',
                'moon', 'safe', 'baby', 'mini', 'rocket', 'gem'
            ],
            # FIXED: Match velocity_checker.py settings (3.0 SOL/s minimum)
            'min_recent_velocity_sol_per_sec': 3.0,
            'max_tokens_per_creator_24h': 3,
            
            # OPTIMIZATION: First-sighting cooldown
            'first_sighting_cooldown_seconds': 0.5,
            
            'filters_enabled': True
        }
        
        # Statistics
        self.tokens_evaluated = 0  # Total tokens evaluated
        self.tokens_deferred = 0  # ADDED: Tokens in cooldown (not filtered)
        self.tokens_filtered = 0  # Actually filtered out
        self.tokens_passed = 0  # Passed all filters
    
    async def _get_sol_price(self) -> float:
        """Get current SOL price with caching (5 min cache)"""
        if time.time() - self.last_sol_price_update < 300:
            return self.sol_price_usd
        
        try:
            birdeye_key = os.getenv('BIRDEYE_API_KEY', '')
            if not birdeye_key:
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
        except Exception as e:
            logger.debug(f"SOL price fetch failed: {e}")
        
        return self.sol_price_usd
    
    def _event_age_seconds(self, token_data: dict) -> float:
        """
        CRITICAL FIX: Get REAL token age from event data, not first sighting time
        This prevents bypassing age checks on tokens that are already old
        """
        now = time.time()
        
        # Prefer explicit age if provided
        age = token_data.get('age')
        if isinstance(age, (int, float)) and age >= 0:
            return float(age)
        
        # Check common timestamp fields
        for key in ('blockTime', 'createdAt', 'ts', 'timestamp'):
            if key in token_data:
                ts = token_data[key]
                # Normalize: ms to seconds if needed
                ts = ts / 1000.0 if ts > 1e12 else ts
                return max(0.0, now - float(ts))
        
        # Fallback: use first sighting time (less accurate)
        mint = token_data.get('mint') or token_data.get('address', '')
        if mint:
            if mint not in self.token_first_seen:
                self.token_first_seen[mint] = now
            return now - self.token_first_seen[mint]
        
        return 0.0
    
    def _calculate_market_cap(self, token_data: dict) -> float:
        """Calculate market cap from bonding curve data"""
        try:
            v_sol = float(token_data.get('vSolInBondingCurve', 0))
            v_tokens = float(token_data.get('vTokensInBondingCurve', 0))
            
            if v_sol == 0 or v_tokens == 0:
                return 0
            
            price_sol = v_sol / v_tokens
            total_supply = 1_000_000_000
            market_cap_usd = total_supply * price_sol * self.sol_price_usd
            
            return market_cap_usd
        except Exception as e:
            logger.error(f"MC calculation error: {e}")
            return 0
    
    def _store_recent_velocity_snapshot(self, mint: str, sol_raised: float):
        """OPTIMIZED: Store snapshot with high frequency during first 3s"""
        now = time.time()
        
        if mint not in self.recent_velocity_snapshots:
            self.recent_velocity_snapshots[mint] = []
        
        self.recent_velocity_snapshots[mint].append({
            'timestamp': now,
            'sol_raised': sol_raised
        })
        
        # Keep only last 10 snapshots (~3 seconds of history)
        if len(self.recent_velocity_snapshots[mint]) > 10:
            self.recent_velocity_snapshots[mint] = self.recent_velocity_snapshots[mint][-10:]
    
    def _check_creator_spam(self, creator_address: str) -> tuple:
        """Check if creator is spamming tokens"""
        try:
            now = time.time()
            
            # Use setdefault to avoid race conditions
            self.creator_token_launches.setdefault(creator_address, [])
            
            # Clean up old entries (> 24h)
            self.creator_token_launches[creator_address] = [
                ts for ts in self.creator_token_launches[creator_address]
                if now - ts < 86400  # 24 hours
            ]
            
            # Check how many tokens this creator launched in 24h
            token_count = len(self.creator_token_launches[creator_address])
            max_allowed = self.filters['max_tokens_per_creator_24h']
            
            if token_count >= max_allowed:
                logger.info(
                    f"❌ CREATOR SPAM: {creator_address[:8]}... launched "
                    f"{token_count} tokens in 24h (max {max_allowed})"
                )
                return (False, token_count, f"creator_spam: {token_count}_tokens")
            
            # Record this token launch
            self.creator_token_launches[creator_address].append(now)
            
            return (True, token_count + 1, "ok")
            
        except Exception as e:
            logger.error(f"Error checking creator spam: {e}")
            return (True, 0, f"error: {e}")
    
    async def _check_holders_helius(self, mint: str, max_retries: int = 2) -> dict:
        """
        Helius holder check - NOW ONLY USED BY MAIN.PY
        This method is kept in the class so main.py can access it via scanner instance
        """
        retry_delays = [2.5, 2.5]
        
        for attempt in range(max_retries + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
                    
                    payload = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getTokenLargestAccounts",
                        "params": [mint]
                    }
                    
                    timeout = aiohttp.ClientTimeout(total=0.8)
                    
                    try:
                        async with session.post(url, json=payload, timeout=timeout) as resp:
                            if resp.status != 200:
                                logger.warning(f"Helius API returned {resp.status} for {mint[:8]}... (attempt {attempt + 1}/{max_retries + 1})")
                                if attempt < max_retries:
                                    retry_delay = retry_delays[attempt] + random.uniform(0, 0.5)
                                    logger.info(f"⏳ Waiting {retry_delay:.1f}s and retrying...")
                                    await asyncio.sleep(retry_delay)
                                    continue
                                return {'passed': False, 'reason': f'HTTP {resp.status}'}
                            
                            data = await resp.json()
                            
                            if 'error' in data:
                                error_msg = data['error'].get('message', '')
                                
                                if 'not a Token mint' in error_msg and attempt < max_retries:
                                    retry_delay = retry_delays[attempt] + random.uniform(0, 0.5)
                                    logger.info(f"⏳ Token {mint[:8]}... not indexed yet (attempt {attempt + 1}/{max_retries + 1}). Waiting {retry_delay:.1f}s and retrying...")
                                    await asyncio.sleep(retry_delay)
                                    continue
                                
                                logger.warning(f"Helius API error for {mint[:8]}...: {error_msg}")
                                return {'passed': False, 'reason': error_msg}
                            
                            if 'result' not in data or 'value' not in data['result']:
                                logger.warning(f"Malformed response for {mint[:8]}... (attempt {attempt + 1}/{max_retries + 1})")
                                if attempt < max_retries:
                                    retry_delay = retry_delays[attempt] + random.uniform(0, 0.5)
                                    await asyncio.sleep(retry_delay)
                                    continue
                                return {'passed': False, 'reason': 'malformed response'}
                            
                            accounts = data['result']['value']
                            account_count = len(accounts)
                            
                            if account_count < self.filters['min_holders']:
                                return {
                                    'passed': False,
                                    'holder_count': account_count,
                                    'reason': f'only {account_count} holders'
                                }
                            
                            total_supply = sum(float(acc.get('amount', 0)) for acc in accounts)
                            if total_supply == 0:
                                return {'passed': False, 'reason': 'zero supply'}
                            
                            top_10_count = min(10, account_count)
                            top_10_supply = sum(float(acc.get('amount', 0)) for acc in accounts[:top_10_count])
                            concentration = (top_10_supply / total_supply * 100)
                            
                            if self.filters.get('check_concentration', False):
                                if concentration > self.filters['max_top10_concentration']:
                                    return {
                                        'passed': False,
                                        'holder_count': account_count,
                                        'concentration': concentration,
                                        'reason': f'concentration {concentration:.1f}%'
                                    }
                            
                            logger.debug(f"✅ Token {mint[:8]}... has {account_count} holders (attempt {attempt + 1})")
                            return {
                                'passed': True,
                                'holder_count': account_count,
                                'concentration': concentration
                            }
                    
                    except asyncio.TimeoutError:
                        logger.warning(f"Timeout for {mint[:8]}... (attempt {attempt + 1}/{max_retries + 1})")
                        if attempt < max_retries:
                            retry_delay = retry_delays[attempt] + random.uniform(0, 0.5)
                            logger.info(f"⏳ Waiting {retry_delay:.1f}s and retrying...")
                            await asyncio.sleep(retry_delay)
                            continue
                        return {'passed': False, 'reason': 'holder_timeout'}
                        
            except Exception as e:
                logger.error(f"Helius exception for {mint[:8]}...: {e}")
                if attempt < max_retries:
                    retry_delay = retry_delays[attempt] + random.uniform(0, 0.5)
                    await asyncio.sleep(retry_delay)
                    continue
                return {'passed': False, 'reason': str(e)}
        
        logger.warning(f"❌ Failed to get holder count for {mint[:8]}... after {max_retries + 1} attempts")
        return {'passed': False, 'reason': f'failed_after_{max_retries + 1}_attempts'}
    
    def _log_filter(self, reason: str, detail: str):
        """Track why tokens are filtered"""
        if reason not in self.filter_reasons:
            self.filter_reasons[reason] = 0
        self.filter_reasons[reason] += 1
        logger.info(f"❌ Filtered ({reason}): {detail}")
    
    async def _process_pending_tokens(self):
        """
        CRITICAL FIX: Re-evaluate tokens after cooldown expires
        This background task checks pending tokens and re-processes them
        """
        while self.running:
            try:
                await asyncio.sleep(0.1)  # Check every 100ms
                
                now = time.time()
                tokens_to_process = []
                
                # Find tokens ready for re-evaluation
                for mint, stored_data in list(self.pending_tokens.items()):
                    first_sight_time = self.first_sighting_times.get(mint, now)
                    time_since_first = now - first_sight_time
                    
                    if time_since_first >= self.filters['first_sighting_cooldown_seconds']:
                        tokens_to_process.append((mint, stored_data))
                        del self.pending_tokens[mint]
                
                # Re-evaluate tokens that passed cooldown
                for mint, data in tokens_to_process:
                    logger.info(f"⏰ Cooldown complete for {mint[:8]}... - re-evaluating")
                    passed, token_age = await self._apply_quality_filters_post_cooldown(data)
                    
                    if not passed:
                        self.tokens_filtered += 1
                        continue
                    
                    # Token passed all filters!
                    if mint in self.seen_tokens:
                        continue
                    
                    self.seen_tokens.add(mint)
                    self.tokens_passed += 1
                    
                    token_data = data.get('data', data)
                    v_sol = token_data.get('vSolInBondingCurve', 0)
                    creator_sol = token_data.get('solAmount', 0)
                    market_cap = self._calculate_market_cap(token_data)
                    
                    logger.info("=" * 60)
                    logger.info("🚀 TOKEN PASSED MONITOR FILTERS - SENDING TO MAIN.PY")
                    logger.info(f"📜 Mint: {mint}")
                    logger.info(f"📊 {self.tokens_passed} passed / {self.tokens_filtered} filtered / {self.tokens_evaluated} total")
                    logger.info(f"💰 Creator: {creator_sol:.2f} SOL | Curve: {v_sol:.2f} SOL")
                    logger.info(f"💵 Market Cap: ${market_cap:,.0f}")
                    logger.info(f"🔥 Momentum: {v_sol/creator_sol if creator_sol > 0 else 0:.1f}x")
                    logger.info(f"⏱️ Token age: {token_age:.1f}s")
                    logger.info(f"📝 {token_data.get('name', 'Unknown')} ({token_data.get('symbol', 'Unknown')})")
                    logger.info(f"⚡ NOTE: Helius check will happen in main.py AFTER probe")
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
                            'age': token_age,
                            'token_age': token_age,
                            'sol_raised_at_detection': v_sol
                        })
                
            except Exception as e:
                logger.error(f"Error in pending tokens processor: {e}")
    
    async def _apply_quality_filters(self, data: dict) -> bool:
        """
        OPTIMIZED FILTER ORDER - FIRST PASS ONLY:
        1. Age check (before expensive RPC)
        2. Curve prefilter (skip obvious duds)
        3. First-sighting cooldown (0.5s confirmation) - STORE TOKEN FOR RE-EVAL
        """
        if not self.filters['filters_enabled']:
            return True
            
        token_data = data.get('data', data)
        mint = self._extract_mint(data)
        
        now = time.time()
        v_sol = float(token_data.get('vSolInBondingCurve', 0))
        
        # Use REAL token age from event
        token_age = self._event_age_seconds(token_data)
        
        if token_age > self.filters['max_token_age_seconds']:
            self._log_filter("too_old_prefilter", f"{token_age:.1f}s > {self.filters['max_token_age_seconds']}s")
            return False
        
        # Curve prefilter
        if v_sol < self.filters['min_curve_sol_prefilter']:
            self._log_filter("low_curve_prefilter", f"{v_sol:.2f} SOL < {self.filters['min_curve_sol_prefilter']}")
            return False
        
        # First-sighting cooldown
        if mint not in self.first_sighting_times:
            self.first_sighting_times[mint] = now
            self.pending_tokens[mint] = data
            self._store_recent_velocity_snapshot(mint, v_sol)
            self.tokens_deferred += 1
            logger.info(f"📊 FIRST SIGHTING: {mint[:8]}... (age {token_age:.1f}s, {v_sol:.1f} SOL) - waiting {self.filters['first_sighting_cooldown_seconds']}s")
            return False
        
        return False
    
    async def _apply_quality_filters_post_cooldown(self, data: dict) -> tuple:
        """
        🚀 PROBE-FIRST FIX: NO MORE 3S SLEEP OR HELIUS CHECK
        
        This method now ONLY does basic filters:
        - Creator buy size
        - Name quality
        - Curve range
        - Momentum
        
        Helius holder check is moved to main.py AFTER probe entry
        
        Returns (passed: bool, token_age: float)
        """
        token_data = data.get('data', data)
        mint = self._extract_mint(data)
        
        now = time.time()
        v_sol = float(token_data.get('vSolInBondingCurve', 0))
        
        # Re-check age (token is now older)
        token_age = self._event_age_seconds(token_data)
        
        # Continue storing velocity snapshots
        self._store_recent_velocity_snapshot(mint, v_sol)
        
        logger.info(f"✓ Token {mint[:8]}... passed cooldown: {token_age:.1f}s old, {v_sol:.1f} SOL")
        
        # Basic filters
        creator_sol = float(token_data.get('solAmount', 0))
        creator_address = str(token_data.get('traderPublicKey', 'unknown'))
        
        # Creator buy size check
        if creator_sol < self.filters['min_creator_sol'] or creator_sol > self.filters['max_creator_sol']:
            self._log_filter("creator_buy", f"{creator_sol:.3f} SOL")
            return (False, token_age)
        
        # Creator spam check
        creator_passed, _, _ = self._check_creator_spam(creator_address)
        if not creator_passed:
            self._log_filter("creator_spam", "max tokens/24h")
            return (False, token_age)
        
        # Name quality
        name = str(token_data.get('name', '')).strip()
        symbol = str(token_data.get('symbol', '')).strip()
        
        if len(name) < self.filters['min_name_length'] or not name.isascii() or not symbol.isascii():
            self._log_filter("name_quality", f"{name}")
            return (False, token_age)
        
        # Blacklist check
        name_lower = name.lower()
        symbol_lower = symbol.lower()
        for blacklisted in self.filters['name_blacklist']:
            if blacklisted in name_lower or blacklisted in symbol_lower:
                self._log_filter("blacklist", f"'{blacklisted}' in {name}")
                return (False, token_age)
        
        # Bonding curve SOL range
        if v_sol < self.filters['min_curve_sol'] or v_sol > self.filters['max_curve_sol']:
            self._log_filter("curve_range", f"{v_sol:.2f} SOL")
            return (False, token_age)
        
        # Momentum check
        required_multiplier = 8 if v_sol < 35 else (5 if v_sol < 50 else 3)
        required_sol = creator_sol * required_multiplier
        if v_sol < required_sol:
            self._log_filter("momentum", f"{v_sol:.2f} vs {required_sol:.2f}")
            return (False, token_age)
        
        # Virtual tokens
        v_tokens = float(token_data.get('vTokensInBondingCurve', 0))
        if v_tokens < self.filters['min_v_tokens']:
            self._log_filter("tokens_low", f"{v_tokens:,.0f}")
            return (False, token_age)
        
        # Market cap check (fast, local calculation)
        await self._get_sol_price()
        market_cap = self._calculate_market_cap(token_data)
        
        if market_cap < self.filters['min_market_cap'] or market_cap > self.filters['max_market_cap']:
            self._log_filter("mc_range", f"${market_cap:,.0f}")
            return (False, token_age)
        
        # ===================================================================
        # 🚀 CRITICAL CHANGE: NO MORE 3S SLEEP OR HELIUS CHECK HERE!
        # Token passes immediately to main.py for probe entry
        # Helius check will happen AFTER probe, BEFORE confirm
        # ===================================================================
        
        momentum = v_sol / creator_sol if creator_sol > 0 else 0
        
        logger.info(f"✅ PASSED BASIC FILTERS (NO HELIUS YET): {name} ({symbol})")
        logger.info(f"   Creator: {creator_sol:.2f} SOL | Curve: {v_sol:.2f} SOL | MC: ${market_cap:,.0f}")
        logger.info(f"   Momentum: {momentum:.1f}x | Token age: {token_age:.1f}s")
        logger.info(f"   ⚡ Helius check will happen in main.py after probe")
        
        return (True, token_age)
        
    async def start(self):
        """Connect to PumpPortal WebSocket"""
        self.running = True
        logger.info("🔍 Connecting to PumpPortal WebSocket...")
        logger.info(f"Strategy: PROBE-FIRST (NO HELIUS IN MONITOR)")
        logger.info(f"  ✅ Age check: <{self.filters['max_token_age_seconds']}s")
        logger.info(f"  ✅ Curve prefilter: ≥{self.filters['min_curve_sol_prefilter']} SOL")
        logger.info(f"  ✅ First-sighting cooldown: {self.filters['first_sighting_cooldown_seconds']}s")
        logger.info(f"  ✅ Basic filters: creator, name, curve, momentum, MC")
        logger.info(f"  🚀 REMOVED: 3s sleep before Helius")
        logger.info(f"  🚀 REMOVED: Helius holder check (moved to main.py)")
        logger.info(f"  ⚡ Result: Tokens pass to probe in ~1-2s instead of 10-12s")
        
        uri = "wss://pumpportal.fun/api/data"
        
        # Start background task for re-evaluating pending tokens
        pending_task = asyncio.create_task(self._process_pending_tokens())
        
        while self.running:
            try:
                async with websockets.connect(
                    uri,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5
                ) as websocket:
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
                                
                                if not mint:
                                    continue
                                
                                self.tokens_evaluated += 1
                                
                                passed = await self._apply_quality_filters(data)
                                
                                if not passed:
                                    if self.tokens_evaluated % 10 == 0:
                                        filter_rate = (self.tokens_filtered / self.tokens_evaluated * 100) if self.tokens_evaluated > 0 else 0
                                        logger.info(f"📊 Filter stats: {self.tokens_passed} passed / {self.tokens_filtered} filtered / {self.tokens_deferred} deferred / {self.tokens_evaluated} evaluated ({filter_rate:.1f}% filtered)")
                                        if self.filter_reasons:
                                            top_reasons = sorted(self.filter_reasons.items(), key=lambda x: x[1], reverse=True)[:3]
                                            logger.info(f"   Top reasons: {top_reasons}")
                                    continue
                        
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
        
        # Cleanup
        pending_task.cancel()
    
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
            'tokens_evaluated': self.tokens_evaluated,
            'tokens_passed': self.tokens_passed,
            'tokens_filtered': self.tokens_filtered,
            'tokens_deferred': self.tokens_deferred,
            'filter_rate': (self.tokens_filtered / self.tokens_evaluated * 100) if self.tokens_evaluated > 0 else 0,
            'filter_reasons': self.filter_reasons
        }
    
    def stop(self):
        self.running = False
        stats = self.get_stats()
        logger.info(f"PumpPortal monitor stopped")
        logger.info(f"Stats: {stats['tokens_passed']} passed / {stats['tokens_filtered']} filtered / {stats['tokens_deferred']} deferred / {stats['tokens_evaluated']} evaluated ({stats['filter_rate']:.1f}% filtered)")
        if self.filter_reasons:
            logger.info(f"Filter breakdown: {self.filter_reasons}")
