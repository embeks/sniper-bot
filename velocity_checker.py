"""
Velocity Checker - SURGICAL FIX APPLIED
✅ Fix 4: Removed 2-snapshot requirement (now only needs 1 snapshot)
✅ Validates velocity immediately without waiting for confirmation
"""

import logging
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

class VelocityChecker:
    """
    Checks token velocity before allowing buy.
    ✅ FIXED: Now only requires 1 snapshot (removed 2-snapshot delay)
    """
    
    def __init__(
        self, 
        min_sol_per_second: float = 2.0, 
        min_unique_buyers: int = 5,
        max_token_age_seconds: float = 6.0,
        min_recent_1s_sol: float = 2.0,
        min_recent_3s_sol: float = 4.0,
        max_drop_percent: float = 25.0,
        min_snapshots: int = 1  # ✅ FIXED: Changed from 2 to 1
    ):
        """
        Args:
            min_sol_per_second: Minimum average SOL/s inflow rate (default 2.0)
            min_unique_buyers: Minimum unique wallet count (default 5)
            max_token_age_seconds: Max age to consider (default 6s)
            min_recent_1s_sol: Minimum SOL in last 1 second (default 2.0)
            min_recent_3s_sol: Minimum SOL in last 3 seconds (default 4.0)
            max_drop_percent: Max velocity drop allowed (default 25%)
            min_snapshots: Minimum snapshots required (default 1) ✅ FIXED
        """
        self.min_sol_per_second = min_sol_per_second
        self.min_unique_buyers = min_unique_buyers
        self.max_token_age_seconds = max_token_age_seconds
        self.min_recent_1s_sol = min_recent_1s_sol
        self.min_recent_3s_sol = min_recent_3s_sol
        self.max_drop_percent = max_drop_percent
        self.min_snapshots = min_snapshots  # ✅ FIXED: Now 1 instead of 2
        
        # Track velocity snapshots for dynamic checks
        self.velocity_history: Dict[str, list] = {}
        
        # Track pre-buy velocity for fail-fast comparison
        self.pre_buy_velocity: Dict[str, float] = {}
    
    def check_velocity(
        self, 
        mint: str,
        curve_data: Dict, 
        token_age_seconds: float
    ) -> Tuple[bool, str]:
        """
        ✅ FIXED: Only requires 1 snapshot (removed 2-snapshot delay)
        Validates velocity using fresh blockchain data
        
        Args:
            mint: Token mint address
            curve_data: Bonding curve data with sol_raised
            token_age_seconds: Age of token since creation
            
        Returns:
            (passed, reason) tuple
        """
        try:
            # Age gate
            if token_age_seconds > self.max_token_age_seconds:
                logger.info(
                    f"❌ VELOCITY FAILED: token age {token_age_seconds:.1f}s > "
                    f"{self.max_token_age_seconds}s max"
                )
                return False, f"too_old: {token_age_seconds:.1f}s"
            
            sol_raised = curve_data.get('sol_raised', 0) or curve_data.get('sol_in_curve', 0)
            
            # Estimate unique buyers
            unique_buyers = self._estimate_unique_buyers(sol_raised, token_age_seconds)
            
            # Avoid division by zero
            age = max(token_age_seconds, 0.1)
            
            # Calculate AVERAGE velocity
            avg_sol_per_second = sol_raised / age
            
            # ✅ REMOVED 2-SNAPSHOT RULE - Process immediately
            # Store snapshot for monitoring
            self._store_velocity_snapshot(mint, sol_raised, unique_buyers, time.time())
            
            snapshot_count = len(self.velocity_history.get(mint, []))
            logger.debug(f"📊 Velocity check for {mint[:8]}... (snapshot {snapshot_count})")
            
            # Basic velocity checks
            sol_check = avg_sol_per_second >= self.min_sol_per_second
            buyers_check = unique_buyers >= self.min_unique_buyers
            
            if not sol_check or not buyers_check:
                reasons = []
                if not sol_check:
                    reasons.append(f"Avg SOL/s: {avg_sol_per_second:.2f} < {self.min_sol_per_second}")
                if not buyers_check:
                    reasons.append(f"buyers: ~{unique_buyers} < {self.min_unique_buyers}")
                
                reason_str = ", ".join(reasons)
                logger.info(f"❌ VELOCITY FAILED: {reason_str}")
                return False, f"low_velocity: {reason_str}"
            
            # ✅ If we have 2+ snapshots, check for dying velocity
            # But don't REQUIRE 2 snapshots - this is just extra validation
            if snapshot_count >= 2:
                velocity_drop = self._get_velocity_drop_percent(mint, sol_raised)
                
                if velocity_drop is not None and velocity_drop > self.max_drop_percent:
                    logger.info(
                        f"❌ VELOCITY DYING: dropped {velocity_drop:.1f}% from previous snapshot "
                        f"(max allowed: {self.max_drop_percent}%)"
                    )
                    return False, f"velocity_dying: {velocity_drop:.1f}% drop"
                
                logger.debug(f"✓ Velocity stable: {velocity_drop:+.1f}% change from previous")
            
            # Get recent velocity if we have enough history
            recent_1s_sol = self._get_recent_sol_delta(mint, sol_raised, 1.0)
            recent_3s_sol = self._get_recent_sol_delta(mint, sol_raised, 3.0)
            
            # Check if recent flow meets minimums (optional if we have data)
            if recent_1s_sol is not None and recent_1s_sol < self.min_recent_1s_sol:
                logger.info(
                    f"❌ RECENT VELOCITY TOO LOW: {recent_1s_sol:.2f} SOL in last 1s "
                    f"(need ≥{self.min_recent_1s_sol})"
                )
                return False, f"recent_1s_low: {recent_1s_sol:.2f} SOL"
            
            if recent_3s_sol is not None and recent_3s_sol < self.min_recent_3s_sol:
                logger.info(
                    f"❌ RECENT VELOCITY TOO LOW: {recent_3s_sol:.2f} SOL in last 3s "
                    f"(need ≥{self.min_recent_3s_sol})"
                )
                return False, f"recent_3s_low: {recent_3s_sol:.2f} SOL"
            
            # Store pre-buy velocity for fail-fast comparison later
            self.pre_buy_velocity[mint] = avg_sol_per_second
            
            logger.info(
                f"✅ VELOCITY PASSED: Avg {avg_sol_per_second:.2f} SOL/s "
                f"({sol_raised:.2f} SOL / {age:.1f}s), ~{unique_buyers} buyers"
            )
            if recent_1s_sol is not None:
                logger.debug(f"   Recent flow: {recent_1s_sol:.2f} SOL (last 1s)")
            if recent_3s_sol is not None:
                logger.debug(f"   Recent flow: {recent_3s_sol:.2f} SOL (last 3s)")
            
            return True, "velocity_passed"
            
        except Exception as e:
            logger.error(f"Error checking velocity: {e}")
            return False, f"velocity_error: {str(e)}"
    
    def _get_recent_sol_delta(
        self, 
        mint: str, 
        current_sol_raised: float, 
        window_seconds: float
    ) -> Optional[float]:
        """
        Get SOL raised in the last N seconds.
        
        Args:
            mint: Token mint
            current_sol_raised: Current total SOL raised
            window_seconds: Time window (1.0 or 3.0 seconds)
            
        Returns:
            SOL delta in last N seconds, or None if not enough history
        """
        try:
            if mint not in self.velocity_history or len(self.velocity_history[mint]) < 2:
                return None
            
            history = self.velocity_history[mint]
            current_time = time.time()
            target_time = current_time - window_seconds
            
            # Find closest snapshot to target time
            closest_snapshot = None
            min_time_diff = float('inf')
            
            for snap in history:
                time_diff = abs(snap['timestamp'] - target_time)
                if time_diff < min_time_diff:
                    min_time_diff = time_diff
                    closest_snapshot = snap
            
            # If we found a snapshot within 2s of target, use it
            if closest_snapshot and min_time_diff < 2.0:
                sol_delta = current_sol_raised - closest_snapshot['sol_raised']
                return max(0, sol_delta)
            
            return None
            
        except Exception as e:
            logger.error(f"Error getting recent SOL delta: {e}")
            return None
    
    def _get_velocity_drop_percent(
        self, 
        mint: str, 
        current_sol_raised: float
    ) -> Optional[float]:
        """
        Calculate velocity drop % from previous snapshot to current.
        Positive value = velocity decreased (BAD)
        Negative value = velocity increased (GOOD)
        
        Returns:
            Drop percentage, or None if not enough history
        """
        try:
            if mint not in self.velocity_history or len(self.velocity_history[mint]) < 2:
                return None
            
            history = self.velocity_history[mint]
            current_time = time.time()
            
            # Get previous snapshot (most recent before current)
            prev_snapshot = history[-2] if len(history) >= 2 else history[-1]
            
            # Calculate time deltas from token creation
            first_snapshot = history[0]
            prev_age = prev_snapshot['timestamp'] - first_snapshot['timestamp']
            current_age = current_time - first_snapshot['timestamp']
            
            if prev_age < 0.1 or current_age < 0.1:
                return None
            
            # Calculate velocities (SOL/s)
            prev_velocity = (prev_snapshot['sol_raised'] - first_snapshot['sol_raised']) / max(prev_age, 0.1)
            current_velocity = (current_sol_raised - first_snapshot['sol_raised']) / max(current_age, 0.1)
            
            if prev_velocity <= 0:
                return None
            
            # Calculate drop percent (positive = bad, negative = good)
            drop_percent = ((prev_velocity - current_velocity) / prev_velocity) * 100
            
            return drop_percent
            
        except Exception as e:
            logger.error(f"Error calculating velocity drop: {e}")
            return None
    
    def _estimate_unique_buyers(self, sol_raised: float, age_seconds: float) -> int:
        """
        Estimate unique buyers from SOL raised and age.
        Assumes average buy ~0.5 SOL in early seconds.
        """
        if sol_raised < 0.5:
            return 0
        
        avg_buy_size = 0.4
        estimated = int(sol_raised / avg_buy_size)
        return estimated
    
    def _store_velocity_snapshot(
        self, 
        mint: str, 
        sol_raised: float, 
        buyers: int, 
        timestamp: float
    ):
        """Store velocity snapshot for tracking changes over time"""
        if mint not in self.velocity_history:
            self.velocity_history[mint] = []
        
        self.velocity_history[mint].append({
            'sol_raised': sol_raised,
            'buyers': buyers,
            'timestamp': timestamp
        })
        
        # Keep only last 15 snapshots
        if len(self.velocity_history[mint]) > 15:
            self.velocity_history[mint] = self.velocity_history[mint][-15:]
    
    def get_pre_buy_velocity(self, mint: str) -> Optional[float]:
        """
        Get the velocity recorded just before buy (for fail-fast comparison).
        
        Returns:
            Pre-buy SOL/s, or None if not recorded
        """
        return self.pre_buy_velocity.get(mint)
    
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
            True if velocity is increasing >20%
        """
        try:
            if mint not in self.velocity_history or len(self.velocity_history[mint]) < 2:
                return False
            
            history = self.velocity_history[mint]
            current_time = time.time()
            
            # Find snapshot from ~window_seconds ago
            old_snapshot = None
            for snap in reversed(history):
                if current_time - snap['timestamp'] >= window_seconds:
                    old_snapshot = snap
                    break
            
            if not old_snapshot:
                return False
            
            # Calculate old velocity
            old_age = old_snapshot['timestamp'] - history[0]['timestamp']
            if old_age < 0.1:
                return False
            
            old_velocity = old_snapshot['sol_raised'] / old_age
            
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
        if mint in self.pre_buy_velocity:
            del self.pre_buy_velocity[mint]
