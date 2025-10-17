"""
PumpPortal WebSocket Monitor - LATENCY OPTIMIZED with FAST PATH
✅ Fast path for tokens <10s old (skip holder checks)
✅ Stricter velocity gate on fast path (1.5x = 3.0 SOL/s)
✅ All previous fixes maintained
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
from config import HELIUS_API_KEY, FAST_PATH_ENABLED, FAST_PATH_MAX_AGE, FAST_PATH_VELOCITY_MULT

logger = logging.getLogger(__name__)

class PumpPortalMonitor:
    def __init__(self, callback):
        self.callback = callback
        self.running = False
        self.seen_tokens = set()
        self.reconnect_count = 0
        
        if not HELIUS_API_KEY:
            logger.error("❌ CRITICAL: HELIUS_API_KEY not found!")
            raise ValueError("HELIUS_API_KEY is required for holder checks")
        else:
            logger.info(f"✅ Helius API key loaded: {HELIUS_API_KEY[:10]}...")
        
        self.token_history = {}
        self.filter_reasons = {}
        self.recent_velocity_snapshots = {}
        self.first_sighting_times = {}
        self.pending_tokens = {}
        self.creator_token_launches = {}
        self.token_first_seen = {}
        self.token_mc_history = {}
        self.token_metadata_cache = {}
        
        self.sol_price_usd = 250
        self.last_sol_price_update = 0
        
        self.filters = {
            'max_token_age_seconds': 4.0,
            'min_curve_sol_prefilter': 3.0,
            'min_creator_sol': 0.095,
            'max_creator_sol': 5.0,
            'min_curve_sol': 15.0,
            'max_curve_sol': 45.0,
            'min_v_tokens': 500_000_000,
            'min_name_length': 3,
            'min_holders': 10,
            'check_concentration': False,
            'max_top10_concentration': 85,
            'max_velocity_sol_per_sec': 1.5,
            'min_market_cap': 4000,
            'max_market_cap': 35000,
            'max_token_age_minutes': 8,
            'name_blacklist': [
                'test', 'rug', 'airdrop', 'claim', 'scam', 'fake',
                'stealth', 'fair', 'liquidity', 'burned', 'renounced', 'safu', 
                'dev', 'team', 'official',
                'pepe', 'elon', 'trump', 'inu', 'doge', 'shib', 'floki',
                'moon', 'safe', 'baby', 'mini', 'rocket', 'gem'
            ],
            'min_recent_velocity_sol_per_sec': 3.0,
            'max_tokens_per_creator_24h': 3,
            'first_sighting_cooldown_seconds': 0.0,  # FIX #2: Changed from 0.5 to 0.0
            'filters_enabled': True
        }
        
        self.tokens_evaluated = 0
        self.tokens_deferred = 0
        self.tokens_filtered = 0
        self.tokens_passed = 0
        self.tokens_fast_path = 0  # New counter for fast path
    
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
        """Get REAL token age from event data"""
        now = time.time()
        
        age = token_data.get('age')
        if isinstance(age, (int, float)) and age >= 0:
            return float(age)
        
        for key in ('blockTime', 'createdAt', 'ts', 'timestamp'):
            if key in token_data:
                ts = token_data[key]
                ts = ts / 1000.0 if ts > 1e12 else ts
                return max(0.0, now - float(ts))
        
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
        """Store snapshot with high frequency during first 3s"""
        now = time.time()
        
        if mint not in self.recent_velocity_snapshots:
            self.recent_velocity_snapshots[mint] = []
        
        self.recent_velocity_snapshots[mint].append({
            'timestamp': now,
            'sol_raised': sol_raised
        })
        
        if len(self.recent_velocity_snapshots[mint]) > 10:
            self.recent_velocity_snapshots[mint] = self.recent_velocity_snapshots[mint][-10:]
    
    def _check_recent_velocity(self, mint: str, current_sol: float) -> tuple:
        """Check velocity in LAST 1 SECOND with adaptive window"""
        try:
            if mint not in self.recent_velocity_snapshots or len(self.recent_velocity_snapshots[mint]) < 2:
                return (True, None, "insufficient_history")
            
            history = self.recent_velocity_snapshots[mint]
            now = time.time()
            
            first_snapshot_time = history[0]['timestamp']
            time_elapsed = now - first_snapshot_time
            
            if time_elapsed < 0.9:
                logger.debug(f"Velocity check deferred for {mint[:8]}... (only {time_elapsed:.2f}s of history)")
                return (True, None, f"deferred_young_token: {time_elapsed:.2f}s")
            
            lookback_window = min(1.0, time_elapsed - 0.1)
            target_time = now - lookback_window
            
            closest_snapshot = None
            min_time_diff = float('inf')
            
            for snap in history[:-1]:
                time_diff = abs(snap['timestamp'] - target_time)
                if time_diff < min_time_diff:
                    min_time_diff = time_diff
                    closest_snapshot = snap
            
            if closest_snapshot and min_time_diff < 2.0:
                time_delta = now - closest_snapshot['timestamp']
                sol_delta = current_sol - closest_snapshot['sol_raised']
                
                if time_delta > 0:
                    recent_velocity = max(0, sol_delta / time_delta)
                    min_required = self.filters['min_recent_velocity_sol_per_sec']
                    
                    if recent_velocity < min_required:
                        return (False, recent_velocity, f"recent_velocity_low: {recent_velocity:.2f} SOL/s over {time_delta:.2f}s")
                    
                    logger.debug(f"Velocity check passed for {mint[:8]}...: {recent_velocity:.2f} SOL/s over {time_delta:.2f}s")
                    return (True, recent_velocity, "ok")
            
            return (True, None, "insufficient_time")
            
        except Exception as e:
            logger.error(f"Error checking recent velocity: {e}")
            return (True, None, f"error: {e}")
    
    def _check_creator_spam(self, creator_address: str) -> tuple:
        """Check if creator is spamming tokens"""
        try:
            now = time.time()
            
            self.creator_token_launches.setdefault(creator_address, [])
            
            self.creator_token_launches[creator_address] = [
                ts for ts in self.creator_token_launches[creator_address]
                if now - ts < 86400
            ]
            
            token_count = len(self.creator_token_launches[creator_address])
            max_allowed = self.filters['max_tokens_per_creator_24h']
            
            if token_count >= max_allowed:
                logger.info(
                    f"❌ CREATOR SPAM: {creator_address[:8]}... launched "
                    f"{token_count} tokens in 24h (max {max_allowed})"
                )
                return (False, token_count, f"creator_spam: {token_count}_tokens")
            
            self.creator_token_launches[creator_address].append(now)
            
            return (True, token_count + 1, "ok")
            
        except Exception as e:
            logger.error(f"Error checking creator spam: {e}")
            return (True, 0, f"error: {e}")
    
    async def _check_holders_helius(self, mint: str, max_retries: int = 2) -> dict:
        """Fast-fail RPC with 0.8s timeout + 2 retries"""
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
        """Re-evaluate tokens after cooldown expires"""
        while self.running:
            try:
                await asyncio.sleep(0.1)
                
                now = time.time()
                tokens_to_process = []
                
                for mint, stored_data in list(self.pending_tokens.items()):
                    first_sight_time = self.first_sighting_times.get(mint, now)
                    time_since_first = now - first_sight_time
                    
                    if time_since_first >= self.filters['first_sighting_cooldown_seconds']:
                        tokens_to_process.append((mint, stored_data))
                        del self.pending_tokens[mint]
                
                for mint, data in tokens_to_process:
                    logger.info(f"⏰ Cooldown complete for {mint[:8]}... - re-evaluating")
                    passed, token_age = await self._apply_quality_filters_post_cooldown(data)
                    
                    if not passed:
                        self.tokens_filtered += 1
                        continue
                    
                    if mint in self.seen_tokens:
                        continue
                    
                    self.seen_tokens.add(mint)
                    self.tokens_passed += 1
                    
                    token_data = data.get('data', data)
                    v_sol = token_data.get('vSolInBondingCurve', 0)
                    creator_sol = token_data.get('solAmount', 0)
                    market_cap = self._calculate_market_cap(token_data)
                    
                    holder_data = getattr(self, '_last_holder_result', {})
                    
                    logger.info("=" * 60)
                    logger.info("🚀 TOKEN PASSED ALL FILTERS!")
                    logger.info(f"📜 Mint: {mint}")
                    logger.info(f"📊 {self.tokens_passed} passed / {self.tokens_filtered} filtered / {self.tokens_evaluated} total")
                    logger.info(f"💰 Creator: {creator_sol:.2f} SOL | Curve: {v_sol:.2f} SOL")
                    logger.info(f"💵 Market Cap: ${market_cap:,.0f}")
                    logger.info(f"🔥 Momentum: {v_sol/creator_sol if creator_sol > 0 else 0:.1f}x")
                    logger.info(f"👥 Holders: {holder_data.get('holder_count', 0)} | Concentration: {holder_data.get('concentration', 0):.1f}%")
                    logger.info(f"⏱️ Token age: {token_age:.1f}s")
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
                            'holder_data': holder_data,
                            'age': token_age,
                            'token_age': token_age,
                            'sol_raised_at_detection': v_sol
                        })
                
            except Exception as e:
                logger.error(f"Error in pending tokens processor: {e}")
    
    async def _apply_quality_filters(self, data: dict) -> bool:
        """FIRST PASS: Age + Curve + Cooldown checks"""
        if not self.filters['filters_enabled']:
            return True
            
        token_data = data.get('data', data)
        mint = self._extract_mint(data)
        
        now = time.time()
        v_sol = float(token_data.get('vSolInBondingCurve', 0))
        
        token_age = self._event_age_seconds(token_data)
        
        if token_age > self.filters['max_token_age_seconds']:
            self._log_filter("too_old_prefilter", f"{token_age:.1f}s > {self.filters['max_token_age_seconds']}s")
            return False
        
        if v_sol < self.filters['min_curve_sol_prefilter']:
            self._log_filter("low_curve_prefilter", f"{v_sol:.2f} SOL < {self.filters['min_curve_sol_prefilter']}")
            return False
        
        # ===================================================================
        # LATENCY OPTIMIZATION: FAST PATH FOR ULTRA-YOUNG TOKENS
        # Skip holder checks for tokens <15s old with stricter gates
        # FIX #3: Changed minimum age from 1.0s to 0.5s
        # ===================================================================
        if FAST_PATH_ENABLED and token_age < FAST_PATH_MAX_AGE and token_age >= 0.5:
            logger.info(f"⚡ FAST PATH activated for {mint[:8]}... (age: {token_age:.1f}s)")
            self.tokens_fast_path += 1
            
            # FIX: If token is very young, wait for RPC to index
            if token_age < 2.0:
                wait_time = 2.0 - token_age
                logger.info(f"⏳ Token very young ({token_age:.1f}s), waiting {wait_time:.1f}s for RPC indexing...")
                await asyncio.sleep(wait_time)
                # Update age after wait
                token_age = self._event_age_seconds(data)
                logger.info(f"✅ Resuming fast path (token now {token_age:.1f}s old)")
            
            # Skip cooldown and holder checks, but apply stricter velocity
            passed, fast_path_age = await self._apply_fast_path_filters(data)
            
            if passed:
                if mint in self.seen_tokens:
                    return False
                
                self.seen_tokens.add(mint)
                self.tokens_passed += 1
                
                token_data = data.get('data', data)
                v_sol = token_data.get('vSolInBondingCurve', 0)
                creator_sol = token_data.get('solAmount', 0)
                market_cap = self._calculate_market_cap(token_data)
                
                logger.info("=" * 60)
                logger.info("⚡ FAST PATH TOKEN PASSED!")
                logger.info(f"📜 Mint: {mint}")
                logger.info(f"📊 {self.tokens_passed} passed / {self.tokens_filtered} filtered / {self.tokens_fast_path} fast path")
                logger.info(f"💰 Creator: {creator_sol:.2f} SOL | Curve: {v_sol:.2f} SOL")
                logger.info(f"💵 Market Cap: ${market_cap:,.0f}")
                logger.info(f"🔥 Momentum: {v_sol/creator_sol if creator_sol > 0 else 0:.1f}x")
                logger.info(f"⏱️ Token age: {fast_path_age:.1f}s")
                logger.info(f"⚡ Bypassed holder checks (age <{FAST_PATH_MAX_AGE}s)")
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
                        'fast_path': True,
                        'market_cap': market_cap,
                        'holder_data': {'holder_count': 0, 'concentration': 0, 'fast_path': True},
                        'age': fast_path_age,
                        'token_age': fast_path_age,
                        'sol_raised_at_detection': v_sol
                    })
                
                return True
            else:
                self.tokens_filtered += 1
                return False
        
        # Normal path: Use cooldown
        if mint not in self.first_sighting_times:
            self.first_sighting_times[mint] = now
            self.pending_tokens[mint] = data
            self._store_recent_velocity_snapshot(mint, v_sol)
            self.tokens_deferred += 1
            logger.info(f"📊 FIRST SIGHTING: {mint[:8]}... (age {token_age:.1f}s, {v_sol:.1f} SOL) - waiting {self.filters['first_sighting_cooldown_seconds']}s")
            return False
        
        return False
    
    async def _apply_fast_path_filters(self, data: dict) -> tuple:
        """
        FAST PATH: Stricter gates, no holder checks
        Returns (passed: bool, token_age: float)
        """
        token_data = data.get('data', data)
        mint = self._extract_mint(data)
        
        token_age = self._event_age_seconds(token_data)
        v_sol = float(token_data.get('vSolInBondingCurve', 0))
        
        self._store_recent_velocity_snapshot(mint, v_sol)
        
        logger.info(f"⚡ Fast path evaluation for {mint[:8]}... (age: {token_age:.1f}s)")
        
        # Basic filters
        creator_sol = float(token_data.get('solAmount', 0))
        creator_address = str(token_data.get('traderPublicKey', 'unknown'))
        
        if creator_sol < self.filters['min_creator_sol'] or creator_sol > self.filters['max_creator_sol']:
            self._log_filter("fast_path_creator_buy", f"{creator_sol:.3f} SOL")
            return (False, token_age)
        
        creator_passed, _, _ = self._check_creator_spam(creator_address)
        if not creator_passed:
            self._log_filter("fast_path_creator_spam", "max tokens/24h")
            return (False, token_age)
        
        name = str(token_data.get('name', '')).strip()
        symbol = str(token_data.get('symbol', '')).strip()
        
        if len(name) < self.filters['min_name_length'] or not name.isascii() or not symbol.isascii():
            self._log_filter("fast_path_name_quality", f"{name}")
            return (False, token_age)
        
        name_lower = name.lower()
        symbol_lower = symbol.lower()
        for blacklisted in self.filters['name_blacklist']:
            if blacklisted in name_lower or blacklisted in symbol_lower:
                self._log_filter("fast_path_blacklist", f"'{blacklisted}' in {name}")
                return (False, token_age)
        
        if v_sol < self.filters['min_curve_sol'] or v_sol > self.filters['max_curve_sol']:
            self._log_filter("fast_path_curve_range", f"{v_sol:.2f} SOL")
            return (False, token_age)
        
        # Stricter momentum for fast path
        required_multiplier = 10 if v_sol < 35 else (7 if v_sol < 50 else 5)
        required_sol = creator_sol * required_multiplier
        if v_sol < required_sol:
            self._log_filter("fast_path_momentum", f"{v_sol:.2f} vs {required_sol:.2f}")
            return (False, token_age)
        
        v_tokens = float(token_data.get('vTokensInBondingCurve', 0))
        if v_tokens < self.filters['min_v_tokens']:
            self._log_filter("fast_path_tokens_low", f"{v_tokens:,.0f}")
            return (False, token_age)
        
        # Market cap
        await self._get_sol_price()
        market_cap = self._calculate_market_cap(token_data)
        
        if market_cap < self.filters['min_market_cap'] or market_cap > self.filters['max_market_cap']:
            self._log_filter("fast_path_mc_range", f"${market_cap:,.0f}")
            return (False, token_age)
        
        # CRITICAL: Stricter velocity gate for fast path (1.5x normal = 3.0 SOL/s)
        min_velocity_fast_path = self.filters['min_recent_velocity_sol_per_sec'] * FAST_PATH_VELOCITY_MULT
        avg_velocity = v_sol / max(token_age, 0.1)
        
        if avg_velocity < min_velocity_fast_path:
            self._log_filter("fast_path_velocity", f"{avg_velocity:.2f} SOL/s < {min_velocity_fast_path:.2f}")
            return (False, token_age)
        
        logger.info(f"✅ FAST PATH PASSED: {name} ({symbol})")
        logger.info(f"   Creator: {creator_sol:.2f} SOL | Curve: {v_sol:.2f} SOL | MC: ${market_cap:,.0f}")
        logger.info(f"   Velocity: {avg_velocity:.2f} SOL/s (≥{min_velocity_fast_path:.2f} required)")
        logger.info(f"   Token age: {token_age:.1f}s | ⚡ SKIPPED HOLDER CHECKS")
        
        return (True, token_age)
    
    async def _apply_quality_filters_post_cooldown(self, data: dict) -> tuple:
        """SECOND PASS - After cooldown expires (normal path with holder checks)"""
        token_data = data.get('data', data)
        mint = self._extract_mint(data)
        
        now = time.time()
        v_sol = float(token_data.get('vSolInBondingCurve', 0))
        
        token_age = self._event_age_seconds(token_data)
        
        self._store_recent_velocity_snapshot(mint, v_sol)
        
        logger.info(f"✓ Token {mint[:8]}... passed cooldown: {token_age:.1f}s old, {v_sol:.1f} SOL")
        
        creator_sol = float(token_data.get('solAmount', 0))
        creator_address = str(token_data.get('traderPublicKey', 'unknown'))
        
        if creator_sol < self.filters['min_creator_sol'] or creator_sol > self.filters['max_creator_sol']:
            self._log_filter("creator_buy", f"{creator_sol:.3f} SOL")
            return (False, token_age)
        
        creator_passed, _, _ = self._check_creator_spam(creator_address)
        if not creator_passed:
            self._log_filter("creator_spam", "max tokens/24h")
            return (False, token_age)
        
        name = str(token_data.get('name', '')).strip()
        symbol = str(token_data.get('symbol', '')).strip()
        
        if len(name) < self.filters['min_name_length'] or not name.isascii() or not symbol.isascii():
            self._log_filter("name_quality", f"{name}")
            return (False, token_age)
        
        name_lower = name.lower()
        symbol_lower = symbol.lower()
        for blacklisted in self.filters['name_blacklist']:
            if blacklisted in name_lower or blacklisted in symbol_lower:
                self._log_filter("blacklist", f"'{blacklisted}' in {name}")
                return (False, token_age)
        
        if v_sol < self.filters['min_curve_sol'] or v_sol > self.filters['max_curve_sol']:
            self._log_filter("curve_range", f"{v_sol:.2f} SOL")
            return (False, token_age)
        
        required_multiplier = 8 if v_sol < 35 else (5 if v_sol < 50 else 3)
        required_sol = creator_sol * required_multiplier
        if v_sol < required_sol:
            self._log_filter("momentum", f"{v_sol:.2f} vs {required_sol:.2f}")
            return (False, token_age)
        
        v_tokens = float(token_data.get('vTokensInBondingCurve', 0))
        if v_tokens < self.filters['min_v_tokens']:
            self._log_filter("tokens_low", f"{v_tokens:,.0f}")
            return (False, token_age)
        
        logger.info(f"🔍 Running concurrent checks for {mint[:8]}...")
        
        await asyncio.sleep(3)
        
        await self._get_sol_price()
        market_cap = self._calculate_market_cap(token_data)
        
        if market_cap < self.filters['min_market_cap'] or market_cap > self.filters['max_market_cap']:
            self._log_filter("mc_range", f"${market_cap:,.0f}")
            return (False, token_age)
        
        holder_task = asyncio.create_task(self._check_holders_helius(mint))
        holder_result = await holder_task
        
        if not holder_result['passed']:
            self._log_filter("holder_distribution", holder_result.get('reason', 'unknown'))
            return (False, token_age)
        
        momentum = v_sol / creator_sol if creator_sol > 0 else 0
        holder_count = holder_result.get('holder_count', 0)
        concentration = holder_result.get('concentration', 0)
        
        logger.info(f"✅ PASSED ALL FILTERS: {name} ({symbol})")
        logger.info(f"   Creator: {creator_sol:.2f} SOL | Curve: {v_sol:.2f} SOL | MC: ${market_cap:,.0f}")
        logger.info(f"   Momentum: {momentum:.1f}x | Token age: {token_age:.1f}s")
        logger.info(f"   Holders: {holder_count} | Concentration: {concentration:.1f}%")
        
        self._last_holder_result = holder_result
        return (True, token_age)
        
    async def start(self):
        """Connect to PumpPortal WebSocket"""
        self.running = True
        logger.info("🔍 Connecting to PumpPortal WebSocket...")
        logger.info(f"Strategy: LATENCY OPTIMIZED + FAST PATH")
        logger.info(f"  ⚡ Fast path: {'ENABLED' if FAST_PATH_ENABLED else 'DISABLED'}")
        if FAST_PATH_ENABLED:
            logger.info(f"  ⚡ Fast path age: <{FAST_PATH_MAX_AGE}s")
            logger.info(f"  ⚡ Fast path velocity: {self.filters['min_recent_velocity_sol_per_sec'] * FAST_PATH_VELOCITY_MULT:.1f} SOL/s ({FAST_PATH_VELOCITY_MULT}x stricter)")
            logger.info(f"  ⚡ Fast path skips: Holder checks, 3s sleep")
        logger.info(f"  Age check: <{self.filters['max_token_age_seconds']}s (BEFORE RPC)")
        logger.info(f"  Curve prefilter: ≥{self.filters['min_curve_sol_prefilter']} SOL")
        logger.info(f"  First-sighting cooldown: {self.filters['first_sighting_cooldown_seconds']}s")
        logger.info(f"  Normal path: 3s sleep + holder checks")
        
        uri = "wss://pumpportal.fun/api/data"
        
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
                                        logger.info(f"📊 Filter stats: {self.tokens_passed} passed / {self.tokens_filtered} filtered / {self.tokens_deferred} deferred / {self.tokens_fast_path} fast path / {self.tokens_evaluated} evaluated ({filter_rate:.1f}% filtered)")
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
            'tokens_fast_path': self.tokens_fast_path,
            'filter_rate': (self.tokens_filtered / self.tokens_evaluated * 100) if self.tokens_evaluated > 0 else 0,
            'filter_reasons': self.filter_reasons
        }
    
    def stop(self):
        self.running = False
        stats = self.get_stats()
        logger.info(f"PumpPortal monitor stopped")
        logger.info(f"Stats: {stats['tokens_passed']} passed / {stats['tokens_filtered']} filtered / {stats['tokens_deferred']} deferred / {stats['tokens_fast_path']} fast path / {stats['tokens_evaluated']} evaluated ({stats['filter_rate']:.1f}% filtered)")
        if self.filter_reasons:
            logger.info(f"Filter breakdown: {self.filter_reasons}"))
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
                            await asyncio.sleep(retry_delay
