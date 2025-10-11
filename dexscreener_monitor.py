"""
GeckoTerminal Fresh Pair Monitor (DEXScreener Replacement)
Polls GeckoTerminal API for new Solana pairs on Raydium/Meteora/Orca
"""

import asyncio
import logging
import time
from typing import Set, Dict, Optional, Callable
from datetime import datetime
import aiohttp

logger = logging.getLogger(__name__)


class DexScreenerMonitor:
    """Monitors GeckoTerminal for fresh Solana pairs"""
    
    def __init__(self, config):
        self.config = config
        self.dex_config = config.DEX_CONFIG
        self.seen_pairs: Set[str] = set()
        self.callback: Optional[Callable] = None
        self.running = False
        self.session: Optional[aiohttp.ClientSession] = None
        
        # GeckoTerminal network ID for Solana
        self.network_id = "solana"
        
        # Map our DEX names to GeckoTerminal DEX IDs
        self.dex_mapping = {
            'raydium': 'raydium',
            'meteora': 'meteora',
            'orca': 'orca'
        }
        
        # Rate limiting
        self.last_request_time = 0
        self.min_request_interval = self.dex_config['poll_interval']
        
        # Statistics
        self.pairs_seen = 0
        self.pairs_filtered = 0
        self.pairs_passed = 0
        
    async def start(self, callback: Callable):
        """
        Start monitoring GeckoTerminal
        
        Args:
            callback: Async function to call when new pair found
        """
        self.callback = callback
        self.running = True
        self.session = aiohttp.ClientSession()
        
        logger.info("🔍 GeckoTerminal monitor started (DEXScreener replacement)")
        logger.info(f"Filters: Min Liq=${self.dex_config['min_liquidity_usd']:,}, "
                   f"Max Age={self.dex_config['max_pair_age_seconds']}s, "
                   f"Min Vol=${self.dex_config['min_volume_5m']:,}")
        logger.info(f"DEXs: {', '.join(self.dex_config['allowed_dexs'])}")
        logger.info(f"Poll interval: {self.min_request_interval}s")
        
        try:
            while self.running:
                try:
                    await self._poll_geckoterminal()
                    await asyncio.sleep(self.min_request_interval)
                except Exception as e:
                    logger.error(f"Poll error: {e}")
                    await asyncio.sleep(5)
        finally:
            if self.session:
                await self.session.close()
    
    async def _poll_geckoterminal(self):
        """Poll GeckoTerminal API for fresh Solana pairs"""
        try:
            # Rate limiting
            elapsed = time.time() - self.last_request_time
            if elapsed < self.min_request_interval:
                return
            
            # Get recently updated pools (sorted by pool_created_at desc)
            url = f"https://api.geckoterminal.com/api/v2/networks/{self.network_id}/new_pools"
            
            # Add headers as recommended by GeckoTerminal
            headers = {
                'Accept': 'application/json'
            }
            
            async with self.session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                self.last_request_time = time.time()
                
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.warning(f"GeckoTerminal API error {resp.status}: {error_text[:200]}")
                    return
                
                data = await resp.json()
                pools = data.get('data', [])
                
                if not pools:
                    logger.debug("No pools returned from GeckoTerminal")
                    return
                
                logger.debug(f"Received {len(pools)} new pools from GeckoTerminal")
                
                # Process each pool
                for pool in pools:
                    await self._process_pool(pool)
                    
        except asyncio.TimeoutError:
            logger.warning("GeckoTerminal API timeout")
        except Exception as e:
            logger.error(f"Failed to poll GeckoTerminal: {e}")
            import traceback
            logger.debug(traceback.format_exc())
    
    async def _process_pool(self, pool: Dict):
        """Process a single pool from GeckoTerminal"""
        try:
            pool_attrs = pool.get('attributes', {})
            pool_address = pool_attrs.get('address')
            
            if not pool_address or pool_address in self.seen_pairs:
                return
            
            self.pairs_seen += 1
            
            # Apply filters
            if not self._apply_filters(pool_attrs):
                self.pairs_filtered += 1
                return
            
            # Mark as seen and pass to callback
            self.seen_pairs.add(pool_address)
            self.pairs_passed += 1
            
            # Extract relevant data
            base_token = pool_attrs.get('base_token_price_usd', '0')
            
            pair_data = {
                'pair_address': pool_address,
                'token_address': pool_attrs.get('base_token_address'),
                'token_name': pool_attrs.get('name', '').split('/')[0].strip(),
                'token_symbol': pool_attrs.get('base_token_symbol', ''),
                'dex_id': pool_attrs.get('dex_id', '').lower(),
                'liquidity_usd': float(pool_attrs.get('reserve_in_usd', 0)),
                'volume_5m': float(pool_attrs.get('volume_usd', {}).get('m5', 0)),
                'price_usd': float(base_token) if base_token else 0,
                'pair_created_at': pool_attrs.get('pool_created_at'),
                'txns_5m_buys': pool_attrs.get('transactions', {}).get('m5', {}).get('buys', 0),
                'txns_5m_sells': pool_attrs.get('transactions', {}).get('m5', {}).get('sells', 0),
                'price_change_5m': float(pool_attrs.get('price_change_percentage', {}).get('m5', 0)),
                'source': 'geckoterminal'
            }
            
            logger.info("=" * 60)
            logger.info("🚀 NEW FRESH PAIR FOUND!")
            logger.info(f"Token: {pair_data['token_symbol']} ({pair_data['token_name']})")
            logger.info(f"Mint: {pair_data['token_address']}")
            logger.info(f"DEX: {pair_data['dex_id']}")
            logger.info(f"Liquidity: ${pair_data['liquidity_usd']:,.0f}")
            logger.info(f"Volume (5m): ${pair_data['volume_5m']:,.0f}")
            logger.info(f"Txns (5m): {pair_data['txns_5m_buys']} buys / {pair_data['txns_5m_sells']} sells")
            logger.info(f"Stats: {self.pairs_passed} passed / {self.pairs_filtered} filtered / {self.pairs_seen} total")
            logger.info("=" * 60)
            
            if self.callback:
                await self.callback(pair_data)
                
        except Exception as e:
            logger.error(f"Failed to process pool: {e}")
            import traceback
            logger.debug(traceback.format_exc())
    
    def _apply_filters(self, pool_attrs: Dict) -> bool:
        """Apply quality filters to pool"""
        try:
            # Extract data
            pool_name = pool_attrs.get('name', 'Unknown')
            pool_address = pool_attrs.get('address', '')[:8]
            dex_id = pool_attrs.get('dex_id', '').lower()
            liquidity = float(pool_attrs.get('reserve_in_usd', 0))
            volume_5m = float(pool_attrs.get('volume_usd', {}).get('m5', 0))
            pool_created_at = pool_attrs.get('pool_created_at')
            txns = pool_attrs.get('transactions', {}).get('m5', {})
            buys = txns.get('buys', 0)
            sells = txns.get('sells', 0)
            total_txns = buys + sells
            
            # Log every token we see
            logger.info(f"🔍 Checking: {pool_name} ({pool_address}...) | DEX: {dex_id} | Liq: ${liquidity:,.0f} | Vol(5m): ${volume_5m:,.0f} | Txns: {total_txns} (B:{buys}/S:{sells})")
            
            # Filter 1: DEX whitelist
            if dex_id not in self.dex_config['allowed_dexs']:
                logger.info(f"   ❌ FILTERED: DEX '{dex_id}' not in whitelist {self.dex_config['allowed_dexs']}")
                return False
            
            # Filter 2: Minimum liquidity
            if liquidity < self.dex_config['min_liquidity_usd']:
                logger.info(f"   ❌ FILTERED: Liquidity ${liquidity:,.0f} < ${self.dex_config['min_liquidity_usd']:,}")
                return False
            
            # Filter 3: Pool age (freshness check)
            age_seconds = None
            if pool_created_at:
                try:
                    # Parse ISO timestamp
                    from datetime import datetime
                    created_dt = datetime.fromisoformat(pool_created_at.replace('Z', '+00:00'))
                    age_seconds = (datetime.now(created_dt.tzinfo) - created_dt).total_seconds()
                    
                    if age_seconds > self.dex_config['max_pair_age_seconds']:
                        logger.info(f"   ❌ FILTERED: Age {age_seconds:.0f}s > {self.dex_config['max_pair_age_seconds']}s (too old)")
                        return False
                except Exception as e:
                    logger.info(f"   ❌ FILTERED: Could not parse pool age: {e}")
                    return False
            
            # Filter 4: Minimum volume
            if volume_5m < self.dex_config['min_volume_5m']:
                logger.info(f"   ❌ FILTERED: Volume ${volume_5m:,.0f} < ${self.dex_config['min_volume_5m']:,}")
                return False
            
            # Filter 5: Minimum transactions
            if total_txns < self.dex_config['min_txns_5m']:
                logger.info(f"   ❌ FILTERED: Txns {total_txns} < {self.dex_config['min_txns_5m']}")
                return False
            
            # Filter 6: Minimum buys
            if buys < self.dex_config['min_buys_5m']:
                logger.info(f"   ❌ FILTERED: Buys {buys} < {self.dex_config['min_buys_5m']}")
                return False
            
            # PASSED ALL FILTERS
            age_str = f"{age_seconds:.0f}s" if age_seconds else "unknown"
            logger.info(f"   ✅ PASSED ALL FILTERS! Age: {age_str}")
            return True
            
        except Exception as e:
            logger.error(f"Filter error: {e}")
            return False
    
    def get_stats(self) -> Dict:
        """Get monitor statistics"""
        return {
            'pairs_seen': self.pairs_seen,
            'pairs_filtered': self.pairs_filtered,
            'pairs_passed': self.pairs_passed,
            'filter_rate': (self.pairs_filtered / self.pairs_seen * 100) if self.pairs_seen > 0 else 0
        }
    
    def stop(self):
        """Stop monitoring"""
        self.running = False
        stats = self.get_stats()
        logger.info(f"GeckoTerminal monitor stopped")
        logger.info(f"Stats: {stats['pairs_passed']} passed, {stats['pairs_filtered']} filtered ({stats['filter_rate']:.1f}%)")
