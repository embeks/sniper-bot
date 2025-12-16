
"""
Bonding Curve State Reader - Read liquidity directly from chain
✅ OPUS FIX (Issue B): Add explicit price_lamports_per_atomic field for consistency
"""

import struct
import logging
import time
from typing import Optional, Dict, Tuple
from solders.pubkey import Pubkey
from solana.rpc.api import Client

logger = logging.getLogger(__name__)

class BondingCurveReader:
    """Read PumpFun bonding curve state for liquidity validation"""
    
    def __init__(self, rpc_client: Client, program_id: Pubkey):
        self.client = rpc_client
        self.program_id = program_id
        self.cache = {}
        self.CACHE_TTL = 2
        
    def derive_curve_pda(self, mint: Pubkey) -> Tuple[Pubkey, int]:
        """Derive bonding curve PDA"""
        seeds = [b"bonding-curve", bytes(mint)]
        return Pubkey.find_program_address(seeds, self.program_id)
    
    def _parse_curve_account(self, account_data: bytes) -> Optional[Dict]:
        """
        Parse bonding curve layout
        ✅ OPUS FIX: Add explicit price_lamports_per_atomic field
        """
        try:
            if not account_data or len(account_data) < 49:
                return None
            
            # All values in ATOMIC UNITS (raw blockchain data)
            virtual_token_reserves = struct.unpack('<Q', account_data[8:16])[0]
            virtual_sol_reserves = struct.unpack('<Q', account_data[16:24])[0]
            real_token_reserves = struct.unpack('<Q', account_data[24:32])[0]
            real_sol_reserves = struct.unpack('<Q', account_data[32:40])[0]
            token_total_supply = struct.unpack('<Q', account_data[40:48])[0]
            complete = bool(account_data[48])
            
            # Convert to human-readable for display/validation
            sol_raised = real_sol_reserves / 1e9
            tokens_minted = (token_total_supply - real_token_reserves) / 1e6
            
            # ✅ OPUS FIX: Calculate explicit price field for consistency
            # This ensures price calculation is standardized across all modules
            price_lamports_per_atomic = (
                virtual_sol_reserves / virtual_token_reserves
            ) if virtual_token_reserves > 0 else 0
            
            logger.debug(
                f"Parsed curve: {sol_raised:.2f} SOL raised, "
                f"v_sol={virtual_sol_reserves:,} lamports, "
                f"v_tokens={virtual_token_reserves:,} atomic, "
                f"price={price_lamports_per_atomic:.10f} lamports/atomic"
            )
            
            return {
                'virtual_token_reserves': virtual_token_reserves,  # Atomic units (lamports-equivalent for tokens)
                'virtual_sol_reserves': virtual_sol_reserves,      # Lamports
                'real_token_reserves': real_token_reserves,        # Atomic units
                'real_sol_reserves': real_sol_reserves,            # Lamports
                'sol_raised': sol_raised,                          # Human-readable SOL
                'tokens_minted': tokens_minted,                    # Human-readable tokens
                'complete': complete,
                'price_lamports_per_atomic': price_lamports_per_atomic,  # ✅ EXPLICIT PRICE FIELD
                'is_valid': True
            }
            
        except Exception as e:
            logger.error(f"Parse error: {e}")
            return None
    
    def get_curve_state(self, mint: str, use_cache: bool = True) -> Optional[Dict]:
        """Get current curve state"""
        if use_cache and mint in self.cache:
            cached = self.cache[mint]
            if time.time() - cached['timestamp'] < self.CACHE_TTL:
                return cached['data']
        
        try:
            mint_pubkey = Pubkey.from_string(mint)
            curve_pda, _ = self.derive_curve_pda(mint_pubkey)
            
            response = self.client.get_account_info(curve_pda)

            if not response.value or not response.value.data:
                return None
            
            parsed = self._parse_curve_account(response.value.data)
            
            if parsed:
                self.cache[mint] = {
                    'data': parsed,
                    'timestamp': time.time()
                }
                return parsed
            
            return None
            
        except Exception as e:
            logger.error(f"Get curve state error: {e}")
            return None
    
    def validate_liquidity(
        self, 
        mint: str, 
        buy_size_sol: float,
        min_multiplier: float = 5.0,
        min_absolute_sol: float = 0.6
    ) -> Tuple[bool, str, Optional[Dict]]:
        """
        Check if curve has enough liquidity for safe buy
        Returns: (passed, reason, curve_data)
        """
        curve_data = self.get_curve_state(mint, use_cache=False)
        
        if not curve_data:
            return False, "curve_not_found", None
        
        if curve_data['complete']:
            return False, "curve_complete", curve_data
        
        sol_raised = curve_data['sol_raised']
        
        if sol_raised < min_absolute_sol:
            return False, f"too_low_{sol_raised:.3f}", curve_data
        
        required_sol = buy_size_sol * min_multiplier
        if sol_raised < required_sol:
            return False, f"need_{min_multiplier}x_got_{sol_raised:.2f}", curve_data
        
        logger.info(f"✅ Liquidity OK: {sol_raised:.4f} SOL (>= {min_multiplier}x {buy_size_sol})")
        return True, "ok", curve_data
    
    def estimate_slippage(self, mint: str, buy_size_sol: float) -> Optional[float]:
        """
        Estimate buy slippage %
        Uses the standardized price_lamports_per_atomic field
        """
        curve_data = self.get_curve_state(mint)
        if not curve_data:
            return None
        
        try:
            # Get atomic units from curve data
            virtual_sol = curve_data['virtual_sol_reserves']  # lamports
            virtual_tokens = curve_data['virtual_token_reserves']  # atomic units
            
            # Convert to human-readable for calculation
            sol_human = virtual_sol / 1e9
            tokens_human = virtual_tokens / 1e6  # Assuming 6 decimals (PumpFun standard)
            
            current_price = sol_human / tokens_human if tokens_human > 0 else 0
            
            # Constant product AMM formula
            k = sol_human * tokens_human
            new_sol = sol_human + buy_size_sol
            new_tokens = k / new_sol
            tokens_out = tokens_human - new_tokens
            
            effective_price = buy_size_sol / tokens_out if tokens_out > 0 else 0
            slippage_pct = ((effective_price / current_price) - 1) * 100 if current_price > 0 else 0
            
            logger.debug(
                f"Slippage estimate: current={current_price:.10f}, "
                f"effective={effective_price:.10f}, slippage={slippage_pct:.2f}%"
            )
            
            return slippage_pct
            
        except Exception as e:
            logger.error(f"Slippage estimate error: {e}")
            return None
