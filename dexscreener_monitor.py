"""
DEXScreener Fresh Pair Monitor
Polls DEXScreener API for new Solana pairs on Raydium/Meteora/Orca
"""

import asyncio
import logging
import time
from typing import Set, Dict, Optional, Callable
from datetime import datetime
import aiohttp

logger = logging.getLogger(__name__)


class DexScreenerMonitor:
    """Monitors DEXScreener for fresh Solana pairs"""
    
    def __init__(self, config):
        self.config = config
        self.dex_config = config.DEX_CONFIG
        self.seen_pairs: Set[str] = set()
        self.callback: Optional[Callable] = None
        self.running = False
        self.session: Optional[aiohttp.ClientSession] = None
        
        # Rate limiting
        self.last_request_time = 0
        self.min_request_interval = self.dex_config['poll_interval']
        
        # Statistics
        self.pairs_seen = 0
        self.pairs_filtered = 0
        self.pairs_passed = 0
        
    async def start(self, callback: Callable):
        """
        Start monitoring DEXScreener
        
        Args:
            callback: Async function to call when new pair found
                     Signature: async def callback(pair_data: dict)
        """
        self.callback = callback
        self.running = True
        self.session = aiohttp.ClientSession()
        
        logger.info("🔍 DEXScreener monitor started")
        logger.info(f"Filters: Min Liq=${self.dex_config['min_liquidity_usd']:,}, "
                   f"Max Age={self.dex_config['max_pair_age_seconds']}s, "
                   f"Min Vol=${self.dex_config['min_volume_5m']:,}")
        logger.info(f"DEXs: {', '.join(self.dex_config['allowed_dexs'])}")
        logger.info(f"Poll interval: {self.min_request_interval}s")
        
        try:
            while self.running:
                try:
                    await self._poll_dexscreener()
                    await asyncio.sleep(self.min_request_interval)
                except Exception as e:
                    logger.error(f"Poll error: {e}")
                    await asyncio.sleep(5)
        finally:
            if self.session:
                await self.session.close()
    
    async def _poll_dexscreener(self):
        """Poll DEXScreener API for fresh Solana pairs"""
        try:
            # Rate limiting
            elapsed = time.time() - self.last_request_time
            if elapsed < self.min_request_interval:
                return
            
            url = "https://api.dexscreener.com/latest/dex/pairs/solana"
            
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                self.last_request_time = time.time()
                
                if resp.status != 200:
                    logger.warning(f"DEXScreener API error: {resp.status}")
                    return
                
                data = await resp.json()
                pairs = data.get('pairs', [])
                
                if not pairs:
                    logger.debug("No pairs returned from DEXScreener")
                    return
                
                logger.debug(f"Received {len(pairs)} pairs from DEXScreener")
                
                # Process each pair
                for pair in pairs:
                    await self._process_pair(pair)
                    
        except asyncio.TimeoutError:
            logger.warning("DEXScreener API timeout")
        except Exception as e:
            logger.error(f"Failed to poll DEXScreener: {e}")
    
    async def _process_pair(self, pair: Dict):
        """Process a single pair from DEXScreener"""
        try:
            pair_address = pair.get('pairAddress')
            
            if not pair_address or pair_address in self.seen_pairs:
                return
            
            self.pairs_seen += 1
            
            # Apply filters
            if not self._apply_filters(pair):
                self.pairs_filtered += 1
                return
            
            # Mark as seen and pass to callback
            self.seen_pairs.add(pair_address)
            self.pairs_passed += 1
            
            # Extract relevant data
            token = pair.get('baseToken', {})
            pair_data = {
                'pair_address': pair_address,
                'token_address': token.get('address'),
                'token_name': token.get('name'),
                'token_symbol': token.get('symbol'),
                'dex_id': pair.get('dexId'),
                'liquidity_usd': float(pair.get('liquidity', {}).get('usd', 0)),
                'volume_5m': float(pair.get('volume', {}).get('m5', 0)),
                'price_usd': float(pair.get('priceUsd', 0)),
                'pair_created_at': pair.get('pairCreatedAt'),
                'txns_5m_buys': pair.get('txns', {}).get('m5', {}).get('buys', 0),
                'txns_5m_sells': pair.get('txns', {}).get('m5', {}).get('sells', 0),
                'price_change_5m': float(pair.get('priceChange', {}).get('m5', 0)),
                'source': 'dexscreener'
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
            logger.error(f"Failed to process pair: {e}")
    
    def _apply_filters(self, pair: Dict) -> bool:
        """Apply quality filters to pair"""
        try:
            # Extract data
            dex_id = pair.get('dexId', '').lower()
            liquidity = float(pair.get('liquidity', {}).get('usd', 0))
            volume_5m = float(pair.get('volume', {}).get('m5', 0))
            pair_created_at = pair.get('pairCreatedAt')
            txns = pair.get('txns', {}).get('m5', {})
            buys = txns.get('buys', 0)
            total_txns = txns.get('buys', 0) + txns.get('sells', 0)
            
            # Filter 1: DEX whitelist
            if dex_id not in self.dex_config['allowed_dexs']:
                logger.debug(f"Filtered: DEX {dex_id} not in whitelist")
                return False
            
            # Filter 2: Minimum liquidity
            if liquidity < self.dex_config['min_liquidity_usd']:
                logger.debug(f"Filtered: Liquidity ${liquidity:,.0f} < ${self.dex_config['min_liquidity_usd']:,}")
                return False
            
            # Filter 3: Pair age (freshness check)
            if pair_created_at:
                try:
                    # Parse timestamp (milliseconds)
                    created_timestamp = int(pair_created_at) / 1000
                    age_seconds = time.time() - created_timestamp
                    
                    if age_seconds > self.dex_config['max_pair_age_seconds']:
                        logger.debug(f"Filtered: Age {age_seconds:.0f}s > {self.dex_config['max_pair_age_seconds']}s")
                        return False
                except:
                    # If we can't parse age, skip (might be too old)
                    logger.debug("Filtered: Could not parse pair age")
                    return False
            
            # Filter 4: Minimum volume
            if volume_5m < self.dex_config['min_volume_5m']:
                logger.debug(f"Filtered: Volume ${volume_5m:,.0f} < ${self.dex_config['min_volume_5m']:,}")
                return False
            
            # Filter 5: Minimum transactions
            if total_txns < self.dex_config['min_txns_5m']:
                logger.debug(f"Filtered: Txns {total_txns} < {self.dex_config['min_txns_5m']}")
                return False
            
            # Filter 6: Minimum buys
            if buys < self.dex_config['min_buys_5m']:
                logger.debug(f"Filtered: Buys {buys} < {self.dex_config['min_buys_5m']}")
                return False
            
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
        logger.info(f"DEXScreener monitor stopped")
        logger.info(f"Stats: {stats['pairs_passed']} passed, {stats['pairs_filtered']} filtered ({stats['filter_rate']:.1f}%)")
