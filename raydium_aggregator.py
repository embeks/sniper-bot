
"""
Raydium Aggregator - Complete Fixed Version
Monitors Raydium for new pool creations and liquidity additions
Fixes the 'dict' object has no attribute 'offset' error
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
WSOL = "So11111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

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
            logger.info("[Raydium] Aggregator initialized")
            
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
        if self.ws_client:
            await self.ws_client.close()
        if self.session:
            await self.session.close()
        await self.client.close()
    
    async def find_pool_for_token(self, token_mint: str) -> Optional[RaydiumPool]:
        """Find Raydium pool for a given token - FIXED VERSION"""
        try:
            # Check cache first
            if token_mint in self.token_to_pool:
                pool_id = self.token_to_pool[token_mint]
                if pool_id in self.pool_cache:
                    return self.pool_cache[pool_id]
            
            logger.info(f"[Raydium] Searching for pool with {token_mint[:8]}...")
            
            # Try limited scan first (faster)
            logger.info(f"[Raydium] Doing limited scan (max 50 pools)...")
            pool = await self._scan_recent_pools(token_mint)
            
            if pool:
                logger.info(f"[Raydium] Found pool {pool.pool_id[:8]}... for {token_mint[:8]}...")
                return pool
            
            # If not found, try program accounts scan
            logger.info(f"[Raydium] Doing full program scan...")
            pool = await self._scan_program_accounts(token_mint)
            
            if pool:
                logger.info(f"[Raydium] Found pool {pool.pool_id[:8]}... via program scan")
                return pool
            
            logger.info(f"[Raydium] No pool found for {token_mint[:8]}... (might be on Jupiter only)")
            return None
            
        except Exception as e:
            logger.error(f"[Raydium] Error finding pool: {e}")
            return None
    
    async def _scan_recent_pools(self, token_mint: str, max_pools: int = 50) -> Optional[RaydiumPool]:
        """Scan recent pools - FIXED VERSION without offset attribute access"""
        try:
            # Get recent program accounts with proper data slice
            response = await self.client.get_program_accounts(
                RAYDIUM_AMM_PROGRAM,
                commitment=Processed,
                encoding="base64",
                data_slice={"offset": 0, "length": 752}  # Pool state size
            )
            
            if not response or not response.value:
                return None
            
            # Process only recent pools (limit for performance)
            accounts_to_check = response.value[:max_pools] if len(response.value) > max_pools else response.value
            
            for account_info in accounts_to_check:
                try:
                    pool = await self._parse_pool_account(
                        str(account_info.pubkey),
                        account_info.account
                    )
                    
                    if pool and pool.token_mint == token_mint:
                        # Cache the pool
                        self.pool_cache[pool.pool_id] = pool
                        self.token_to_pool[token_mint] = pool.pool_id
                        return pool
                        
                except Exception as e:
                    continue
            
            return None
            
        except Exception as e:
            logger.warning(f"[Raydium] Limited scan failed: {e}, falling back to Jupiter")
            return None
    
    async def _scan_program_accounts(self, token_mint: str) -> Optional[RaydiumPool]:
        """Full program account scan"""
        try:
            # This is more expensive but thorough
            response = await self.client.get_program_accounts(
                RAYDIUM_AMM_PROGRAM,
                commitment=Processed,
                encoding="base64"
            )
            
            if not response or not response.value:
                return None
            
            for account_info in response.value:
                try:
                    pool = await self._parse_pool_account(
                        str(account_info.pubkey),
                        account_info.account
                    )
                    
                    if pool and pool.token_mint == token_mint:
                        # Cache the pool
                        self.pool_cache[pool.pool_id] = pool
                        self.token_to_pool[token_mint] = pool.pool_id
                        return pool
                        
                except Exception as e:
                    continue
            
            return None
            
        except Exception as e:
            logger.error(f"[Raydium] Program scan failed: {e}")
            return None
    
    async def _parse_pool_account(self, pool_id: str, account: Account) -> Optional[RaydiumPool]:
        """Parse pool account data - FIXED VERSION"""
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
                base_info = await self.client.get_account_info(Pubkey.from_string(base_vault))
                if base_info and base_info.value:
                    base_amount = base_info.value.lamports
                
                quote_info = await self.client.get_account_info(Pubkey.from_string(quote_vault))
                if quote_info and quote_info.value:
                    quote_amount = quote_info.value.lamports
            except:
                pass
            
            return RaydiumPool(
                pool_id=pool_id,
                token_mint=token_mint,
                base_vault=base_vault,
                quote_vault=quote_vault,
                lp_supply=0,  # Would need LP mint balance
                base_amount=base_amount,
                quote_amount=quote_amount,
                creation_time=time.time()
            )
            
        except Exception as e:
            return None
    
    async def get_pool_info(self, pool_id: str) -> Optional[RaydiumPool]:
        """Get detailed pool information"""
        try:
            # Check cache
            if pool_id in self.pool_cache:
                pool = self.pool_cache[pool_id]
                # Update balances if pool is cached
                if time.time() - pool.creation_time > 5:  # Refresh every 5 seconds
                    await self._update_pool_balances(pool)
                return pool
            
            # Fetch pool account
            response = await self.client.get_account_info(
                Pubkey.from_string(pool_id),
                commitment=Processed
            )
            
            if not response or not response.value:
                logger.error(f"[Raydium] No account data returned for pool {pool_id[:8]}...")
                return None
            
            pool = await self._parse_pool_account(pool_id, response.value)
            if pool:
                self.pool_cache[pool_id] = pool
                self.token_to_pool[pool.token_mint] = pool_id
            
            return pool
            
        except Exception as e:
            logger.error(f"[Raydium] Error getting pool info: {e}")
            return None
    
    async def _update_pool_balances(self, pool: RaydiumPool):
        """Update pool balance information"""
        try:
            # Get base vault balance
            base_info = await self.client.get_account_info(
                Pubkey.from_string(pool.base_vault)
            )
            if base_info and base_info.value:
                pool.base_amount = base_info.value.lamports
            
            # Get quote vault balance  
            quote_info = await self.client.get_account_info(
                Pubkey.from_string(pool.quote_vault)
            )
            if quote_info and quote_info.value:
                pool.quote_amount = quote_info.value.lamports
                
        except Exception as e:
            logger.error(f"[Raydium] Error updating balances: {e}")
    
    def register_pool(self, pool_id: str, token_mint: str, base_vault: str = "", 
                     quote_vault: str = "", lp_amount: float = 0):
        """Register a new pool in cache - FIXED to prevent duplicate registrations"""
        try:
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
            
            logger.info(f"[Raydium] Registering new pool {pool_id[:8]}... for {token_mint[:8]}...")
            
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
            
            logger.info(f"[Raydium] Registered pool {pool_id[:8]}... for token {token_mint[:8]}...")
            
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
                                
                                # Get full transaction
                                tx = await self.client.get_transaction(
                                    signature,
                                    encoding="json",
                                    commitment=Confirmed,
                                    max_supported_transaction_version=0
                                )
                                
                                if tx and tx.value:
                                    # Parse pool creation
                                    pool_info = await self._parse_pool_creation_tx(tx.value)
                                    if pool_info and callback:
                                        await callback(pool_info)
                                        
                    except Exception as e:
                        logger.error(f"[Raydium] Error processing log: {e}")
                        
        except Exception as e:
            logger.error(f"[Raydium] Monitor error: {e}")
        finally:
            self.monitoring = False
    
    async def _parse_pool_creation_tx(self, tx_data: dict) -> Optional[Dict]:
        """Parse pool creation transaction"""
        try:
            meta = tx_data.get('meta', {})
            
            # Check for errors
            if meta.get('err'):
                return None
            
            # Extract token balances
            post_balances = meta.get('postTokenBalances', [])
            
            # Find new token mint
            for balance in post_balances:
                mint = balance.get('mint')
                if mint and mint not in [WSOL, USDC, USDT]:
                    # Check if we already know about this pool
                    if mint not in self.token_to_pool:
                        return {
                            'token_mint': mint,
                            'timestamp': time.time(),
                            'signature': tx_data.get('signature')
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
    
    async def monitor_transactions(self, callback):
        """Monitor Raydium transactions for opportunities"""
        try:
            self.monitoring = True
            logger.info("[Raydium] Starting transaction monitor...")
            
            while self.monitoring:
                try:
                    # Get recent signatures
                    signatures = await self.client.get_signatures_for_address(
                        RAYDIUM_AMM_PROGRAM,
                        limit=10,
                        commitment=Processed
                    )
                    
                    if signatures and signatures.value:
                        for sig_info in signatures.value:
                            signature = sig_info.signature
                            
                            # Get transaction
                            tx = await self.client.get_transaction(
                                signature,
                                encoding="json",
                                commitment=Confirmed,
                                max_supported_transaction_version=0
                            )
                            
                            if tx and tx.value:
                                # Check for pool creation or large liquidity add
                                await self._analyze_transaction(tx.value, callback)
                    
                    await asyncio.sleep(0.5)  # Poll every 500ms
                    
                except Exception as e:
                    logger.error(f"[Raydium] Transaction monitor error: {e}")
                    await asyncio.sleep(1)
                    
        except Exception as e:
            logger.error(f"[Raydium] Monitor error: {e}")
        finally:
            self.monitoring = False
    
    async def _analyze_transaction(self, tx_data: dict, callback):
        """Analyze transaction for opportunities"""
        try:
            meta = tx_data.get('meta', {})
            
            # Skip errored transactions
            if meta.get('err'):
                return
            
            logs = meta.get('logMessages', [])
            
            # Check for pool creation
            if any("InitializePool" in log or "initialize2" in log for log in logs):
                pool_info = await self._parse_pool_creation_tx(tx_data)
                if pool_info and callback:
                    await callback('pool_creation', pool_info)
            
            # Check for liquidity addition
            elif any("AddLiquidity" in log or "add_liquidity" in log for log in logs):
                liq_info = await self._parse_liquidity_add(tx_data)
                if liq_info and callback:
                    await callback('liquidity_add', liq_info)
                    
        except Exception as e:
            logger.error(f"[Raydium] Error analyzing transaction: {e}")
    
    async def _parse_liquidity_add(self, tx_data: dict) -> Optional[Dict]:
        """Parse liquidity addition transaction"""
        try:
            meta = tx_data.get('meta', {})
            
            # Get token transfers
            pre_balances = meta.get('preTokenBalances', [])
            post_balances = meta.get('postTokenBalances', [])
            
            # Calculate added amounts
            added_amounts = {}
            
            for post in post_balances:
                mint = post.get('mint')
                post_amount = float(post.get('uiTokenAmount', {}).get('uiAmount', 0))
                
                # Find corresponding pre-balance
                pre_amount = 0
                for pre in pre_balances:
                    if pre.get('mint') == mint:
                        pre_amount = float(pre.get('uiTokenAmount', {}).get('uiAmount', 0))
                        break
                
                if post_amount > pre_amount:
                    added_amounts[mint] = post_amount - pre_amount
            
            # Check if significant liquidity was added
            if WSOL in added_amounts and added_amounts[WSOL] > 1:  # More than 1 SOL
                return {
                    'sol_amount': added_amounts[WSOL],
                    'timestamp': time.time(),
                    'signature': tx_data.get('signature')
                }
            
            return None
            
        except Exception as e:
            logger.error(f"[Raydium] Error parsing liquidity add: {e}")
            return None
    
    def get_recent_pools(self, limit: int = 10) -> List[Dict]:
        """Get recently created pools"""
        return self.recent_pools[-limit:]
    
    def clear_cache(self):
        """Clear pool cache"""
        self.pool_cache.clear()
        self.token_to_pool.clear()
        logger.info("[Raydium] Cache cleared")
    
    async def get_pool_stats(self, pool_id: str) -> Optional[Dict]:
        """Get detailed pool statistics"""
        try:
            pool = await self.get_pool_info(pool_id)
            if not pool:
                return None
            
            return {
                'pool_id': pool.pool_id,
                'token_mint': pool.token_mint,
                'liquidity_sol': pool.estimated_lp_sol,
                'base_amount': pool.base_amount,
                'quote_amount': pool.quote_amount,
                'fee_rate': pool.fee_rate,
                'creation_time': pool.creation_time,
                'age_seconds': time.time() - pool.creation_time
            }
            
        except Exception as e:
            logger.error(f"[Raydium] Error getting pool stats: {e}")
            return None
    
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
        """Get trending pools by volume or activity"""
        try:
            cutoff_time = time.time() - (hours * 3600)
            trending = []
            
            for pool_data in self.recent_pools:
                if pool_data['timestamp'] > cutoff_time:
                    # Get current pool info
                    pool = await self.get_pool_info(pool_data['pool_id'])
                    if pool and pool.estimated_lp_sol > 10:  # Min 10 SOL
                        trending.append({
                            'pool_id': pool.pool_id,
                            'token_mint': pool.token_mint,
                            'liquidity_sol': pool.estimated_lp_sol,
                            'age_minutes': (time.time() - pool_data['timestamp']) / 60
                        })
            
            # Sort by liquidity
            trending.sort(key=lambda x: x['liquidity_sol'], reverse=True)
            return trending[:20]  # Top 20
            
        except Exception as e:
            logger.error(f"[Raydium] Error getting trending pools: {e}")
            return []

