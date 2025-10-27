"""
PumpPortal WebSocket Monitor - COMPLETE WITH TRADE SUBSCRIPTIONS V2
Fixed: Trade events now properly store snapshots during cooldown

Key Features:
- Dynamic trade subscriptions during cooldown (collect 5-10 snapshots)
- Sustained velocity analysis (avg ≥1.2, min ≥0.6 SOL/s)
- Public/creator ratio check (≥4x organic demand)
- Tick density filter (≥5 snapshots = active buying)
- Maximum momentum cap (80x for bot pump detection)
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
        
        # Recent velocity snapshots (high-frequency during cooldown)
        self.recent_velocity_snapshots = {}
        
        # WebSocket reference for dynamic subscriptions
        self.websocket = None
        self.trade_subscriptions = set()  # Track which tokens we're subscribed to
        
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
        
        # Filters - OPTIMIZED with sustained velocity checks
        self.filters = {
            # CRITICAL: Age check FIRST (before expensive RPC calls)
            'max_token_age_seconds': 4.0,  # Only process tokens <4s old
            
            # OPTIMIZATION: Early curve prefilter
            'min_curve_sol_prefilter': 3.0,  # Skip tokens with <3 SOL (likely duds)
            
            # OPTIMIZED: Based on live data (GSG: 0.50, Skeleton: 2.96, Rugs: <0.45 or >3.5)
            'min_creator_sol': 0.45,  # Winners: 0.50-2.96 SOL, Rugs: 0.20-0.40 SOL
            'max_creator_sol': 3.5,   # Block manipulation (NOKINGS had 3.46 and was a rug)
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
            'min_recent_velocity_sol_per_sec': 3.0,
            'max_tokens_per_creator_24h': 3,
            
            # OPTIMIZATION: First-sighting cooldown (2s allows RPC indexing + velocity snapshots)
            'first_sighting_cooldown_seconds': 2.0,
            
            'filters_enabled': True
        }
        
        # Statistics
        self.tokens_evaluated = 0
        self.tokens_deferred = 0
        self.tokens_filtered = 0
        self.tokens_passed = 0
    
    async def _subscribe_to_token_trades(self, mint: str):
        """Subscribe to real-time trades for a specific token during cooldown"""
        if not self.websocket or mint in self.trade_subscriptions:
            return
        
        try:
            subscribe_msg = {
                "method": "subscribeTokenTrade",
                "keys": [mint]
            }
            await self.websocket.send(json.dumps(subscribe_msg))
            self.trade_subscriptions.add(mint)
            logger.info(f"📡 Subscribed to trades for {mint[:8]}... (snapshot accumulation)")
        except Exception as e:
            logger.error(f"Failed to subscribe to trades for {mint[:8]}...: {e}")
    
    async def _unsubscribe_from_token_trades(self, mint: str):
        """Unsubscribe from token trades after cooldown completes"""
        if not self.websocket or mint not in self.trade_subscriptions:
            return
        
        try:
            unsubscribe_msg = {
                "method": "unsubscribeTokenTrade",
                "keys": [mint]
            }
            await self.websocket.send(json.dumps(unsubscribe_msg))
            self.trade_subscriptions.discard(mint)
            logger.debug(f"📡 Unsubscribed from trades for {mint[:8]}...")
        except Exception as e:
            logger.debug(f"Failed to unsubscribe from {mint[:8]}...: {e}")
    
    def _is_trade_event(self, data: dict) -> bool:
        """Check if message is a trade event"""
        # Primary indicator: txType field
        if 'txType' in data:
            tx_type = data['txType']
            if tx_type in ['buy', 'sell', 'create']:
                # Exclude 'create' events with initialBuy (those are new tokens)
                if tx_type == 'create' and 'initialBuy' in data:
                    return False
                return True
        
        if 'transactionType' in data:
            return True
        
        # Signature + tokenAmount usually indicates a trade
        if 'signature' in data and 'tokenAmount' in data:
            return True
        
        return False
    
    async def _handle_trade_event(self, data: dict):
        """Handle real-time trade events for tokens in cooldown"""
        mint = self._extract_mint(data)
        
        if not mint:
            return
        
        # CRITICAL: Check if we're tracking this token
        # Trade events can come in BEFORE or AFTER we add to first_sighting_times
        # So we check both first_sighting_times AND pending_tokens
        is_tracking = (mint in self.first_sighting_times or mint in self.pending_tokens)
        
        if not is_tracking:
            # Not tracking this token, ignore
            return
        
        # Extract current SOL in curve from trade event
        v_sol = float(data.get('vSolInBondingCurve', 0))
        if v_sol == 0:
            # Try alternate field names
            v_sol = float(data.get('solAmount', 0))
        
        if v_sol > 0:
            # Store snapshot
            self._store_recent_velocity_snapshot(mint, v_sol)
            
            # Calculate time since first sighting (if available)
            if mint in self.first_sighting_times:
                time_since_first = time.time() - self.first_sighting_times[mint]
            else:
                time_since_first = 0
                
            snapshot_count = len(self.recent_velocity_snapshots.get(mint, []))
            
            logger.info(f"📸 TRADE SNAPSHOT #{snapshot_count} for {mint[:8]}... at t={time_since_first:.2f}s ({v_sol:.1f} SOL)")
    
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
        """Get REAL token age from event data, not first sighting time"""
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
            
            market_cap_sol = price_sol * total_supply
            market_cap_usd = market_cap_sol * self.sol_price_usd
            
            return market_cap_usd
        except Exception as e:
            logger.error(f"Market cap calculation error: {e}")
            return 0
    
    def _store_recent_velocity_snapshot(self, mint: str, sol_raised: float):
        """Store recent velocity snapshot"""
        if mint not in self.recent_velocity_snapshots:
            self.recent_velocity_snapshots[mint] = []
        
        snapshot = {
            'timestamp': time.time(),
            'sol_raised': sol_raised
        }
        
        self.recent_velocity_snapshots[mint].append(snapshot)
        
        # Keep only last 5 seconds of data
        cutoff_time = time.time() - 5.0
        self.recent_velocity_snapshots[mint] = [
            s for s in self.recent_velocity_snapshots[mint]
            if s['timestamp'] > cutoff_time
        ]
    
    def _check_recent_velocity(self, mint: str, current_sol: float) -> tuple:
        """Check velocity in LAST 1 SECOND to catch dying pumps"""
        try:
            if mint not in self.recent_velocity_snapshots or len(self.recent_velocity_snapshots[mint]) < 2:
                return (True, None, "insufficient_history")
            
            history = self.recent_velocity_snapshots[mint]
            now = time.time()
            
            # Calculate actual time elapsed since first snapshot
            first_snapshot_time = history[0]['timestamp']
            time_elapsed = now - first_snapshot_time
            
            # If we have < 0.9s of history, defer (don't filter yet)
            if time_elapsed < 0.9:
                logger.debug(f"Velocity check deferred for {mint[:8]}... (only {time_elapsed:.2f}s of history)")
                return (True, None, f"deferred_young_token: {time_elapsed:.2f}s")
            
            # Use adaptive lookback window (max 1.0s, but use actual elapsed time if less)
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
            
            # Clean up old entries (> 24h)
            self.creator_token_launches[creator_address] = [
                ts for ts in self.creator_token_launches[creator_address]
                if now - ts < 86400
            ]
            
            # Check how many tokens this creator launched in 24h
            token_count = len(self.creator_token_launches[creator_address])
            max_allowed = self.filters['max_tokens_per_creator_24h']
            
            if token_count >= max_allowed:
                logger.info(
                    f"❌ CREATOR SPAM: {creator_address[:8]}... launched "
                    f"{token_count} tokens in 24h (max {max_allowed})"
                )
                return (False, token_count, f"spam: {token_count} launches")
            
            # Record this launch
            self.creator_token_launches[creator_address].append(now)
            return (True, token_count, "ok")
            
        except Exception as e:
            logger.error(f"Error checking creator spam: {e}")
            return (True, 0, f"error: {e}")
    
    async def _check_holders_helius(self, mint: str, max_retries: int = 2) -> dict:
        """Check holder distribution using Helius API with FAST retries"""
        url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
        
        for attempt in range(max_retries + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    payload = {
                        "jsonrpc": "2.0",
                        "id": f"holder-check-{mint[:8]}",
                        "method": "getTokenAccounts",
                        "params": {
                            "mint": mint,
                            "limit": 100,
                            "page": 1
                        }
                    }
                    
                    timeout = aiohttp.ClientTimeout(total=2.5)
                    
                    async with session.post(url, json=payload, timeout=timeout) as resp:
                        if resp.status != 200:
                            if attempt < max_retries:
                                logger.debug(f"Helius attempt {attempt + 1}: HTTP {resp.status}, retrying...")
                                await asyncio.sleep(0.5)
                                continue
                            return {'passed': False, 'reason': f'http_{resp.status}'}
                        
                        data = await resp.json()
                        
                        if 'error' in data:
                            error_msg = data['error'].get('message', 'unknown')
                            if 'not found' in error_msg.lower() or 'not indexed' in error_msg.lower():
                                if attempt < max_retries:
                                    logger.debug(f"Helius attempt {attempt + 1}: not indexed yet, retrying...")
                                    await asyncio.sleep(0.5)
                                    continue
                            return {'passed': False, 'reason': f'error: {error_msg}'}
                        
                        result = data.get('result', {})
                        token_accounts = result.get('token_accounts', [])
                        
                        if not token_accounts:
                            if attempt < max_retries:
                                logger.debug(f"Helius attempt {attempt + 1}: no accounts, retrying...")
                                await asyncio.sleep(0.5)
                                continue
                            return {'passed': False, 'reason': 'no_holders'}
                        
                        # Calculate holder distribution
                        holders = {}
                        total_supply = 0
                        
                        for acc in token_accounts:
                            owner = acc.get('owner', '')
                            amount = float(acc.get('amount', 0))
                            
                            if owner not in holders:
                                holders[owner] = 0
                            holders[owner] += amount
                            total_supply += amount
                        
                        holder_count = len(holders)
                        
                        if holder_count < self.filters['min_holders']:
                            return {
                                'passed': False,
                                'reason': f'low_holders: {holder_count}',
                                'holder_count': holder_count
                            }
                        
                        if self.filters['check_concentration'] and total_supply > 0:
                            sorted_holders = sorted(holders.values(), reverse=True)
                            top10_supply = sum(sorted_holders[:10])
                            concentration = (top10_supply / total_supply) * 100
                            
                            if concentration > self.filters['max_top10_concentration']:
                                return {
                                    'passed': False,
                                    'reason': f'high_concentration: {concentration:.1f}%',
                                    'holder_count': holder_count,
                                    'concentration': concentration
                                }
                            
                            return {
                                'passed': True,
                                'holder_count': holder_count,
                                'concentration': concentration
                            }
                        else:
                            return {
                                'passed': True,
                                'holder_count': holder_count,
                                'concentration': 0
                            }
                            
            except asyncio.TimeoutError:
                if attempt < max_retries:
                    logger.debug(f"Helius attempt {attempt + 1}: timeout, retrying...")
                    await asyncio.sleep(0.5)
                    continue
                logger.warning(f"❌ Helius timeout for {mint[:8]}... after {max_retries + 1} attempts")
                return {'passed': False, 'reason': f'timeout_after_{max_retries + 1}_attempts'}
            except Exception as e:
                if attempt < max_retries:
                    logger.debug(f"Helius attempt {attempt + 1}: {str(e)[:50]}, retrying...")
                    await asyncio.sleep(0.5)
                    continue
                logger.error(f"Helius error for {mint[:8]}...: {e}")
                return {'passed': False, 'reason': f'error: {str(e)[:30]}'}
        
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
                
                # Find tokens ready for re-evaluation
                for mint, stored_data in list(self.pending_tokens.items()):
                    first_sight_time = self.first_sighting_times.get(mint, now)
                    time_since_first = now - first_sight_time
                    
                    if time_since_first >= self.filters['first_sighting_cooldown_seconds']:
                        tokens_to_process.append((mint, stored_data))
                        del self.pending_tokens[mint]
                        
                        # Unsubscribe from trade events
                        await self._unsubscribe_from_token_trades(mint)
                
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
    
    async def _apply_quality_filters(self, data: dict) -> bool:
        """
        FIRST PASS - Age check, curve prefilter, then cooldown
        """
        if not self.filters['filters_enabled']:
            return True
            
        token_data = data.get('data', data)
        mint = self._extract_mint(data)
        
        now = time.time()
        v_sol = float(token_data.get('vSolInBondingCurve', 0))
        
        # Age check FIRST
        token_age = self._event_age_seconds(token_data)
        
        if token_age > self.filters['max_token_age_seconds']:
            self._log_filter("too_old_prefilter", f"{token_age:.1f}s > {self.filters['max_token_age_seconds']}s")
            return False
        
        # Curve prefilter
        if v_sol < self.filters['min_curve_sol_prefilter']:
            self._log_filter("low_curve_prefilter", f"{v_sol:.2f} SOL < {self.filters['min_curve_sol_prefilter']}")
            return False
        
        # First-sighting cooldown - store token and subscribe to trades
        if mint not in self.first_sighting_times:
            self.first_sighting_times[mint] = now
            self.pending_tokens[mint] = data
            self._store_recent_velocity_snapshot(mint, v_sol)
            self.tokens_deferred += 1
            
            logger.info(f"📊 FIRST SIGHTING: {mint[:8]}... (age {token_age:.1f}s, {v_sol:.1f} SOL) - waiting {self.filters['first_sighting_cooldown_seconds']}s")
            
            # Subscribe to real-time trades for this token
            await self._subscribe_to_token_trades(mint)
            
            return False
        
        return False
    
    async def _apply_quality_filters_post_cooldown(self, data: dict) -> tuple:
        """
        SECOND PASS - After cooldown expires with sustained velocity checks
        Returns (passed: bool, token_age: float)
        """
        token_data = data.get('data', data)
        mint = self._extract_mint(data)
        
        now = time.time()
        v_sol = float(token_data.get('vSolInBondingCurve', 0))
        
        # Re-check age (token is now older)
        token_age = self._event_age_seconds(token_data)
        
        # Store final snapshot
        self._store_recent_velocity_snapshot(mint, v_sol)
        
        logger.info(f"✓ Token {mint[:8]}... passed cooldown: {token_age:.1f}s old, {v_sol:.1f} SOL")
        
        # Basic filters
        creator_sol = float(token_data.get('solAmount', 0))
        creator_address = str(token_data.get('traderPublicKey', 'unknown'))
        
        # Creator buy filter (optimized range)
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
        
        # Curve range
        if v_sol < self.filters['min_curve_sol'] or v_sol > self.filters['max_curve_sol']:
            self._log_filter("curve_range", f"{v_sol:.2f} SOL")
            return (False, token_age)
        
        # Momentum check (MINIMUM)
        required_multiplier = 8 if v_sol < 35 else (5 if v_sol < 50 else 3)
        required_sol = creator_sol * required_multiplier
        momentum = v_sol / creator_sol if creator_sol > 0 else 0
        
        if v_sol < required_sol:
            self._log_filter("momentum", f"{momentum:.1f}x < {required_multiplier}x")
            return (False, token_age)
        
        # ═══════════════════════════════════════════════════════════════
        # SUSTAINED VELOCITY CHECKS - Key to filtering single-spike rugs
        # ═══════════════════════════════════════════════════════════════
        
        # Check 1: Tick density - must have consistent updates
        if mint in self.recent_velocity_snapshots:
            snapshots = self.recent_velocity_snapshots[mint]
            snapshot_count = len(snapshots)
            
            # Show snapshot timeline
            if snapshot_count > 0:
                time_span = snapshots[-1]['timestamp'] - snapshots[0]['timestamp']
                logger.info(f"📸 Snapshot analysis: {snapshot_count} snapshots over {time_span:.2f}s")
            
            if snapshot_count < 5:
                self._log_filter("tick_density", f"only {snapshot_count} snapshots (need ≥5)")
                return (False, token_age)
            
            # Check 2: Sustained velocity - calculate avg and min
            now = time.time()
            lookback_window = 1.2
            recent_snaps = [s for s in snapshots if now - s['timestamp'] <= lookback_window]
            
            if len(recent_snaps) >= 3:
                velocities = []
                for i in range(1, len(recent_snaps)):
                    time_delta = recent_snaps[i]['timestamp'] - recent_snaps[i-1]['timestamp']
                    sol_delta = recent_snaps[i]['sol_raised'] - recent_snaps[i-1]['sol_raised']
                    if time_delta > 0:
                        vel = sol_delta / time_delta
                        velocities.append(vel)
                
                if velocities:
                    avg_velocity = sum(velocities) / len(velocities)
                    min_velocity = min(velocities)
                    
                    # Require sustained buying: avg ≥1.2 SOL/s, min ≥0.6 SOL/s
                    if avg_velocity < 1.2:
                        self._log_filter("avg_velocity_low", f"{avg_velocity:.2f} SOL/s < 1.2")
                        return (False, token_age)
                    
                    if min_velocity < 0.6:
                        self._log_filter("min_velocity_low", f"{min_velocity:.2f} SOL/s < 0.6")
                        return (False, token_age)
                    
                    logger.info(f"✓ Sustained velocity: avg={avg_velocity:.2f}, min={min_velocity:.2f} SOL/s")
        
        # Check 3: High momentum gate
        if momentum > 90:
            if mint in self.recent_velocity_snapshots:
                snapshots = self.recent_velocity_snapshots[mint]
                recent_snaps = [s for s in snapshots if time.time() - s['timestamp'] <= 1.2]
                
                if len(recent_snaps) >= 3:
                    velocities = []
                    for i in range(1, len(recent_snaps)):
                        time_delta = recent_snaps[i]['timestamp'] - recent_snaps[i-1]['timestamp']
                        sol_delta = recent_snaps[i]['sol_raised'] - recent_snaps[i-1]['sol_raised']
                        if time_delta > 0:
                            velocities.append(sol_delta / time_delta)
                    
                    if velocities:
                        min_velocity = min(velocities)
                        if min_velocity < 1.2:
                            self._log_filter("high_momentum_weak_sustain", f"{momentum:.1f}x but min_vel {min_velocity:.2f} < 1.2")
                            return (False, token_age)
        
        # Check 4: Public vs creator contribution
        public_sol = v_sol - creator_sol
        public_to_creator_ratio = public_sol / creator_sol if creator_sol > 0 else 0
        
        if public_to_creator_ratio < 4.0:
            self._log_filter("low_public_interest", f"public/creator ratio {public_to_creator_ratio:.1f}x < 4x")
            return (False, token_age)
        
        logger.info(f"✓ Public interest strong: {public_to_creator_ratio:.1f}x creator buy")
        
        # Check 5: Maximum momentum cap
        max_multiplier = 80 if v_sol < 35 else (60 if v_sol < 50 else 40)
        if momentum > max_multiplier:
            self._log_filter("momentum_too_high", f"{momentum:.1f}x > {max_multiplier}x")
            return (False, token_age)
        
        # Virtual tokens
        v_tokens = float(token_data.get('vTokensInBondingCurve', 0))
        if v_tokens < self.filters['min_v_tokens']:
            self._log_filter("v_tokens", f"{v_tokens:,.0f} < {self.filters['min_v_tokens']:,.0f}")
            return (False, token_age)
        
        # Market cap check
        logger.info(f"🔍 Running market cap check for {mint[:8]}...")
        await self._get_sol_price()
        market_cap = self._calculate_market_cap(token_data)
        
        if market_cap < self.filters['min_market_cap'] or market_cap > self.filters['max_market_cap']:
            self._log_filter("mc_range", f"${market_cap:,.0f}")
            return (False, token_age)
        
        # All filters passed!
        holder_count = 0
        concentration = 0
        
        logger.info(f"✅ PASSED ALL FILTERS: {name} ({symbol})")
        logger.info(f"   Creator: {creator_sol:.2f} SOL | Curve: {v_sol:.2f} SOL | MC: ${market_cap:,.0f}")
        logger.info(f"   Momentum: {momentum:.1f}x | Token age: {token_age:.1f}s")
        logger.info(f"   Holders: DISABLED ⚡")
        
        self._last_holder_result = {
            'passed': True,
            'holder_count': holder_count,
            'concentration': concentration
        }
        
        return (True, token_age)
        
    async def start(self):
        """Connect to PumpPortal WebSocket"""
        self.running = True
        logger.info("🔍 Connecting to PumpPortal WebSocket...")
        logger.info(f"Strategy: OPTIMIZED + SUSTAINED VELOCITY + TRADE SUBSCRIPTIONS V2")
        logger.info(f"  ✅ Creator buy: {self.filters['min_creator_sol']}-{self.filters['max_creator_sol']} SOL")
        logger.info(f"  ✅ Tick density: ≥5 snapshots (real-time trades)")
        logger.info(f"  ✅ Sustained velocity: avg ≥1.2, min ≥0.6 SOL/s")
        logger.info(f"  ✅ Public/creator ratio: ≥4x organic demand")
        logger.info(f"  ✅ Maximum momentum: 80x cap")
        logger.info(f"  ✅ Cooldown: {self.filters['first_sighting_cooldown_seconds']}s")
        
        uri = "wss://pumpportal.fun/api/data"
        
        # Start background task
        pending_task = asyncio.create_task(self._process_pending_tokens())
        
        while self.running:
            try:
                async with websockets.connect(
                    uri,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5
                ) as websocket:
                    self.websocket = websocket
                    logger.info("✅ Connected to PumpPortal WebSocket!")
                    
                    subscribe_msg = {"method": "subscribeNewToken"}
                    await websocket.send(json.dumps(subscribe_msg))
                    logger.info("📡 Subscribed to new token events")
                    
                    while self.running:
                        try:
                            message = await asyncio.wait_for(websocket.recv(), timeout=30)
                            data = json.loads(message)
                            
                            # Handle trade events (real-time snapshots)
                            if self._is_trade_event(data):
                                await self._handle_trade_event(data)
                                continue
                            
                            # Handle new token events
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
                self.websocket = None
                self.trade_subscriptions.clear()
                if self.running:
                    self.seen_tokens.clear()
                    self.reconnect_count += 1
                    logger.info(f"Reconnecting in 5s... (attempt #{self.reconnect_count + 1})")
                    await asyncio.sleep(5)
        
        # Cleanup
        pending_task.cancel()
        self.websocket = None
    
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
        self.websocket = None
        self.trade_subscriptions.clear()
        stats = self.get_stats()
        logger.info(f"PumpPortal monitor stopped")
        logger.info(f"Stats: {stats['tokens_passed']} passed / {stats['tokens_filtered']} filtered / {stats['tokens_deferred']} deferred / {stats['tokens_evaluated']} evaluated ({stats['filter_rate']:.1f}% filtered)")
        if self.filter_reasons:
            logger.info(f"Filter breakdown: {self.filter_reasons}")
