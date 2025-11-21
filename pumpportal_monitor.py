"""
PumpPortal WebSocket Monitor - FINAL OPTIMIZED + CHATGPT FIXES + 2 RETRIES + AGE FIX
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
        
        # Verify Helius API key
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
            'max_token_age_seconds': 16.0,  # Up from 2.0 - whale zone

            # OPTIMIZATION: Early curve prefilter
            'min_curve_sol_prefilter': 10.0,  # Early filter

            # Raised from 0.095 to 0.5 to filter out low-effort launches
            'min_creator_sol': 0.095,
            'max_creator_sol': 5.0,
            'min_curve_sol': 10.0,           # Whale entry zone
            'max_curve_sol': 14.0,           # Before whale exits
            'min_v_tokens': 500_000_000,
            'min_name_length': 3,

            # NEW: Reject bot pumps with excessive momentum
            'max_momentum': 350.0,  # Up from 200.0 - allow whale pumps
            'max_momentum_high_mc': 50.0,  # Stricter for >$6K MC

            'min_holders': 10,
            'check_concentration': False,
            'max_top10_concentration': 85,
            'max_velocity_sol_per_sec': 1.5,  # Unused - kept for compatibility
            'min_market_cap': 2350,          # 10 SOL @ $235
            'max_market_cap': 4700,          # 14 SOL @ $235 (lowered from 9400)  
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
        ✅ FIX: PumpPortal doesn't send timestamps - estimate from SOL raised
        """
        now = time.time()

        # Priority 1: Explicit age field (if available)
        if 'age' in token_data:
            age = token_data['age']
            if isinstance(age, (int, float)) and age > 0:
                logger.debug(f"📊 Age from token_data.age: {age:.1f}s")
                return float(age)

        if 'data' in token_data:
            inner = token_data['data']
            if 'age' in inner:
                age = inner['age']
                if isinstance(age, (int, float)) and age > 0:
                    logger.debug(f"📊 Age from token_data.data.age: {age:.1f}s")
                    return float(age)

        # Priority 2: Timestamp fields (if available)
        for key in ('blockTime', 'createdAt', 'timestamp', 'ts'):
            if key in token_data:
                try:
                    ts = float(token_data[key])
                    ts = ts / 1000.0 if ts > 1e12 else ts
                    age = now - ts
                    if 0 < age < 3600:
                        logger.debug(f"📊 Age from token_data.{key}: {age:.1f}s")
                        return age
                except (ValueError, TypeError):
                    continue

            if 'data' in token_data and key in token_data['data']:
                try:
                    ts = float(token_data['data'][key])
                    ts = ts / 1000.0 if ts > 1e12 else ts
                    age = now - ts
                    if 0 < age < 3600:
                        logger.debug(f"📊 Age from token_data.data.{key}: {age:.1f}s")
                        return age
                except (ValueError, TypeError):
                    continue

        # Priority 3: FALLBACK - Estimate from SOL raised (PumpPortal has no timestamps)
        token_data_inner = token_data.get('data', token_data)
        v_sol = float(token_data_inner.get('vSolInBondingCurve', 0))

        if v_sol > 0:
            # Estimate: Tokens pump at ~2-4 SOL/s in first 15s
            # Use conservative 2.5 SOL/s to avoid underestimating
            estimated_age = v_sol / 2.5

            # Clamp to reasonable range (2-60s)
            estimated_age = max(2.0, min(estimated_age, 60.0))

            logger.info(f"📊 Age estimated from SOL: {v_sol:.2f} SOL ÷ 2.5 = {estimated_age:.1f}s")
            return estimated_age

        # Priority 4: Use first-sighting time as last resort
        mint = token_data.get('mint') or token_data_inner.get('mint', '')
        if mint:
            if mint not in self.token_first_seen:
                self.token_first_seen[mint] = now
                logger.info(f"📊 First sighting of {mint[:8]}... - starting age clock at 0s")
                return 0.0

            age_since_first = now - self.token_first_seen[mint]
            logger.info(f"📊 Age from first sighting: {age_since_first:.1f}s")
            return age_since_first

        # No way to determine age - reject
        logger.error(f"❌ Could not determine age - no SOL data or mint address")
        return 999.0
    
    def _calculate_market_cap(self, token_data: dict) -> float:
        """
        Calculate market cap from bonding curve data
        Uses dynamic SOL price from cache
        """
        try:
            v_sol = float(token_data.get('vSolInBondingCurve', 0))
            v_tokens = float(token_data.get('vTokensInBondingCurve', 0))

            if v_sol == 0 or v_tokens == 0:
                return 0

            # PumpPortal sends human-readable units
            price_sol = v_sol / v_tokens
            total_supply = 1_000_000_000

            # Use current SOL price (updated every 5 min)
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
    
    def _check_recent_velocity(self, mint: str, current_sol: float) -> tuple:
        """
        Check velocity in LAST 1 SECOND to catch dying pumps
        FIX #1: Adaptive time window - don't fail young tokens with insufficient history
        """
        try:
            if mint not in self.recent_velocity_snapshots or len(self.recent_velocity_snapshots[mint]) < 2:
                return (True, None, "insufficient_history")
            
            history = self.recent_velocity_snapshots[mint]
            now = time.time()
            
            # CRITICAL FIX: Calculate actual time elapsed since first snapshot
            first_snapshot_time = history[0]['timestamp']
            time_elapsed = now - first_snapshot_time
            
            # If we have < 0.9s of history, defer (don't filter yet)
            if time_elapsed < 0.9:
                logger.debug(f"Velocity check deferred for {mint[:8]}... (only {time_elapsed:.2f}s of history)")
                return (True, None, f"deferred_young_token: {time_elapsed:.2f}s")
            
            # Use adaptive lookback window (max 1.0s, but use actual elapsed time if less)
            lookback_window = min(1.0, time_elapsed - 0.1)  # Leave 0.1s buffer
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
        """
        Check if creator is spamming tokens.
        FIXED: Thread-safe counter update
        """
        try:
            now = time.time()
            
            # FIXED: Use setdefault to avoid race conditions
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
        OPTIMIZED: Fast-fail RPC with 0.8s timeout + 2 retries
        FIX #3: Two retry attempts (2.5s + 2.5s) for fresh tokens that need more time
        NOTE: Main flow includes 3s sleep BEFORE calling this, so token is already ~3.5s old
        """
        retry_delays = [2.5, 2.5]  # First retry at +2.5s, second retry at +2.5s more
        
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
                    
                    # OPTIMIZATION: 0.8s timeout for fast-fail
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
                                
                                # Handle "not a Token mint" error with retry
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
    
    async def _apply_quality_filters(self, data: dict) -> tuple:
        """
        OPTIMIZED FILTER ORDER - IMMEDIATE PROCESSING:
        1. Age check (before expensive RPC)
        2. Curve prefilter (skip obvious duds)
        3. All quality filters immediately (no cooldown)
        Returns: (passed: bool, token_age: float)
        """
        if not self.filters['filters_enabled']:
            return (True, 0.0)

        token_data = data.get('data', data)
        mint = self._extract_mint(data)

        now = time.time()
        v_sol = float(token_data.get('vSolInBondingCurve', 0))

        # ===================================================================
        # CRITICAL FIX: Use REAL token age from event, not first sighting
        # ===================================================================
        token_age = self._event_age_seconds(token_data)

        if token_age > self.filters['max_token_age_seconds']:
            self._log_filter("too_old_prefilter", f"{token_age:.1f}s > {self.filters['max_token_age_seconds']}s")
            return (False, token_age)

        # ===================================================================
        # OPTIMIZATION 2: CURVE PREFILTER (skip obvious duds early)
        # ===================================================================
        if v_sol < self.filters['min_curve_sol_prefilter']:
            self._log_filter("low_curve_prefilter", f"{v_sol:.2f} SOL < {self.filters['min_curve_sol_prefilter']}")
            return (False, token_age)

        # ===================================================================
        # REMOVED COOLDOWN: Process immediately on first sighting for speed
        # ===================================================================
        logger.debug(f"📊 FIRST SIGHTING: {mint[:8]}... (age {token_age:.1f}s, {v_sol:.1f} SOL) - processing immediately")

        # Store velocity snapshot for monitoring
        self._store_recent_velocity_snapshot(mint, v_sol)

        # Continue to post-cooldown filters immediately (no waiting)
        return await self._apply_quality_filters_post_cooldown(data)
    
    async def _apply_quality_filters_post_cooldown(self, data: dict) -> tuple:
        """
        SECOND PASS - After cooldown expires:
        Returns (passed: bool, token_age: float)
        ALL 3 FIXES APPLIED HERE
        """
        token_data = data.get('data', data)
        mint = self._extract_mint(data)
        
        now = time.time()
        v_sol = float(token_data.get('vSolInBondingCurve', 0))
        
        # Re-check age (token is now older)
        token_age = self._event_age_seconds(token_data)
        
        # CRITICAL FIX: Continue storing velocity snapshots during evaluation
        # We need at least 0.9s of history for velocity check
        self._store_recent_velocity_snapshot(mint, v_sol)
        
        logger.info(f"✓ Token {mint[:8]}... passed cooldown: {token_age:.1f}s old, {v_sol:.1f} SOL")
        
        # Basic filters
        creator_sol = float(token_data.get('solAmount', 0))
        creator_address = str(token_data.get('traderPublicKey', 'unknown'))
        
        # FIX #2: Lowered minimum to 0.095 to handle rounding (0.099 SOL now passes!)
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
        
        # ✅ ADAPTIVE MOMENTUM: High ceiling for early tokens
        momentum = v_sol / creator_sol if creator_sol > 0 else 0
        await self._get_sol_price()
        market_cap = self._calculate_market_cap(token_data)

        # Adaptive momentum ceiling
        if market_cap < 6000:
            # Early tokens ($2-6K MC): Allow high momentum (whale zone)
            max_momentum = self.filters.get('max_momentum', 200.0)
        else:
            # Later tokens (>$6K MC): Strict bot pump check
            max_momentum = self.filters.get('max_momentum_high_mc', 50.0)

        if momentum > max_momentum:
            self._log_filter(
                "momentum_ceiling",
                f"{momentum:.1f}x > {max_momentum}x (MC: ${market_cap:,.0f})"
            )
            return (False, token_age)

        logger.info(f"✅ Momentum passed: {momentum:.1f}x (limit: {max_momentum}x for MC ${market_cap:,.0f})")

        # Virtual tokens
        v_tokens = float(token_data.get('vTokensInBondingCurve', 0))
        if v_tokens < self.filters['min_v_tokens']:
            self._log_filter("tokens_low", f"{v_tokens:,.0f}")
            return (False, token_age)
        
        # NOTE: Recent velocity check REMOVED from monitor
        # Velocity checking happens in main.py with live curve reads
        # We only have static snapshots here from WebSocket events
        
        # ===================================================================
        # OPTIMIZATION 4: CONCURRENT HOLDERS + LIQUIDITY + MC
        # FIX #3: Fast Helius retry with 3 attempts (2.5s + 2.5s + 2.5s) implemented above
        # CRITICAL: Wait 3s before first check to allow Helius indexing
        # ===================================================================
        logger.info(f"🔍 Running concurrent checks for {mint[:8]}...")
        
        # HELIUS REMOVED: No holder check - rely on price action post-buy
        # Skip the 3s sleep and holder validation entirely
        logger.info(f"✅ Skipping Helius check for speed - using price-based exits")

        # Still need market cap check
        await self._get_sol_price()
        market_cap = self._calculate_market_cap(token_data)

        if market_cap < self.filters['min_market_cap'] or market_cap > self.filters['max_market_cap']:
            self._log_filter("mc_range", f"${market_cap:,.0f}")
            return (False, token_age)
        
        # All filters passed!
        momentum = v_sol / creator_sol if creator_sol > 0 else 0

        logger.info(f"✅ PASSED ALL FILTERS: {name} ({symbol})")
        logger.info(f"   Creator: {creator_sol:.2f} SOL | Curve: {v_sol:.2f} SOL | MC: ${market_cap:,.0f}")
        logger.info(f"   Momentum: {momentum:.1f}x | Token age: {token_age:.1f}s")
        logger.info(f"   ⚡ FAST ENTRY MODE - No holder check")

        self._last_holder_result = {}
        return (True, token_age)
        
    async def start(self):
        """Connect to PumpPortal WebSocket"""
        self.running = True
        logger.info("🔍 Connecting to PumpPortal WebSocket...")
        logger.info(f"Strategy: ⚡ ULTRA-FAST ENTRY - NO HELIUS - WHALE COPY MODE")
        logger.info(f"  Speed: 0.5-2s entry (no cooldown, no RPC delays)")
        logger.info(f"  Filters: WebSocket only (creator, spam, momentum, blacklist)")
        logger.info(f"  Protection: Post-buy price action (rug trap, fail-fast, stop loss)")
        logger.info(f"  Goal: Match whale 52.7% win rate with 8-33s hold times")
        
        uri = "wss://pumpportal.fun/api/data"

        # DISABLED: No longer need pending queue since we process immediately
        # pending_task = asyncio.create_task(self._process_pending_tokens())
        logger.info("⚡ Pending queue disabled - immediate processing enabled")
        
        while self.running:
            try:
                # FIXED: Add WebSocket keepalive params
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
                                
                                # FIXED: Increment evaluated counter for ALL tokens
                                self.tokens_evaluated += 1

                                # CRITICAL FIX: Check filters immediately (no cooldown)
                                passed, token_age = await self._apply_quality_filters(data)

                                if not passed:
                                    # Token filtered
                                    self.tokens_filtered += 1
                                    # Stats logged every 10 tokens
                                    if self.tokens_evaluated % 10 == 0:
                                        filter_rate = (self.tokens_filtered / self.tokens_evaluated * 100) if self.tokens_evaluated > 0 else 0
                                        logger.info(f"📊 Filter stats: {self.tokens_passed} passed / {self.tokens_filtered} filtered / {self.tokens_evaluated} evaluated ({filter_rate:.1f}% filtered)")
                                        if self.filter_reasons:
                                            top_reasons = sorted(self.filter_reasons.items(), key=lambda x: x[1], reverse=True)[:3]
                                            logger.info(f"   Top reasons: {top_reasons}")
                                    continue

                                # Token passed all filters!
                                if mint in self.seen_tokens:
                                    continue

                                self.seen_tokens.add(mint)
                                self.tokens_passed += 1

                                token_data_inner = data.get('data', data)
                                v_sol = token_data_inner.get('vSolInBondingCurve', 0)
                                creator_sol = token_data_inner.get('solAmount', 0)
                                market_cap = self._calculate_market_cap(token_data_inner)

                                holder_data = getattr(self, '_last_holder_result', {})

                                logger.info("=" * 60)
                                logger.info("🚀 TOKEN PASSED ALL FILTERS!")
                                logger.info(f"📜 Mint: {mint}")
                                logger.info(f"📊 {self.tokens_passed} passed / {self.tokens_filtered} filtered / {self.tokens_evaluated} total")
                                logger.info(f"💰 Creator: {creator_sol:.2f} SOL | Curve: {v_sol:.2f} SOL")
                                logger.info(f"💵 Market Cap: ${market_cap:,.0f}")
                                logger.info(f"🔥 Momentum: {v_sol/creator_sol if creator_sol > 0 else 0:.1f}x")
                                logger.info(f"⏱️ Token age: {token_age:.1f}s")
                                logger.info(f"📝 {token_data_inner.get('name', 'Unknown')} ({token_data_inner.get('symbol', 'Unknown')})")
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
        
        # Cleanup (pending_task disabled)
        # pending_task.cancel()
    
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
