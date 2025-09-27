"""
DEX Integration - PumpFun Bonding Curves
Cleaned version - removed unused methods, kept critical functionality
"""

import time
import base64
import logging
from typing import Optional, Dict, Tuple
from solders.pubkey import Pubkey

from config import (
    PUMPFUN_PROGRAM_ID, MIGRATION_THRESHOLD_SOL, RPC_ENDPOINT
)

logger = logging.getLogger(__name__)

class PumpFunDEX:
    """PumpFun bonding curve integration - minimal version"""
    
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
    
    def get_bonding_curve_data(self, mint: str) -> Optional[Dict]:
        """Get bonding curve data - CRITICAL METHOD for price monitoring"""
        try:
            # First check WebSocket data
            if mint in self.token_websocket_data:
                ws_data = self.token_websocket_data[mint]
                data_age = time.time() - ws_data['timestamp']
                
                if data_age < 60:  # Use if less than 60 seconds old
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
            
            # Check cache
            if mint in self.bonding_curves_cache:
                cached = self.bonding_curves_cache[mint]
                cache_age = time.time() - cached['timestamp']
                if cache_age < 10:
                    logger.debug(f"Using cached bonding curve for {mint[:8]}...")
                    return cached['data']
            
            # Try chain query as fallback
            mint_pubkey = Pubkey.from_string(mint)
            bonding_curve, _ = self.derive_bonding_curve_pda(mint_pubkey)
            
            response = self.client.get_account_info(bonding_curve)
            if response.value and response.value.data:
                logger.warning(f"Chain query for {mint[:8]}... - using estimate")
                
                curve_data = {
                    'bonding_curve': str(bonding_curve),
                    'virtual_token_reserves': 1_000_000_000_000,
                    'virtual_sol_reserves': 30_000_000_000,
                    'real_token_reserves': 1_000_000_000_000,
                    'real_sol_reserves': 30_000_000_000,
                    'price_per_token': 0.00003,
                    'sol_in_curve': 30,
                    'is_migrating': False,
                    'can_buy': True,
                    'from_websocket': False,
                    'is_estimate': True,
                    'is_valid': True,
                    'needs_retry': False
                }
                
                self.bonding_curves_cache[mint] = {
                    'data': curve_data,
                    'timestamp': time.time()
                }
                
                self.last_good_prices[mint] = {
                    'data': curve_data.copy(),
                    'timestamp': time.time()
                }
                
                return curve_data
            
            # Check persistent cache
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
            
            # Return safe placeholder
            logger.debug(f"No bonding curve account found for {mint[:8]}...")
            curve_data = {
                'bonding_curve': str(bonding_curve) if 'bonding_curve' in locals() else '',
                'virtual_token_reserves': 1_000_000_000_000,
                'virtual_sol_reserves': 30_000_000_000,
                'real_token_reserves': 1_000_000_000_000,
                'real_sol_reserves': 30_000_000_000,
                'price_per_token': 0.00003,
                'sol_in_curve': 30,
                'is_migrating': False,
                'can_buy': True,
                'from_websocket': False,
                'is_estimate': True,
                'is_valid': True,
                'needs_retry': False,
                'no_data_available': True
            }
            
            self.bonding_curves_cache[mint] = {
                'data': curve_data,
                'timestamp': time.time()
            }
            
            return curve_data
            
        except Exception as e:
            logger.error(f"Failed to get bonding curve data for {mint[:8]}...: {e}")
            
            if mint in self.last_good_prices:
                cached_price = self.last_good_prices[mint]
                logger.info(f"Error fetching price, using last good price")
                price_data = cached_price['data'].copy()
                price_data['is_stale'] = True
                price_data['needs_retry'] = False
                return price_data
            
            return {
                'bonding_curve': '',
                'virtual_token_reserves': 1_000_000_000_000,
                'virtual_sol_reserves': 30_000_000_000,
                'real_token_reserves': 1_000_000_000_000,
                'real_sol_reserves': 30_000_000_000,
                'price_per_token': 0.00003,
                'sol_in_curve': 30,
                'is_migrating': False,
                'can_buy': True,
                'from_websocket': False,
                'is_estimate': True,
                'is_valid': True,
                'needs_retry': False,
                'error': str(e)
            }
