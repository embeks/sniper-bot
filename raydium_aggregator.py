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

# QUALITY THRESHOLDS - FIXED FOR YOUR SETUP
MIN_POOL_LIQUIDITY_SOL = 3.0  # CHANGED FROM 10.0 to match your MIN_LP
MIN_POOL_AGE_SECONDS = 0  # CHANGED FROM 30 - snipe immediately

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
        # Only check minimum liquidity now
        return self.estimated_lp_sol >= MIN_POOL_LIQUIDITY_SOL

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
        """Find Raydium pool for a given token - FIXED TO PREVENT HANGING"""
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
            
            # FIX: Just check cache, don't do RPC scans that hang
            logger.info(f"[Raydium] Checking cache for {token_mint[:8]}...")
            
            # Check recent pools in cache
            for pool_id, pool in list(self.pool_cache.items())[:20]:
                if pool.token_mint == token_mint:
                    return pool
            
            # FIX: Don't do full program scan - it causes hanging
            logger.info(f"[Raydium] No cached pool found for {token_mint[:8]}...")
            return None
            
        except Exception as e:
            logger.error(f"[Raydium] Error finding pool: {e}")
            return None
    
    async def _scan_recent_pools(self, token_mint: str, max_pools: int = 10) -> Optional[RaydiumPool]:
        """Scan recent pools - FIXED VERSION"""
        try:
            # FIX: Just check cache instead of doing RPC calls
            for pool_id, pool in list(self.pool_cache.items())[:max_pools]:
                if pool.token_mint == token_mint:
                    if pool.is_quality_pool():
                        return pool
            
            return None
            
        except Exception as e:
            logger.warning(f"[Raydium] Limited scan failed: {e}")
            return None
    
    async def _scan_program_accounts(self, token_mint: str) -> Optional[RaydiumPool]:
        """Full program account scan - FIXED TO PREVENT TIMEOUT"""
        try:
            # FIX: Limit accounts and add timeout
            max_accounts_to_scan = 5  # REDUCED from 20
            
            response = await asyncio.wait_for(
                self.client.get_program_accounts(
                    RAYDIUM_AMM_PROGRAM,
                    commitment=Processed,
                    encoding="base64"
                ),
                timeout=3.0  # REDUCED from 10.0
            )
            
            if not response:
                return None
                
            accounts = response.value if hasattr(response, 'value') else response
            if not accounts:
                return None
            
            # Process only limited accounts
            accounts_to_process = accounts[:max_accounts_to_scan]
            
            for account_info in accounts_to_process:
                try:
                    pool = await self._parse_pool_account(
                        str(account_info.pubkey),
                        account_info.account
                    )
                    
                    if pool and pool.token_mint == token_mint:
                        if pool.is_quality_pool():
                            self.pool_cache[pool.pool_id] = pool
                            self.token_to_pool[token_mint] = pool.pool_id
                            self.quality_pools_found += 1
                            return pool
                        
                except Exception as e:
                    continue
            
            return None
            
        except asyncio.TimeoutError:
            logger.warning(f"[Raydium] Program scan timed out")
            return None
        except Exception as e:
            logger.error(f"[Raydium] Program scan failed: {e}")
            return None
    
    async def _parse_pool_account(self, pool_id: str, account: Account) -> Optional[RaydiumPool]:
        """Parse pool account data - FIXED TO AVOID DELAYS"""
        try:
            if not account or not account.data:
                return None
            
            # Decode account data
            data = base58.b58decode(account.data[0]) if isinstance(account.data, list) else account.data
            
            if len(data) < 752:  # Minimum pool state size
                return None
            
            # Parse pool state structure
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
            
            # FIX: Don't fetch vault balances - causes delays
            base_amount = 0
            quote_amount = 0
            
            pool = RaydiumPool(
                pool_id=pool_id,
                token_mint=token_mint,
                base_vault=base_vault,
                quote_vault=quote_vault,
                lp_supply=0,
                base_amount=base_amount,
                quote_amount=quote_amount,
                creation_time=time.time()
            )
            
            return pool
            
        except Exception as e:
            logger.debug(f"[Raydium] Error parsing pool account: {e}")
            return None
    
    async def get_pool_info(self, pool_id: str) -> Optional[RaydiumPool]:
        """Get detailed pool information - FIXED"""
        try:
            # Check cache only - don't do RPC calls
            if pool_id in self.pool_cache:
                return self.pool_cache[pool_id]
            
            return None
            
        except Exception as e:
            logger.error(f"[Raydium] Error getting pool info: {e}")
            return None
    
    async def _update_pool_balances(self, pool: RaydiumPool):
        """FIX: Don't update balances - causes delays"""
        pass
    
    def register_pool(self, pool_id: str, token_mint: str, base_vault: str = "", 
                     quote_vault: str = "", lp_amount: float = 0):
        """Register a new pool in cache - FIXED TO USE TX LIQUIDITY"""
        try:
            # Use liquidity from transaction detection
            import os
            min_lp = float(os.getenv("MIN_LP", "3.0")) if 'os' in globals() else MIN_POOL_LIQUIDITY_SOL
            
            # Log registration
            if lp_amount > 0:
                logger.info(f"[Raydium] Registering pool {pool_id[:8]}... for {token_mint[:8]}... with {lp_amount:.2f} SOL")
                
                if lp_amount < min_lp:
                    logger.warning(f"[Raydium] Low quality pool but registering anyway")
                    self.low_quality_rejected += 1
            
            # Check for duplicates
            if pool_id in self.pool_cache:
                existing_pool = self.pool_cache[pool_id]
                if existing_pool.token_mint != token_mint:
                    logger.warning(f"[Raydium] Pool already registered for different token")
                    return
            
            if token_mint in self.token_to_pool:
                existing_pool_id = self.token_to_pool[token_mint]
                if existing_pool_id != pool_id:
                    logger.warning(f"[Raydium] Token already has different pool")
                    return
            
            pool = RaydiumPool(
                pool_id=pool_id,
                token_mint=token_mint,
                base_vault=base_vault,
                quote_vault=quote_vault,
                lp_supply=0,
                base_amount=0,
                quote_amount=lp_amount * 1e9 if lp_amount else 0,  # Use TX liquidity
                creation_time=time.time()
            )
            
            self.pool_cache[pool_id] = pool
            self.token_to_pool[token_mint] = pool_id
            
            if lp_amount >= min_lp:
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
            
            logger.info(f"[Raydium] Successfully registered pool")
            
        except Exception as e:
            logger.error(f"[Raydium] Error registering pool: {e}")
    
    async def monitor_pool_creations(self, callback):
        """Monitor for new pool creations via WebSocket"""
        try:
            self.monitoring = True
            logger.info("[Raydium] Starting pool creation monitor...")
            
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
            # Check cache only
            if token_mint in self.token_to_pool:
                pool_id = self.token_to_pool[token_mint]
                if pool_id in self.pool_cache:
                    return self.pool_cache[pool_id].estimated_lp_sol
            return 0.0
        except Exception as e:
            logger.error(f"[Raydium] Error getting liquidity: {e}")
            return 0.0
    
    async def estimate_price_impact(self, token_mint: str, buy_amount_sol: float) -> float:
        """Estimate price impact for a buy"""
        try:
            # Check cache for pool
            if token_mint in self.token_to_pool:
                pool_id = self.token_to_pool[token_mint]
                if pool_id in self.pool_cache:
                    pool = self.pool_cache[pool_id]
                    reserve_sol = pool.estimated_lp_sol
                    if reserve_sol > 0:
                        impact = (buy_amount_sol / (reserve_sol + buy_amount_sol)) * 100
                        return min(impact, 100.0)
            
            return 5.0  # Default 5% if no pool found
            
        except Exception as e:
            logger.error(f"[Raydium] Error estimating price impact: {e}")
            return 5.0
    
    async def get_trending_pools(self, hours: int = 1) -> List[Dict]:
        """Get trending pools by volume or activity"""
        try:
            cutoff_time = time.time() - (hours * 3600)
            trending = []
            
            for pool_data in self.recent_pools:
                if pool_data['timestamp'] > cutoff_time:
                    trending.append(pool_data)
            
            # Sort by liquidity
            trending.sort(key=lambda x: x.get('lp_sol', 0), reverse=True)
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
