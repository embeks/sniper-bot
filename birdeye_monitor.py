"""
Birdeye Fresh Pair Monitor
Polls Birdeye API for new Solana pairs on Raydium/Meteora/Orca
"""

import asyncio
import logging
import time
from typing import Set, Dict, Optional, Callable
from datetime import datetime
import aiohttp

logger = logging.getLogger(__name__)


class BirdeyeMonitor:
    """Monitors Birdeye for fresh Solana pairs"""
    
    def __init__(self, config):
        self.config = config
        self.birdeye_config = config.BIRDEYE_CONFIG
        self.api_key = config.BIRDEYE_API_KEY
        self.seen_pairs: Set[str] = set()
        self.callback: Optional[Callable] = None
        self.running = False
        self.session: Optional[aiohttp.ClientSession] = None
        
        # Rate limiting
        self.last_request_time = 0
        self.min_request_interval = self.birdeye_config['poll_interval']
        
        # Statistics
        self.pairs_seen = 0
        self.pairs_filtered = 0
        self.pairs_passed = 0
        
        if not self.api_key:
            raise ValueError("BIRDEYE_API_KEY not found in config")
        
    async def start(self, callback: Callable):
        """
        Start monitoring Birdeye
        
        Args:
            callback: Async function to call when new pair found
        """
        self.callback = callback
        self.running = True
        self.session = aiohttp.ClientSession()
        
        logger.info("🔍 Birdeye monitor started")
        logger.info(f"Filters: Min Liq=${self.birdeye_config['min_liquidity_usd']:,}, "
                   f"Max Age={self.birdeye_config['max_pair_age_seconds']}s, "
                   f"Min Vol=${self.birdeye_config['min_volume_5m']:,}")
        logger.info(f"DEXs: {', '.join(self.birdeye_config['allowed_dexs'])}")
        logger.info(f"Poll interval: {self.min_request_interval}s")
        
        try:
            while self.running:
                try:
                    await self._poll_birdeye()
                    await asyncio.sleep(self.min_request_interval)
                except Exception as e:
                    logger.error(f"Poll error: {e}")
                    await asyncio.sleep(5)
        finally:
            if self.session:
                await self.session.close()
    
    async def _poll_birdeye(self):
        """Poll Birdeye for fresh SOL tokens (SIMPLE TEST VERSION)."""
        try:
            # Simple rate limiting
            if time.time() - self.last_request_time < self.min_request_interval:
                return

            # CHANGED: Use v3/token/list with sort by recent listing
            url = "https://public-api.birdeye.so/defi/v3/token/list?sort_by=listing_time&sort_type=desc&limit=20&offset=0"
            
            headers = {
                "X-API-KEY": self.api_key,
                "x-chain": "solana",
                "accept": "application/json",
            }
            
            logger.info(f"🔍 Trying v3/token/list (sorted by listing_time)")
            
            async with self.session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                self.last_request_time = time.time()
                
                logger.info(f"📊 Response status: {resp.status}")
                
                if resp.status == 401:
                    logger.error("❌ Birdeye API authentication failed (401) – check your API key")
                    return
                    
                if resp.status == 429:
                    logger.warning("⚠️ Birdeye rate limit (429) – backing off 10s")
                    await asyncio.sleep(10)
                    return
                    
                if resp.status != 200:
                    txt = await resp.text()
                    logger.error(f"❌ Birdeye API error {resp.status}: {txt[:300]}")
                    return
                    
                data = await resp.json()
                logger.info(f"✅ Got response with keys: {list(data.keys())}")
                
                if not data or data.get("success") is False:
                    logger.warning(f"Birdeye returned success=false or empty: {data}")
                    return
                
                # token_trending returns tokens under data.tokens (NOT data.items!)
                items = data.get('data', {}).get('tokens', []) or []
                
                logger.info(f"📦 Raw data keys: {list(data.keys())}")
                if 'data' in data:
                    logger.info(f"📦 Data keys: {list(data['data'].keys())}")
                
                if not items:
                    logger.info("No tokens in response")
                    return
                    
                logger.info(f"✅ Birdeye returned {len(items)} tokens!")
                logger.info(f"First token: {items[0] if items else 'none'}")
                
                # Process tokens
                for raw in items:
                    await self._process_token_simple(raw)
                    
        except asyncio.TimeoutError:
            logger.warning("Birdeye API timeout")
        except Exception as e:
            logger.error(f"Failed to poll Birdeye: {e}")
            import traceback
            logger.error(traceback.format_exc())
    
    async def _process_token_simple(self, token: Dict):
        """Simple token processing for testing"""
        try:
            address = token.get('address') or token.get('mint') or ''
            if not address or address in self.seen_pairs:
                return
                
            self.seen_pairs.add(address)
            self.pairs_seen += 1
            
            symbol = token.get('symbol', 'UNKNOWN')
            logger.info(f"  📍 Token {self.pairs_seen}: {symbol} ({address[:8]}...)")
            
        except Exception as e:
            logger.error(f"Error processing token: {e}")
    
    async def _process_token(self, token: Dict):
        """Process a single token from Birdeye"""
        try:
            token_address = token.get('address')
            
            if not token_address or token_address in self.seen_pairs:
                return
            
            self.pairs_seen += 1
            
            # Apply filters
            if not self._apply_filters(token):
                self.pairs_filtered += 1
                return
            
            # Mark as seen and pass to callback
            self.seen_pairs.add(token_address)
            self.pairs_passed += 1
            
            # Extract relevant data - defensive parsing
            liquidity_usd = 0
            volume_5m = 0
            buys_5m = 0
            sells_5m = 0
            
            # Try multiple possible field names for liquidity
            if 'liquidity' in token:
                liq = token['liquidity']
                if isinstance(liq, dict):
                    liquidity_usd = float(liq.get('usd', 0) or liq.get('v24hUSD', 0))
                else:
                    liquidity_usd = float(liq or 0)
            
            # Try multiple possible field names for volume/trades
            if 'trade' in token:
                trade = token['trade']
                volume_5m = float(trade.get('v5m', 0))
                buys_5m = int(trade.get('b5m', 0))
                sells_5m = int(trade.get('s5m', 0))
            elif 'volume' in token:
                vol = token['volume']
                if isinstance(vol, dict):
                    volume_5m = float(vol.get('m5', 0) or vol.get('v5m', 0))
            
            if 'txns' in token:
                txns = token['txns']
                if isinstance(txns, dict):
                    m5 = txns.get('m5', {})
                    if isinstance(m5, dict):
                        buys_5m = int(m5.get('buys', 0) or m5.get('b5m', 0))
                        sells_5m = int(m5.get('sells', 0) or m5.get('s5m', 0))
            
            pair_data = {
                'pair_address': token_address,
                'token_address': token_address,
                'token_name': token.get('name', ''),
                'token_symbol': token.get('symbol', ''),
                'dex_id': token.get('source', '').lower(),
                'liquidity_usd': liquidity_usd,
                'volume_5m': volume_5m,
                'price_usd': float(token.get('price', 0)),
                'pair_created_at': token.get('createdAt'),
                'txns_5m_buys': buys_5m,
                'txns_5m_sells': sells_5m,
                'price_change_5m': float(token.get('priceChange', {}).get('m5', 0) if isinstance(token.get('priceChange'), dict) else 0),
                'source': 'birdeye'
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
            logger.error(f"Failed to process token: {e}")
            import traceback
            logger.debug(traceback.format_exc())
    
    def _apply_filters(self, token: Dict) -> bool:
        """Apply quality filters to token"""
        try:
            # Extract data - defensive parsing
            token_name = token.get('name', 'Unknown')
            token_address = token.get('address', '')[:8]
            source = token.get('source', '').lower()
            
            # Parse liquidity - try multiple field names
            liquidity_usd = 0
            if 'liquidity' in token:
                liq = token['liquidity']
                if isinstance(liq, dict):
                    liquidity_usd = float(liq.get('usd', 0) or liq.get('v24hUSD', 0))
                else:
                    liquidity_usd = float(liq or 0)
            
            # Parse volume - try multiple field names
            volume_5m = 0
            if 'trade' in token:
                volume_5m = float(token['trade'].get('v5m', 0))
            elif 'volume' in token:
                vol = token['volume']
                if isinstance(vol, dict):
                    volume_5m = float(vol.get('m5', 0) or vol.get('v5m', 0))
            
            created_at = token.get('createdAt')
            
            # Parse txns - try multiple field names
            buys_5m = 0
            sells_5m = 0
            if 'trade' in token:
                trade = token['trade']
                buys_5m = int(trade.get('b5m', 0))
                sells_5m = int(trade.get('s5m', 0))
            elif 'txns' in token:
                txns = token['txns']
                if isinstance(txns, dict):
                    m5 = txns.get('m5', {})
                    if isinstance(m5, dict):
                        buys_5m = int(m5.get('buys', 0) or m5.get('b5m', 0))
                        sells_5m = int(m5.get('sells', 0) or m5.get('s5m', 0))
            
            total_txns = buys_5m + sells_5m
            
            # Log every token we see
            logger.info(f"🔍 Checking: {token_name} ({token_address}...) | DEX: {source} | Liq: ${liquidity_usd:,.0f} | Vol(5m): ${volume_5m:,.0f} | Txns: {total_txns} (B:{buys_5m}/S:{sells_5m})")
            
            # Filter 1: DEX whitelist (partial match)
            dex_allowed = False
            for allowed_dex in self.birdeye_config['allowed_dexs']:
                if allowed_dex in source:
                    dex_allowed = True
                    break
            
            if not dex_allowed:
                logger.info(f"   ❌ FILTERED: DEX '{source}' not in whitelist {self.birdeye_config['allowed_dexs']}")
                return False
            
            # Filter 2: Minimum liquidity
            if liquidity_usd < self.birdeye_config['min_liquidity_usd']:
                logger.info(f"   ❌ FILTERED: Liquidity ${liquidity_usd:,.0f} < ${self.birdeye_config['min_liquidity_usd']:,}")
                return False
            
            # Filter 3: Pair age (freshness check)
            age_seconds = None
            if created_at:
                try:
                    # Birdeye gives Unix timestamp in milliseconds
                    created_timestamp = int(created_at) / 1000
                    age_seconds = time.time() - created_timestamp
                    
                    if age_seconds > self.birdeye_config['max_pair_age_seconds']:
                        logger.info(f"   ❌ FILTERED: Age {age_seconds:.0f}s > {self.birdeye_config['max_pair_age_seconds']}s (too old)")
                        return False
                except Exception as e:
                    logger.info(f"   ❌ FILTERED: Could not parse token age: {e}")
                    return False
            
            # Filter 4: Minimum volume
            if volume_5m < self.birdeye_config['min_volume_5m']:
                logger.info(f"   ❌ FILTERED: Volume ${volume_5m:,.0f} < ${self.birdeye_config['min_volume_5m']:,}")
                return False
            
            # Filter 5: Minimum transactions
            if total_txns < self.birdeye_config['min_txns_5m']:
                logger.info(f"   ❌ FILTERED: Txns {total_txns} < {self.birdeye_config['min_txns_5m']}")
                return False
            
            # Filter 6: Minimum buys
            if buys_5m < self.birdeye_config['min_buys_5m']:
                logger.info(f"   ❌ FILTERED: Buys {buys_5m} < {self.birdeye_config['min_buys_5m']}")
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
        logger.info(f"Birdeye monitor stopped")
        logger.info(f"Stats: {stats['pairs_passed']} passed, {stats['pairs_filtered']} filtered ({stats['filter_rate']:.1f}%)")
