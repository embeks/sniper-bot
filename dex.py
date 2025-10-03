"""
DEX - PumpFun Bonding Curves with Real-Time Price Parsing
"""

import time
import struct
import logging
from typing import Optional, Dict, Tuple
from solders.pubkey import Pubkey

from config import (
    PUMPFUN_PROGRAM_ID, MIGRATION_THRESHOLD_SOL, RPC_ENDPOINT
)

logger = logging.getLogger(__name__)

class PumpFunDEX:
    """PumpFun bonding curve integration - with real-time price parsing"""
    
    def __init__(self, wallet_manager):
        """Initialize with wallet manager"""
        self.wallet = wallet_manager
        from solana.rpc.api import Client
        self.client = Client(RPC_ENDPOINT)
        
        # Track bonding curve states from WebSocket
        self.bonding_curves_cache = {}
        self.token_websocket_data = {}
        
        # Persistent price cache with TTL
        self.last_good_prices = {}
        self.PRICE_CACHE_TTL = 30  # 30 seconds for volatile tokens
        
    def update_token_data(self, mint: str, websocket_data: Dict):
        """Update token data from WebSocket - CRITICAL METHOD"""
        # Extract actual data from nested structure
        actual_data = websocket_data.get('data', websocket_data)
        
        # Store with proper structure
        self.token_websocket_data[mint] = {
            'data': actual_data,
            'timestamp': time.time()
        }
        
        # Clear old cache to force fresh data usage
        if mint in self.bonding_curves_cache:
            del self.bonding_curves_cache[mint]
        
        v_sol = actual_data.get('vSolInBondingCurve', 0)
        logger.debug(f"Updated WebSocket data for {mint[:8]}... SOL in curve: {v_sol:.2f}")
    
    def derive_bonding_curve_pda(self, mint: Pubkey) -> Tuple[Pubkey, int]:
        """Derive the bonding curve PDA for a token"""
        seeds = [b"bonding-curve", bytes(mint)]
        return Pubkey.find_program_address(seeds, PUMPFUN_PROGRAM_ID)
    
    def _parse_bonding_curve_account(self, account_data: bytes) -> Optional[Dict]:
        """Parse raw bonding curve account data to extract reserves"""
        try:
            if not account_data or len(account_data) < 40:
                logger.debug(f"Account data too short: {len(account_data) if account_data else 0} bytes")
                return None
            
            # PumpFun bonding curve layout (little-endian):
            # 0-8: discriminator
            # 8-16: virtual_token_reserves (u64)
            # 16-24: virtual_sol_reserves (u64)
            # 24-32: real_token_reserves (u64)
            # 32-40: real_sol_reserves (u64)
            
            virtual_token_reserves = struct.unpack('<Q', account_data[8:16])[0]
            virtual_sol_reserves = struct.unpack('<Q', account_data[16:24])[0]
            real_token_reserves = struct.unpack('<Q', account_data[24:32])[0]
            real_sol_reserves = struct.unpack('<Q', account_data[32:40])[0]
            
            # Calculate SOL in curve (convert lamports to SOL)
            sol_in_curve = virtual_sol_reserves / 1e9
            
            # Check for migration
            is_migrated = sol_in_curve >= MIGRATION_THRESHOLD_SOL
            
            logger.debug(f"Parsed bonding curve: {sol_in_curve:.2f} SOL, {virtual_token_reserves:,} tokens")
            
            return {
                'virtual_token_reserves': virtual_token_reserves,
                'virtual_sol_reserves': virtual_sol_reserves,
                'real_token_reserves': real_token_reserves,
                'real_sol_reserves': real_sol_reserves,
                'sol_in_curve': sol_in_curve,
                'is_migrated': is_migrated,
                'is_valid': True,
                'from_chain': True
            }
            
        except Exception as e:
            logger.error(f"Failed to parse bonding curve account: {e}")
            return None
    
    def get_bonding_curve_data(self, mint: str) -> Optional[Dict]:
        """Get bonding curve data - CRITICAL METHOD for price monitoring"""
        try:
            # First check WebSocket data (most recent)
            if mint in self.token_websocket_data:
                ws_data = self.token_websocket_data[mint]
                data_age = time.time() - ws_data['timestamp']
                
                # Use WebSocket data if less than 5 minutes old
                if data_age < 300:
                    token_data = ws_data['data']
                    if 'data' in token_data:
                        token_data = token_data['data']
                    
                    v_sol = token_data.get('vSolInBondingCurve', 0)
                    v_tokens = token_data.get('vTokensInBondingCurve', 0)
                    
                    v_sol_lamports = int(v_sol * 1e9) if v_sol > 0 else 0
                    v_tokens_raw = int(v_tokens * 1e6) if v_tokens > 0 else 0
                    
                    # Check migration
                    is_migrated = v_sol >= MIGRATION_THRESHOLD_SOL
                    if is_migrated:
                        logger.info(f"Token {mint[:8]}... has migrated (SOL: {v_sol:.2f})")
                        return {
                            'is_migrated': True,
                            'sol_in_curve': v_sol,
                            'is_valid': False
                        }
                    
                    curve_data = {
                        'bonding_curve': token_data.get('bondingCurveKey', ''),
                        'virtual_token_reserves': v_tokens_raw,
                        'virtual_sol_reserves': v_sol_lamports,
                        'real_token_reserves': v_tokens_raw,
                        'real_sol_reserves': v_sol_lamports,
                        'price_per_token': v_sol_lamports / v_tokens_raw if v_tokens_raw > 0 else 0,
                        'sol_in_curve': v_sol,
                        'is_migrating': False,
                        'can_buy': True,
                        'from_websocket': True,
                        'is_valid': True,
                        'needs_retry': False
                    }
                    
                    # Save to persistent cache
                    self.last_good_prices[mint] = {
                        'data': curve_data.copy(),
                        'timestamp': time.time()
                    }
                    
                    logger.debug(f"Using WebSocket data for {mint[:8]}... (age: {data_age:.1f}s)")
                    return curve_data
            
            # WebSocket data expired or unavailable - query chain directly via Helius
            logger.debug(f"WebSocket data expired for {mint[:8]}..., querying chain via Helius")
            
            mint_pubkey = Pubkey.from_string(mint)
            bonding_curve, _ = self.derive_bonding_curve_pda(mint_pubkey)
            
            # Query the bonding curve account
            response = self.client.get_account_info(bonding_curve)
            
            if response.value and response.value.data:
                # Parse the actual account data
                parsed_data = self._parse_bonding_curve_account(response.value.data)
                
                if parsed_data:
                    # Successfully parsed real chain data
                    parsed_data['bonding_curve'] = str(bonding_curve)
                    parsed_data['price_per_token'] = (
                        parsed_data['virtual_sol_reserves'] / parsed_data['virtual_token_reserves']
                        if parsed_data['virtual_token_reserves'] > 0 else 0
                    )
                    parsed_data['is_migrating'] = False
                    parsed_data['can_buy'] = True
                    parsed_data['from_websocket'] = False
                    parsed_data['needs_retry'] = False
                    
                    # Cache it
                    self.bonding_curves_cache[mint] = {
                        'data': parsed_data,
                        'timestamp': time.time()
                    }
                    
                    # Save to persistent cache
                    self.last_good_prices[mint] = {
                        'data': parsed_data.copy(),
                        'timestamp': time.time()
                    }
                    
                    logger.info(f"✅ Real-time chain data for {mint[:8]}...: {parsed_data['sol_in_curve']:.2f} SOL")
                    return parsed_data
                else:
                    logger.warning(f"Failed to parse bonding curve data for {mint[:8]}...")
            else:
                logger.debug(f"No bonding curve account found for {mint[:8]}...")
            
            # Check persistent cache as last resort
            if mint in self.last_good_prices:
                cached_price = self.last_good_prices[mint]
                cache_age = time.time() - cached_price['timestamp']
                
                if cache_age < self.PRICE_CACHE_TTL:
                    logger.info(f"Using last good price for {mint[:8]}... (age: {cache_age:.0f}s)")
                    price_data = cached_price['data'].copy()
                    price_data['is_stale'] = True
                    price_data['stale_age_seconds'] = cache_age
                    price_data['needs_retry'] = False
                    return price_data
            
            # No data available at all
            logger.warning(f"❌ No price data available for {mint[:8]}... - cannot calculate P&L")
            return None
            
        except Exception as e:
            logger.error(f"Failed to get bonding curve data for {mint[:8]}...: {e}")
            
            # Check persistent cache on error
            if mint in self.last_good_prices:
                cached_price = self.last_good_prices[mint]
                logger.info(f"Error fetching price, using last good price")
                price_data = cached_price['data'].copy()
                price_data['is_stale'] = True
                price_data['needs_retry'] = False
                return price_data
            
            return None
