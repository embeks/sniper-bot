"""
Velocity Checker - Pre-buy momentum gate
Only allows entries on tokens with strong early velocity
"""

import logging
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

class VelocityChecker:
    """
    Checks token velocity before allowing buy.
    Measures SOL inflow rate and unique buyer count in early seconds.
    """
    
    def __init__(
        self, 
        min_sol_per_second: float = 2.0, 
        min_unique_buyers: int = 5,
        max_token_age_seconds: float = 3.0
    ):
        """
        Args:
            min_sol_per_second: Minimum SOL/s inflow rate required (default 2.0)
            min_unique_buyers: Minimum unique wallet count required (default 5)
            max_token_age_seconds: Max age to even consider (default 3s)
        """
        self.min_sol_per_second = min_sol_per_second
        self.min_unique_buyers = min_unique_buyers
        self.max_token_age_seconds = max_token_age_seconds
        
        # Track velocity snapshots for dynamic exit logic
        self.velocity_history: Dict[str, list] = {}
    
    def check_velocity(
        self, 
        mint: str,
        curve_data: Dict, 
        token_age_seconds: float
    ) -> Tuple[bool, str]:
        """
        Check if token has sufficient velocity to enter.
        
        Args:
            mint: Token mint address
            curve_data: Bonding curve data with sol_raised
            token_age_seconds: Age of token since creation
            
        Returns:
            (passed, reason) tuple
        """
        try:
            # Age gate - skip if too old
            if token_age_seconds > self.max_token_age_seconds:
                logger.info(
                    f"❌ VELOCITY FAILED: token age {token_age_seconds:.1f}s > "
                    f"{self.max_token_age_seconds}s max"
                )
                return False, f"too_old: {token_age_seconds:.1f}s"
            
            sol_raised = curve_data.get('sol_raised', 0)
            
            # Try to get unique buyers from curve data
            # PumpFun bonding curve doesn't always expose this directly
            # So we'll use a heuristic: estimate from SOL raised
            unique_buyers = self._estimate_unique_buyers(sol_raised, token_age_seconds)
            
            # Avoid division by zero
            age = max(token_age_seconds, 0.1)
            
            # Calculate velocity
            sol_per_second = sol_raised / age
            
            # Check thresholds
            sol_check = sol_per_second >= self.min_sol_per_second
            buyers_check = unique_buyers >= self.min_unique_buyers
            
            # Store snapshot for later velocity tracking
            self._store_velocity_snapshot(mint, sol_raised, unique_buyers, time.time())
            
            if sol_check and buyers_check:
                logger.info(
                    f"✅ VELOCITY PASSED: {sol_per_second:.2f} SOL/s "
                    f"({sol_raised:.2f} SOL / {age:.1f}s), ~{unique_buyers} buyers"
                )
                return True, "velocity_passed"
            
            # Log why it failed
            reasons = []
            if not sol_check:
                reasons.append(f"SOL/s: {sol_per_second:.2f} < {self.min_sol_per_second}")
            if not buyers_check:
                reasons.append(f"buyers: ~{unique_buyers} < {self.min_unique_buyers}")
            
            reason_str = ", ".join(reasons)
            logger.info(f"❌ VELOCITY FAILED: {reason_str}")
            
            return False, f"low_velocity: {reason_str}"
            
        except Exception as e:
            logger.error(f"Error checking velocity: {e}")
            return False, f"velocity_error: {str(e)}"
    
    def _estimate_unique_buyers(self, sol_raised: float, age_seconds: float) -> int:
        """
        Estimate unique buyers from SOL raised and age.
        Assumes average buy ~0.5 SOL in early seconds.
        """
        if sol_raised < 0.5:
            return 0
        
        # Very rough heuristic: 1 buyer per 0.3-0.5 SOL in first 3 seconds
        avg_buy_size = 0.4  # Conservative estimate
        estimated = int(sol_raised / avg_buy_size)
        
        return estimated
    
    def _store_velocity_snapshot(
        self, 
        mint: str, 
        sol_raised: float, 
        buyers: int, 
        timestamp: float
    ):
        """Store velocity snapshot for tracking acceleration"""
        if mint not in self.velocity_history:
            self.velocity_history[mint] = []
        
        self.velocity_history[mint].append({
            'sol_raised': sol_raised,
            'buyers': buyers,
            'timestamp': timestamp
        })
        
        # Keep only last 10 snapshots
        if len(self.velocity_history[mint]) > 10:
            self.velocity_history[mint] = self.velocity_history[mint][-10:]
    
    def is_velocity_accelerating(
        self, 
        mint: str, 
        current_sol_raised: float,
        window_seconds: float = 5.0
    ) -> bool:
        """
        Check if velocity is still accelerating (for dynamic exit extension).
        
        Args:
            mint: Token mint address
            current_sol_raised: Current SOL raised
            window_seconds: Time window to compare (default 5s)
            
        Returns:
            True if velocity is increasing
        """
        try:
            if mint not in self.velocity_history or len(self.velocity_history[mint]) < 2:
                return False
            
            history = self.velocity_history[mint]
            current_time = time.time()
            
            # Find snapshot from ~5 seconds ago
            old_snapshot = None
            for snap in reversed(history):
                if current_time - snap['timestamp'] >= window_seconds:
                    old_snapshot = snap
                    break
            
            if not old_snapshot:
                # Not enough history yet
                return False
            
            # Calculate old velocity
            old_age = old_snapshot['timestamp'] - history[0]['timestamp']
            if old_age < 0.1:
                return False
            
            old_velocity = old_snapshot['sol_raised'] / max(old_age, 0.1)
            
            # Calculate current velocity
            current_age = current_time - history[0]['timestamp']
            current_velocity = current_sol_raised / max(current_age, 0.1)
            
            # Check if accelerating (>20% increase)
            is_accelerating = current_velocity > old_velocity * 1.2
            
            if is_accelerating:
                logger.info(
                    f"🚀 Velocity accelerating: {old_velocity:.2f} → {current_velocity:.2f} SOL/s"
                )
            
            return is_accelerating
            
        except Exception as e:
            logger.error(f"Error checking velocity acceleration: {e}")
            return False
    
    def update_snapshot(self, mint: str, sol_raised: float, buyers: int):
        """Update velocity snapshot during monitoring"""
        self._store_velocity_snapshot(mint, sol_raised, buyers, time.time())
    
    def clear_history(self, mint: str):
        """Clear velocity history for a token (after position closes)"""
        if mint in self.velocity_history:
            del self.velocity_history[mint]
