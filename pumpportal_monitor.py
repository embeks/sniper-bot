"""
PumpPortal WebSocket Monitor - FINAL OPTIMIZED + CHATGPT FIXES
All 3 critical fixes applied:
- Fix #1: Adaptive velocity window (no false 0.00 SOL/s)
- Fix #2: Creator buy rounding tolerance (0.095 SOL minimum)
- Fix #3: Fast Helius retry (2.5s instead of 10s)
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
            'min_holders': 10,
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
        """
        Calculate market cap from bonding curve data
        TODO: vTokensInBondingCurve may be in atomic units - verify with PumpPortal
        If atomic, need to normalize by 10**decimals
        """
        try:
            v_sol = float(token_data.get('vSolInBondingCurve', 0))
            v_tokens = float(token_data.get('vTokensInBondingCurve', 0))
            
            if v_sol == 0 or v_tokens == 0:
                return 0
            
            # Assuming v_tokens is in human-readable units (typical for PumpPortal)
            # If you see MC values 1000x off, v_tokens is likely atomic
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
    
    async def _check_holders_helius(self, mint: str, retry: bool = True) -> dict:
        """
        OPTIMIZED: Fast-fail RPC with 0.8s timeout + 1 retry
        FIX #3: Faster retry (2.5s instead of 10s) for fresh tokens
        """
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
                            if retry:
                                logger.debug(f"Retry holder check for {mint[:8]}...")
                                return await self._check_holders_helius(mint, retry=False)
                            return {'passed': False, 'reason': f'HTTP {resp.status}'}
                        
                        data = await resp.json()
                        
                        if 'error' in data:
                            error_msg = data['error'].get('message', '')
                            
                            # FIX #3: Faster retry for fresh tokens (2.5s instead of 10s)
                            if 'not a Token mint' in error_msg and retry:
                                # Token too new - wait briefly and retry once
                                jitter = random.uniform(0, 0.2)
                                retry_delay = 2.5 + jitter  # CHANGED from 10s to 2.5s
                                logger.info(f"⏳ Token not indexed yet, waiting {retry_delay:.1f}s and retrying...")
                                await asyncio.sleep(retry_delay)
                                return await self._check_holders_helius(mint, retry=False)
                            
                            return {'passed': False, 'reason': error_msg}
                        
                        if 'result' not in data or 'value' not in data['result']:
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
                        
                        return {
                            'passed': True,
                            'holder_count': account_count,
                            'concentration': concentration
                        }
                
                except asyncio.TimeoutError:
                    if retry:
                        logger.debug(f"Timeout, retry holder check for {mint[:8]}...")
                        return await self._check_holders_helius(mint, retry=False)
                    return {'passed': False, 'reason': 'holder_timeout'}
                    
        except Exception as e:
            logger.error(f"Helius exception: {e}")
            return {'passed': False, 'reason': str(e)}
    
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
                    passed = await self._apply_quality_filters_post_cooldown(data)
                    
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
        
        # ===================================================================
        # CRITICAL FIX: Use REAL token age from event, not first sighting
        # ===================================================================
        token_age = self._event_age_seconds(token_data)
        
        if token_age > self.filters['max_token_age_seconds']:
            self._log_filter("too_old_prefilter", f"{token_age:.1f}s > {self.filters['max_token_age_seconds']}s")
            return False
        
        # ===================================================================
        # OPTIMIZATION 2: CURVE PREFILTER (skip obvious duds early)
        # ===================================================================
        if v_sol < self.filters['min_curve_sol_prefilter']:
            self._log_filter("low_curve_prefilter", f"{v_sol:.2f} SOL < {self.filters['min_curve_sol_prefilter']}")
            return False
        
        # ===================================================================
        # OPTIMIZATION 3: FIRST-SIGHTING COOLDOWN (0.5s confirmation)
        # CRITICAL FIX: Store token data for re-evaluation by background task
        # ===================================================================
        if mint not in self.first_sighting_times:
            self.first_sighting_times[mint] = now
            self.pending_tokens[mint] = data  # Store for later re-evaluation
            self._store_recent_velocity_snapshot(mint, v_sol)
            self.tokens_deferred += 1
            logger.info(f"📊 FIRST SIGHTING: {mint[:8]}... (age {token_age:.1f}s, {v_sol:.1f} SOL) - waiting {self.filters['first_sighting_cooldown_seconds']}s")
            return False
        
        # This should never be reached now (handled by background task)
        return False
    
    async def _apply_quality_filters_post_cooldown(self, data: dict) -> bool:
        """
        SECOND PASS - After cooldown expires:
        4. Basic checks (name, creator)
        5. CONCURRENT: Holders + Liquidity
        6. Final validation
        ALL 3 FIXES APPLIED HERE
        """
        token_data = data.get('data', data)
        mint = self._extract_mint(data)
        
        now = time.time()
        v_sol = float(token_data.get('vSolInBondingCurve', 0))
        
        # Re-check age (token is now older)
        token_age = self._event_age_seconds(token_data)
        
        # Store snapshot for velocity tracking
        self._store_recent_velocity_snapshot(mint, v_sol)
        
        logger.info(f"✓ Token {mint[:8]}... passed cooldown: {token_age:.1f}s old, {v_sol:.1f} SOL")
        
        # Basic filters
        creator_sol = float(token_data.get('solAmount', 0))
        creator_address = str(token_data.get('traderPublicKey', 'unknown'))
        
        # FIX #2: Lowered minimum to 0.095 to handle rounding (0.099 SOL now passes!)
        if creator_sol < self.filters['min_creator_sol'] or creator_sol > self.filters['max_creator_sol']:
            self._log_filter("creator_buy", f"{creator_sol:.3f} SOL")
            return False
        
        # Creator spam check
        creator_passed, _, _ = self._check_creator_spam(creator_address)
        if not creator_passed:
            self._log_filter("creator_spam", "max tokens/24h")
            return False
        
        # Name quality
        name = str(token_data.get('name', '')).strip()
        symbol = str(token_data.get('symbol', '')).strip()
        
        if len(name) < self.filters['min_name_length'] or not name.isascii() or not symbol.isascii():
            self._log_filter("name_quality", f"{name}")
            return False
        
        # Blacklist check
        name_lower = name.lower()
        symbol_lower = symbol.lower()
        for blacklisted in self.filters['name_blacklist']:
            if blacklisted in name_lower or blacklisted in symbol_lower:
                self._log_filter("blacklist", f"'{blacklisted}' in {name}")
                return False
        
        # Bonding curve SOL range
        if v_sol < self.filters['min_curve_sol'] or v_sol > self.filters['max_curve_sol']:
            self._log_filter("curve_range", f"{v_sol:.2f} SOL")
            return False
        
        # Momentum check
        required_multiplier = 8 if v_sol < 35 else (5 if v_sol < 50 else 3)
        required_sol = creator_sol * required_multiplier
        if v_sol < required_sol:
            self._log_filter("momentum", f"{v_sol:.2f} vs {required_sol:.2f}")
            return False
        
        # Virtual tokens
        v_tokens = float(token_data.get('vTokensInBondingCurve', 0))
        if v_tokens < self.filters['min_v_tokens']:
            self._log_filter("tokens_low", f"{v_tokens:,.0f}")
            return False
        
        # FIX #1: Recent velocity check with adaptive time window
        if len(self.recent_velocity_snapshots.get(mint, [])) >= 2:
            recent_passed, recent_value, _ = self._check_recent_velocity(mint, v_sol)
            if not recent_passed:
                self._log_filter("recent_velocity_low", f"{recent_value:.2f} SOL/s" if recent_value else "insufficient data")
                return False
        
        # ===================================================================
        # OPTIMIZATION 4: CONCURRENT HOLDERS + LIQUIDITY + MC
        # FIX #3: Fast Helius retry (2.5s instead of 10s) implemented above
        # ===================================================================
        logger.info(f"🔍 Running concurrent checks for {mint[:8]}...")
        
        # Market cap calculation (fast, local)
        await self._get_sol_price()
        market_cap = self._calculate_market_cap(token_data)
        
        if market_cap < self.filters['min_market_cap'] or market_cap > self.filters['max_market_cap']:
            self._log_filter("mc_range", f"${market_cap:,.0f}")
            return False
        
        # CONCURRENT: Holder check (this is the slow part, but now with fast retry!)
        holder_task = asyncio.create_task(self._check_holders_helius(mint))
        
        # Wait for holder check with fast-fail
        holder_result = await holder_task
        
        if not holder_result['passed']:
            self._log_filter("holder_distribution", holder_result.get('reason', 'unknown'))
            return False
        
        # All filters passed!
        momentum = v_sol / creator_sol if creator_sol > 0 else 0
        holder_count = holder_result.get('holder_count', 0)
        concentration = holder_result.get('concentration', 0)
        
        logger.info(f"✅ PASSED ALL FILTERS: {name} ({symbol})")
        logger.info(f"   Creator: {creator_sol:.2f} SOL | Curve: {v_sol:.2f} SOL | MC: ${market_cap:,.0f}")
        logger.info(f"   Momentum: {momentum:.1f}x | Token age: {token_age:.1f}s")
        logger.info(f"   Holders: {holder_count} | Concentration: {concentration:.1f}%")
        
        self._last_holder_result = holder_result
        return True
        
    async def start(self):
        """Connect to PumpPortal WebSocket"""
        self.running = True
        logger.info("🔍 Connecting to PumpPortal WebSocket...")
        logger.info(f"Strategy: OPTIMIZED + ALL 3 CHATGPT FIXES")
        logger.info(f"  Fix #1: Adaptive velocity window (no false 0.00 SOL/s)")
        logger.info(f"  Fix #2: Creator buy tolerance (≥0.095 SOL)")
        logger.info(f"  Fix #3: Fast Helius retry (2.5s instead of 10s)")
        logger.info(f"  Age check: <{self.filters['max_token_age_seconds']}s (BEFORE RPC)")
        logger.info(f"  Curve prefilter: ≥{self.filters['min_curve_sol_prefilter']} SOL")
        logger.info(f"  First-sighting cooldown: {self.filters['first_sighting_cooldown_seconds']}s")
        logger.info(f"  RPC timeout: 0.8s with 1 retry")
        logger.info(f"  Concurrent checks: ENABLED")
        
        uri = "wss://pumpportal.fun/api/data"
        
        # Start background task for re-evaluating pending tokens
        pending_task = asyncio.create_task(self._process_pending_tokens())
        
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
                                
                                # CRITICAL FIX: Check filters (stores for later if needed)
                                passed = await self._apply_quality_filters(data)
                                
                                if not passed:
                                    # Token either filtered or deferred for cooldown
                                    # Stats logged every 10 tokens
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
