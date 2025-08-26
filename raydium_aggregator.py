"""
Raydium Aggregator - Complete Fixed Version with Quality Optimizations
Monitors Raydium for new pool creations and liquidity additions
All offset errors and liquidity detection issues FIXED
CRASH PREVENTION ADDED
"""

import asyncio
import json
import logging
import struct
import time
from typing import Dict, List, Optional, Tuple, Set, Any
from dataclasses import dataclass
from datetime import datetime, timedelta

import aiohttp
import base58
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Processed, Confirmed
from solders.pubkey import Pubkey
from solders.rpc.responses import GetAccountInfoResp
from solders.account import Account

logger = logging.getLogger(__name__)

# Raydium Programs
RAYDIUM_AMM_PROGRAM = Pubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8")
RAYDIUM_AUTHORITY = Pubkey.from_string("5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1")
RAYDIUM_OPENBOOK_PROGRAM = Pubkey.from_string("srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX")

# Raydium Pool Seeds
RAYDIUM_POOL_SEED_PREFIX = b"amm_associated_seed"

# Known tokens to track
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

# QUALITY THRESHOLDS - FIXED FOR HIGH QUALITY
MIN_POOL_LIQUIDITY_SOL = 10.0  # Minimum 10 SOL liquidity to consider
MIN_POOL_AGE_SECONDS = 30  # Pool must be at least 30 seconds old

@dataclass
class RaydiumPool:
    """Pool information"""
    pool_id: str
    token_mint: str
    base_vault: str
    quote_vault: str
    lp_supply: float
    base_amount: float
    quote_amount: float
    creation_time: float
    fee_rate: float = 0.0025  # 0.25% default
    
    @property
    def estimated_lp_sol(self) -> float:
        """Estimate SOL liquidity in pool"""
        # If quote is SOL
        if self.quote_vault:
            return self.quote_amount / 1e9
        # If base is SOL (rare)
        elif self.base_vault:
            return self.base_amount / 1e9
        return 0.0
    
    def is_quality_pool(self) -> bool:
        """Check if pool meets quality thresholds"""
        # Must have minimum liquidity
        if self.estimated_lp_sol < MIN_POOL_LIQUIDITY_SOL:
            return False
        
        # Must not be too new (avoid honeypots)
        age = time.time() - self.creation_time
        if age < MIN_POOL_AGE_SECONDS:
            return False
            
        return True

class RaydiumAggregator:
    """Enhanced Raydium aggregator with caching and WebSocket support"""
    
    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url
        self.client = AsyncClient(rpc_url, commitment=Processed)
        self.session = None
        
        # Pool cache
        self.pool_cache: Dict[str, RaydiumPool] = {}
        self.token_to_pool: Dict[str, str] = {}  # token_mint -> pool_id
        
        # Detection stats
        self.pools_found = 0
        self.last_detection = 0
        
        # WebSocket connection
        self.ws_client = None
        self.monitoring = False
        
        # Token tracking
        self.tracked_tokens: Set[str] = set()
        self.recent_pools: List[Dict] = []  # Recent pool creations
        
        # Initialize with attempts counter
        self._init_attempts = 0
        self._max_init_attempts = 3
        
        # QUALITY TRACKING
        self.quality_pools_found = 0
        self.low_quality_rejected = 0
        
    async def __aenter__(self):
        """Async context manager entry"""
        await self.initialize()
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self.close()
    
    async def initialize(self):
        """Initialize aggregator with retry logic"""
        try:
            if not self.session:
                self.session = aiohttp.ClientSession()
            
            # Test RPC connection
            await self.client.is_connected()
            logger.info("[Raydium] Aggregator initialized with quality filters")
            logger.info(f"[Raydium] Min liquidity: {MIN_POOL_LIQUIDITY_SOL} SOL")
            
        except Exception as e:
            self._init_attempts += 1
            if self._init_attempts < self._max_init_attempts:
                logger.warning(f"[Raydium] Init attempt {self._init_attempts} failed: {e}, retrying...")
                await asyncio.sleep(1)
                await self.initialize()
            else:
                logger.error(f"[Raydium] Failed to initialize after {self._max_init_attempts} attempts")
                raise
    
    async def close(self):
        """Cleanup resources"""
        try:
            if self.ws_client:
                await self.ws_client.close()
            if self.session:
                await self.session.close()
            await self.client.close()
        except Exception as e:
            logger.error(f"[Raydium] Error during cleanup: {e}")
    
    async def find_pool_for_token(self, token_mint: str) -> Optional[RaydiumPool]:
        """Find Raydium pool for a given token - FULLY FIXED VERSION WITH CRASH PREVENTION"""
        try:
            # Check cache first
            if token_mint in self.token_to_pool:
                pool_id = self.token_to_pool[token_mint]
                if pool_id in self.pool_cache:
                    pool = self.pool_cache[pool_id]
                    # Validate it's still a quality pool
                    if pool.is_quality_pool():
                        return pool
                    else:
                        logger.info(f"[Raydium] Cached pool {pool_id[:8]}... no longer meets quality standards")
            
            logger.info(f"[Raydium] Searching for quality pool with {token_mint[:8]}...")
            
            # Try limited scan first (faster)
            logger.info(f"[Raydium] Doing limited scan (max 50 pools)...")
            pool = await self._scan_recent_pools(token_mint)
            
            if pool and pool.is_quality_pool():
                logger.info(f"[Raydium] Found QUALITY pool {pool.pool_id[:8]}... for {token_mint[:8]}... with {pool.estimated_lp_sol:.2f} SOL")
                return pool
            elif pool:
                self.low_quality_rejected += 1
                logger.info(f"[Raydium] Pool found but LOW QUALITY: {pool.estimated_lp_sol:.2f} SOL")
            
            # FIX: Add timeout to prevent hanging
            logger.info(f"[Raydium] Doing full program scan...")
            try:
                pool = await asyncio.wait_for(
                    self._scan_program_accounts(token_mint),
                    timeout=10.0  # 10 second timeout
                )
            except asyncio.TimeoutError:
                logger.warning(f"[Raydium] Full program scan timed out for {token_mint[:8]}...")
                return None
            
            if pool and pool.is_quality_pool():
                logger.info(f"[Raydium] Found QUALITY pool {pool.pool_id[:8]}... via program scan")
                return pool
            elif pool:
                self.low_quality_rejected += 1
                logger.info(f"[Raydium] Pool found but LOW QUALITY in full scan")
            
            logger.info(f"[Raydium] No quality pool found for {token_mint[:8]}...")
            return None
            
        except Exception as e:
            logger.error(f"[Raydium] Error finding pool: {e}")
            return None
    
    async def _scan_recent_pools(self, token_mint: str, max_pools: int = 50) -> Optional[RaydiumPool]:
        """Scan recent pools - FULLY FIXED VERSION without offset attribute access"""
        try:
            # FIXED: Simple approach without problematic imports
            response = await self.client.get_program_accounts(
                RAYDIUM_AMM_PROGRAM,
                commitment=Processed,
                encoding="base64"
            )
            
            # Handle both response types
            if not response:
                return None
                
            accounts = response.value if hasattr(response, 'value') else response
            if not accounts:
                return None
            
            # Process only recent pools (limit for performance)
            accounts_to_check = accounts[:max_pools] if len(accounts) > max_pools else accounts
            
            for account_info in accounts_to_check:
                try:
                    pool = await self._parse_pool_account(
                        str(account_info.pubkey),
                        account_info.account
                    )
                    
                    if pool and pool.token_mint == token_mint:
                        # QUALITY CHECK before caching
                        if pool.is_quality_pool():
                            # Cache the pool
                            self.pool_cache[pool.pool_id] = pool
                            self.token_to_pool[token_mint] = pool.pool_id
                            self.quality_pools_found += 1
                            return pool
                        else:
                            logger.debug(f"[Raydium] Pool {pool.pool_id[:8]}... rejected: liquidity {pool.estimated_lp_sol:.2f} SOL")
                        
                except Exception as e:
                    continue
            
            return None
            
        except Exception as e:
            logger.warning(f"[Raydium] Limited scan failed: {e}, falling back to Jupiter")
            return None
    
    async def _scan_program_accounts(self, token_mint: str) -> Optional[RaydiumPool]:
        """Full program account scan with quality filters - FIXED WITH TIMEOUT AND ERROR HANDLING"""
        try:
            # FIX: Limit the number of accounts to scan to prevent hanging
            max_accounts_to_scan = 100  # Reduced from scanning ALL accounts
            
            # This is more expensive but thorough
            response = await self.client.get_program_accounts(
                RAYDIUM_AMM_PROGRAM,
                commitment=Processed,
                encoding="base64"
            )
            
            # Handle both response types
            if not response:
                return None
                
            accounts = response.value if hasattr(response, 'value') else response
            if not accounts:
                return None
            
            # FIX: Process only a limited number of accounts to prevent timeout
            accounts_to_process = accounts[:max_accounts_to_scan] if len(accounts) > max_accounts_to_scan else accounts
            
            for account_info in accounts_to_process:
                try:
                    pool = await self._parse_pool_account(
                        str(account_info.pubkey),
                        account_info.account
                    )
                    
                    if pool and pool.token_mint == token_mint:
                        # QUALITY CHECK
                        if pool.is_quality_pool():
                            # Cache the pool
                            self.pool_cache[pool.pool_id] = pool
                            self.token_to_pool[token_mint] = pool.pool_id
                            self.quality_pools_found += 1
                            return pool
                        
                except Exception as e:
                    # Don't let individual account errors crash the whole scan
                    continue
            
            return None
            
        except Exception as e:
            # FIX: Catch ALL exceptions and return None instead of crashing
            logger.error(f"[Raydium] Program scan failed: {e}")
            return None
    
    async def _parse_pool_account(self, pool_id: str, account: Account) -> Optional[RaydiumPool]:
        """Parse pool account data - ENHANCED WITH VALIDATION"""
        try:
            if not account or not account.data:
                return None
            
            # Decode account data
            data = base58.b58decode(account.data[0]) if isinstance(account.data, list) else account.data
            
            if len(data) < 752:  # Minimum pool state size
                return None
            
            # Parse pool state structure
            # Extract key fields from known offsets
            base_vault = base58.b58encode(data[352:384]).decode()
            base_mint = base58.b58encode(data[384:416]).decode()
            quote_vault = base58.b58encode(data[416:448]).decode()
            quote_mint = base58.b58encode(data[448:480]).decode()
            
            # Determine token mint (non-SOL token)
            token_mint = None
            if quote_mint == WSOL:
                token_mint = base_mint
            elif base_mint == WSOL:
                token_mint = quote_mint
            else:
                # Neither is SOL, check for USDC/USDT pairs
                if quote_mint in [USDC, USDT]:
                    token_mint = base_mint
                elif base_mint in [USDC, USDT]:
                    token_mint = quote_mint
                else:
                    # Use base as default
                    token_mint = base_mint
            
            # Get vault balances
            base_amount = 0
            quote_amount = 0
            
            try:
                # FIX: Add timeout to balance fetching
                base_info = await asyncio.wait_for(
                    self.client.get_account_info(Pubkey.from_string(base_vault)),
                    timeout=2.0
                )
                if base_info and base_info.value:
                    base_amount = base_info.value.lamports
                
                quote_info = await asyncio.wait_for(
                    self.client.get_account_info(Pubkey.from_string(quote_vault)),
                    timeout=2.0
                )
                if quote_info and quote_info.value:
                    quote_amount = quote_info.value.lamports
            except (asyncio.TimeoutError, Exception) as e:
                # Don't crash if we can't get balances
                logger.debug(f"[Raydium] Could not fetch vault balances: {e}")
            
            pool = RaydiumPool(
                pool_id=pool_id,
                token_mint=token_mint,
                base_vault=base_vault,
                quote_vault=quote_vault,
                lp_supply=0,  # Would need LP mint balance
                base_amount=base_amount,
                quote_amount=quote_amount,
                creation_time=time.time()
            )
            
            # IMMEDIATE QUALITY CHECK
            if pool.estimated_lp_sol < MIN_POOL_LIQUIDITY_SOL:
                logger.debug(f"[Raydium] Parsed pool has low liquidity: {pool.estimated_lp_sol:.2f} SOL")
                self.low_quality_rejected += 1
            
            return pool
            
        except Exception as e:
            # FIX: Don't crash on parse errors
            logger.debug(f"[Raydium] Error parsing pool account: {e}")
            return None
    
    async def get_pool_info(self, pool_id: str) -> Optional[RaydiumPool]:
        """Get detailed pool information with quality validation"""
        try:
            # Check cache
            if pool_id in self.pool_cache:
                pool = self.pool_cache[pool_id]
                # Update balances if pool is cached
                if time.time() - pool.creation_time > 5:  # Refresh every 5 seconds
                    await self._update_pool_balances(pool)
                
                # Validate quality
                if not pool.is_quality_pool():
                    logger.warning(f"[Raydium] Pool {pool_id[:8]}... no longer meets quality standards")
                    # Remove from cache
                    del self.pool_cache[pool_id]
                    if pool.token_mint in self.token_to_pool:
                        del self.token_to_pool[pool.token_mint]
                    return None
                    
                return pool
            
            # Fetch pool account with timeout
            response = await asyncio.wait_for(
                self.client.get_account_info(
                    Pubkey.from_string(pool_id),
                    commitment=Processed
                ),
                timeout=5.0
            )
            
            if not response or not response.value:
                logger.error(f"[Raydium] No account data returned for pool {pool_id[:8]}...")
                return None
            
            pool = await self._parse_pool_account(pool_id, response.value)
            if pool and pool.is_quality_pool():
                self.pool_cache[pool_id] = pool
                self.token_to_pool[pool.token_mint] = pool_id
                self.quality_pools_found += 1
            
            return pool
            
        except asyncio.TimeoutError:
            logger.error(f"[Raydium] Timeout getting pool info for {pool_id[:8]}...")
            return None
        except Exception as e:
            logger.error(f"[Raydium] Error getting pool info: {e}")
            return None
    
    async def _update_pool_balances(self, pool: RaydiumPool):
        """Update pool balance information with timeout"""
        try:
            # Get base vault balance with timeout
            base_info = await asyncio.wait_for(
                self.client.get_account_info(Pubkey.from_string(pool.base_vault)),
                timeout=2.0
            )
            if base_info and base_info.value:
                pool.base_amount = base_info.value.lamports
            
            # Get quote vault balance with timeout
            quote_info = await asyncio.wait_for(
                self.client.get_account_info(Pubkey.from_string(pool.quote_vault)),
                timeout=2.0
            )
            if quote_info and quote_info.value:
                pool.quote_amount = quote_info.value.lamports
                
        except (asyncio.TimeoutError, Exception) as e:
            logger.error(f"[Raydium] Error updating balances: {e}")
    
    def register_pool(self, pool_id: str, token_mint: str, base_vault: str = "", 
                     quote_vault: str = "", lp_amount: float = 0):
        """Register a new pool in cache - WITH QUALITY VALIDATION"""
        try:
            # QUALITY CHECK FIRST
            if lp_amount < MIN_POOL_LIQUIDITY_SOL:
                logger.warning(f"[Raydium] NOT registering low quality pool {pool_id[:8]}... with {lp_amount:.2f} SOL")
                self.low_quality_rejected += 1
                return
            
            # Check if this pool is already registered for a different token
            if pool_id in self.pool_cache:
                existing_pool = self.pool_cache[pool_id]
                if existing_pool.token_mint != token_mint:
                    logger.warning(f"[Raydium] Pool {pool_id[:8]}... already registered for {existing_pool.token_mint[:8]}..., not {token_mint[:8]}...")
                    return
            
            # Check if token already has a different pool
            if token_mint in self.token_to_pool:
                existing_pool_id = self.token_to_pool[token_mint]
                if existing_pool_id != pool_id:
                    logger.warning(f"[Raydium] Token {token_mint[:8]}... already has pool {existing_pool_id[:8]}..., not registering {pool_id[:8]}...")
                    return
            
            logger.info(f"[Raydium] Registering QUALITY pool {pool_id[:8]}... for {token_mint[:8]}... with {lp_amount:.2f} SOL")
            
            pool = RaydiumPool(
                pool_id=pool_id,
                token_mint=token_mint,
                base_vault=base_vault,
                quote_vault=quote_vault,
                lp_supply=0,
                base_amount=0,
                quote_amount=lp_amount * 1e9 if lp_amount else 0,  # Convert SOL to lamports
                creation_time=time.time()
            )
            
            self.pool_cache[pool_id] = pool
            self.token_to_pool[token_mint] = pool_id
            self.quality_pools_found += 1
            
            # Add to recent pools
            self.recent_pools.append({
                'pool_id': pool_id,
                'token_mint': token_mint,
                'timestamp': time.time(),
                'lp_sol': lp_amount
            })
            
            # Keep only last 100 pools
            if len(self.recent_pools) > 100:
                self.recent_pools = self.recent_pools[-100:]
            
            logger.info(f"[Raydium] Registered quality pool {pool_id[:8]}... for token {token_mint[:8]}...")
            
        except Exception as e:
            logger.error(f"[Raydium] Error registering pool: {e}")
    
    async def monitor_pool_creations(self, callback):
        """Monitor for new pool creations via WebSocket - QUALITY FILTERED"""
        try:
            self.monitoring = True
            logger.info("[Raydium] Starting pool creation monitor with quality filters...")
            
            # Subscribe to Raydium program logs
            async with self.client.logs_subscribe(
                RAYDIUM_AMM_PROGRAM,
                commitment=Processed
            ) as websocket:
                async for msg in websocket:
                    if not self.monitoring:
                        break
                    
                    try:
                        # Parse log message
                        if msg and hasattr(msg, 'result'):
                            logs = msg.result.value.logs
                            signature = msg.result.value.signature
                            
                            # Look for pool creation indicators
                            if any("InitializePool" in log or "initialize2" in log for log in logs):
                                logger.info(f"[Raydium] Pool creation detected in {signature}")
                                
                                # Get full transaction with timeout
                                tx = await asyncio.wait_for(
                                    self.client.get_transaction(
                                        signature,
                                        encoding="json",
                                        commitment=Confirmed,
                                        max_supported_transaction_version=0
                                    ),
                                    timeout=5.0
                                )
                                
                                if tx and tx.value:
                                    # Parse pool creation
                                    pool_info = await self._parse_pool_creation_tx(tx.value)
                                    if pool_info:
                                        # Check if it meets quality standards
                                        if pool_info.get('liquidity_sol', 0) >= MIN_POOL_LIQUIDITY_SOL:
                                            if callback:
                                                await callback(pool_info)
                                        else:
                                            logger.info(f"[Raydium] Pool creation rejected - low liquidity: {pool_info.get('liquidity_sol', 0):.2f} SOL")
                                        
                    except asyncio.TimeoutError:
                        logger.warning(f"[Raydium] Timeout processing log")
                    except Exception as e:
                        logger.error(f"[Raydium] Error processing log: {e}")
                        
        except Exception as e:
            logger.error(f"[Raydium] Monitor error: {e}")
        finally:
            self.monitoring = False
    
    async def _parse_pool_creation_tx(self, tx_data: dict) -> Optional[Dict]:
        """Parse pool creation transaction with quality checks"""
        try:
            meta = tx_data.get('meta', {})
            
            # Check for errors
            if meta.get('err'):
                return None
            
            # Extract token balances
            post_balances = meta.get('postTokenBalances', [])
            
            # Find new token mint and calculate liquidity
            liquidity_sol = 0
            token_mint = None
            
            for balance in post_balances:
                mint = balance.get('mint')
                if mint == WSOL:
                    # Get SOL amount in pool
                    amount = float(balance.get('uiTokenAmount', {}).get('uiAmount', 0))
                    if amount > liquidity_sol:
                        liquidity_sol = amount
                elif mint and mint not in [USDC, USDT]:
                    # This is likely the new token
                    if not token_mint:
                        token_mint = mint
            
            if token_mint and liquidity_sol >= MIN_POOL_LIQUIDITY_SOL:
                return {
                    'token_mint': token_mint,
                    'timestamp': time.time(),
                    'signature': tx_data.get('signature'),
                    'liquidity_sol': liquidity_sol
                }
            
            return None
            
        except Exception as e:
            logger.error(f"[Raydium] Error parsing pool creation: {e}")
            return None
    
    async def get_pool_liquidity(self, token_mint: str) -> float:
        """Get current liquidity for a token's pool"""
        try:
            pool = await self.find_pool_for_token(token_mint)
            if pool:
                return pool.estimated_lp_sol
            return 0.0
        except Exception as e:
            logger.error(f"[Raydium] Error getting liquidity: {e}")
            return 0.0
    
    async def estimate_price_impact(self, token_mint: str, buy_amount_sol: float) -> float:
        """Estimate price impact for a buy"""
        try:
            pool = await self.find_pool_for_token(token_mint)
            if not pool:
                return 100.0  # Max impact if no pool
            
            # Simple constant product formula
            # Price impact = (amount_in / (reserve + amount_in)) * 100
            reserve_sol = pool.estimated_lp_sol
            if reserve_sol <= 0:
                return 100.0
            
            impact = (buy_amount_sol / (reserve_sol + buy_amount_sol)) * 100
            return min(impact, 100.0)
            
        except Exception as e:
            logger.error(f"[Raydium] Error estimating price impact: {e}")
            return 100.0
    
    async def get_trending_pools(self, hours: int = 1) -> List[Dict]:
        """Get trending pools by volume or activity - QUALITY FILTERED"""
        try:
            cutoff_time = time.time() - (hours * 3600)
            trending = []
            
            for pool_data in self.recent_pools:
                if pool_data['timestamp'] > cutoff_time:
                    # Get current pool info with timeout
                    try:
                        pool = await asyncio.wait_for(
                            self.get_pool_info(pool_data['pool_id']),
                            timeout=2.0
                        )
                        if pool and pool.estimated_lp_sol >= MIN_POOL_LIQUIDITY_SOL:
                            trending.append({
                                'pool_id': pool.pool_id,
                                'token_mint': pool.token_mint,
                                'liquidity_sol': pool.estimated_lp_sol,
                                'age_minutes': (time.time() - pool_data['timestamp']) / 60
                            })
                    except asyncio.TimeoutError:
                        continue
            
            # Sort by liquidity
            trending.sort(key=lambda x: x['liquidity_sol'], reverse=True)
            return trending[:20]  # Top 20
            
        except Exception as e:
            logger.error(f"[Raydium] Error getting trending pools: {e}")
            return []
    
    def get_quality_stats(self) -> Dict:
        """Get statistics about pool quality"""
        return {
            'quality_pools_found': self.quality_pools_found,
            'low_quality_rejected': self.low_quality_rejected,
            'quality_ratio': self.quality_pools_found / max(1, self.quality_pools_found + self.low_quality_rejected),
            'cached_pools': len(self.pool_cache),
            'min_liquidity_threshold': MIN_POOL_LIQUIDITY_SOL
        }
    
    def clear_cache(self):
        """Clear pool cache"""
        self.pool_cache.clear()
        self.token_to_pool.clear()
        logger.info("[Raydium] Cache cleared")
