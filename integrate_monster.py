# integrate_monster.py - DUAL MODE STRATEGY SYSTEM
"""
DUAL STRATEGY MODES:
1. AGGRESSIVE MODE: 1 SOL → 4-6 SOL in ~12 hours (High risk, fast trades) 
2. SAFE MODE: Steady 2 SOL/day baseline (Low risk, quality trades)

Toggle via AGGRESSIVE_MODE=true/false environment variable
"""

import asyncio
import os
import logging
import time
import random
import httpx
from typing import Dict, List, Optional, Tuple, Any
from dotenv import load_dotenv
from fastapi import FastAPI, Request
import uvicorn
import certifi
from dataclasses import dataclass
from collections import deque
from datetime import datetime, timedelta

# Import your existing modules
from sniper_logic import (
    mempool_listener, trending_scanner, 
    start_sniper_with_forced_token, stop_all_tasks,
    pumpfun_migration_monitor, pumpfun_tokens, migration_watch_list
)

# Import utilities
from utils import (
    buy_token as original_buy_token,
    send_telegram_alert, send_telegram_batch,
    keypair, BUY_AMOUNT_SOL,
    is_bot_running, start_bot, stop_bot, 
    get_wallet_summary, get_bot_status_message,
    check_pumpfun_token_status, detect_pumpfun_migration,
    get_liquidity_and_ownership, get_dynamic_position_size,
    get_minimum_liquidity_required, USE_DYNAMIC_SIZING, SCALE_WITH_BALANCE
)

load_dotenv()

# ============================================
# DUAL MODE CONFIGURATION
# ============================================

AGGRESSIVE_MODE = os.getenv("AGGRESSIVE_MODE", "false").lower() == "true"
AGGRESSIVE_START_TIME = time.time() if AGGRESSIVE_MODE else None
AGGRESSIVE_DURATION_HOURS = float(os.getenv("AGGRESSIVE_DURATION_HOURS", 12))
TARGET_MULTIPLIER = float(os.getenv("TARGET_MULTIPLIER", 5))
TIMEFRAME_HOURS = float(os.getenv("TIMEFRAME_HOURS", 12))

# Mode-specific configurations
def get_mode_config():
    """Get configuration based on current mode"""
    if AGGRESSIVE_MODE:
        return {
            # AGGRESSIVE MODE SETTINGS
            "min_position_size": 0.10,
            "max_position_size": 0.25,
            "min_liquidity_usd": 2000,
            "min_volume_usd": 1000,
            "min_holder_count": 20,
            "max_top_holder_percent": 70,
            "min_ai_score": 0.15,
            "stop_loss_pct": 0.40,
            "take_profit_1": 2.0,    # 2x
            "take_profit_2": 3.5,    # 3.5x
            "take_profit_3": 7.0,    # 7x
            "partial_exit_1": 40,    # 40%
            "partial_exit_2": 30,    # 30%
            "partial_exit_3": 30,    # 30%
            "max_drawdown": 0.60,
            "max_daily_loss": 0.50,
            "max_consecutive_losses": 10,
            "alert_prefix": "🚨 AGGRESSIVE PLAY 🚨"
        }
    else:
        return {
            # SAFE MODE SETTINGS
            "min_position_size": 0.05,
            "max_position_size": 0.10,
            "min_liquidity_usd": 10000,
            "min_volume_usd": 5000,
            "min_holder_count": 50,
            "max_top_holder_percent": 30,
            "min_ai_score": 0.30,
            "stop_loss_pct": 0.30,
            "take_profit_1": 1.5,    # 1.5x
            "take_profit_2": 2.5,    # 2.5x
            "take_profit_3": 4.0,    # 4x
            "partial_exit_1": 40,    # 40%
            "partial_exit_2": 30,    # 30%
            "partial_exit_3": 30,    # 30% moonbag
            "max_drawdown": 0.30,
            "max_daily_loss": 0.20,
            "max_consecutive_losses": 5,
            "alert_prefix": "🟢 SAFE MODE TRADE 🟢"
        }

MODE_CONFIG = get_mode_config()

# Track aggressive mode metrics
aggressive_metrics = {
    "start_balance": None,
    "target_balance": None,
    "trades_executed": 0,
    "wins": 0,
    "losses": 0,
    "current_multiplier": 1.0,
    "best_trade": 0,
    "worst_trade": 0
}

# Add global for tracking last progress alert
last_progress_alert_time = 0

def is_aggressive_mode_active():
    """Check if we're still in aggressive mode timeframe"""
    if not AGGRESSIVE_MODE or not AGGRESSIVE_START_TIME:
        return False
    
    hours_elapsed = (time.time() - AGGRESSIVE_START_TIME) / 3600
    if hours_elapsed > AGGRESSIVE_DURATION_HOURS:
        logging.info(f"[AGGRESSIVE] {AGGRESSIVE_DURATION_HOURS}-hour period ended, reverting to safe mode")
        return False
    
    return True

async def check_aggressive_progress(force_alert=False):
    """Monitor progress toward aggressive goals"""
    global last_progress_alert_time
    
    if not is_aggressive_mode_active():
        return False
    
    try:
        from utils import rpc, keypair
        current_balance = rpc.get_balance(keypair.pubkey()).value / 1e9
        
        if aggressive_metrics["start_balance"] is None:
            aggressive_metrics["start_balance"] = current_balance
            aggressive_metrics["target_balance"] = current_balance * TARGET_MULTIPLIER
            logging.info(f"[AGGRESSIVE] Starting balance: {current_balance:.2f} SOL, Target: {aggressive_metrics['target_balance']:.2f} SOL")
            
        aggressive_metrics["current_multiplier"] = current_balance / aggressive_metrics["start_balance"]
        hours_elapsed = (time.time() - AGGRESSIVE_START_TIME) / 3600
        hours_remaining = AGGRESSIVE_DURATION_HOURS - hours_elapsed
        
        # Calculate required growth rate
        required_multiplier = (hours_elapsed / AGGRESSIVE_DURATION_HOURS) * TARGET_MULTIPLIER
        on_track = aggressive_metrics["current_multiplier"] >= required_multiplier * 0.8
        
        # Only send alert if 30 minutes have passed since last alert OR if forced
        current_time = time.time()
        should_send_alert = force_alert or (current_time - last_progress_alert_time > 1800)
        
        if should_send_alert:
            last_progress_alert_time = current_time
            
            progress_msg = f"""
{MODE_CONFIG['alert_prefix']} STATUS
━━━━━━━━━━━━━━━━━━━━━━━━━
Time: {hours_elapsed:.1f}h / {AGGRESSIVE_DURATION_HOURS}h ({hours_remaining:.1f}h left)
Progress: {aggressive_metrics['current_multiplier']:.2f}x / {TARGET_MULTIPLIER}x
Current: {current_balance:.2f} SOL
Target: {aggressive_metrics['target_balance']:.2f} SOL
━━━━━━━━━━━━━━━━━━━━━━━━━
Trades: {aggressive_metrics['trades_executed']}
Wins: {aggressive_metrics['wins']} | Losses: {aggressive_metrics['losses']}
Win Rate: {(aggressive_metrics['wins']/max(1, aggressive_metrics['trades_executed'])*100):.0f}%
Best Trade: +{aggressive_metrics['best_trade']:.3f} SOL
Worst Trade: {aggressive_metrics['worst_trade']:.3f} SOL
━━━━━━━━━━━━━━━━━━━━━━━━━
Status: {'🟢 ON TRACK!' if on_track else '🔴 NEED MORE AGGRESSION!'}
"""
            
            await send_telegram_alert(progress_msg)
        
        # Return True if we need to increase aggression
        return not on_track
            
    except Exception as e:
        logging.error(f"[AGGRESSIVE] Progress check error: {e}")
    
    return False

# ============================================
# STARTUP CONFIGURATION LOGGING
# ============================================

def log_configuration():
    """Log all critical configuration at startup"""
    mode = "AGGRESSIVE (12-HOUR SPRINT)" if is_aggressive_mode_active() else "SAFE (STEADY PROFIT)"
    config = MODE_CONFIG
    
    config_items = [
        ("MODE", mode),
        ("AGGRESSIVE_MODE", os.getenv("AGGRESSIVE_MODE", "false")),
        ("TARGET_MULTIPLIER", str(TARGET_MULTIPLIER) if AGGRESSIVE_MODE else "N/A"),
        ("MIN_POSITION_SIZE", f"{config['min_position_size']:.2f} SOL"),
        ("MAX_POSITION_SIZE", f"{config['max_position_size']:.2f} SOL"),
        ("MIN_LIQUIDITY_USD", f"${config['min_liquidity_usd']}"),
        ("MIN_VOLUME_USD", f"${config['min_volume_usd']}"),
        ("MIN_HOLDER_COUNT", str(config['min_holder_count'])),
        ("MAX_TOP_HOLDER_PERCENT", f"{config['max_top_holder_percent']}%"),
        ("MIN_AI_SCORE", str(config['min_ai_score'])),
        ("STOP_LOSS", f"{config['stop_loss_pct']*100:.0f}%"),
        ("PROFIT_TARGETS", f"{config['take_profit_1']}x, {config['take_profit_2']}x, {config['take_profit_3']}x"),
        ("MAX_DRAWDOWN", f"{config['max_drawdown']*100:.0f}%"),
        ("MAX_DAILY_LOSS", f"{config['max_daily_loss']*100:.0f}%"),
    ]
    
    logging.info("=" * 60)
    logging.info(f"DUAL MODE SNIPER CONFIGURATION - {mode}")
    logging.info("=" * 60)
    
    for key, value in config_items:
        logging.info(f"{key}: {value}")
    
    logging.info("=" * 60)

# ============================================
# ENHANCED POSITION SIZING WITH FIXES
# ============================================

def calculate_position_size_fixed(balance_sol: float, mint: str, liquidity: float) -> float:
    """Calculate position size with balance validation fixes"""
    global RISK_STATE
    
    # FIX 1: Reserve minimum for fees
    MIN_RESERVE = 0.01
    available_balance = max(0, balance_sol - MIN_RESERVE)
    
    if available_balance < 0.01:
        logging.warning(f"Insufficient balance: {balance_sol:.4f} SOL")
        return 0
    
    config = MODE_CONFIG
    
    logging.info(f"[SIZING] Mode: {'AGGRESSIVE' if AGGRESSIVE_MODE else 'SAFE'}, Balance: {balance_sol:.2f}, Available: {available_balance:.2f}")
    
    if AGGRESSIVE_MODE:
        # AGGRESSIVE MODE: 10-25% of balance
        position_pct = 0.25  # Default 25%
        
        # Adjust based on liquidity
        if liquidity < 5:
            position_pct = 0.10
        elif liquidity < 10:
            position_pct = 0.15
        elif liquidity < 20:
            position_pct = 0.20
        else:
            position_pct = 0.25
        
        position_size = available_balance * position_pct
        
        # Enforce min/max limits
        position_size = max(config['min_position_size'], position_size)
        position_size = min(config['max_position_size'], position_size)
        
    else:
        # SAFE MODE: 5-10% of balance
        position_pct = 0.10  # Default 10%
        
        # Adjust based on liquidity
        if liquidity < 10:
            position_pct = 0.05
        elif liquidity < 50:
            position_pct = 0.075
        else:
            position_pct = 0.10
        
        position_size = available_balance * position_pct
        
        # Enforce min/max limits
        position_size = max(config['min_position_size'], position_size)
        position_size = min(config['max_position_size'], position_size)
    
    # FIX 1 continued: Never exceed available balance
    position_size = min(position_size, available_balance)
    position_size = max(0.01, position_size)  # Minimum viable position
    
    logging.info(f"[SIZING] Final: {position_size:.3f} SOL ({position_pct*100:.0f}% of balance)")
    
    return position_size

# ============================================
# AI SCORING ENGINE (Mode-Aware)
# ============================================

class AIScorer:
    """Machine Learning scoring for token potential"""
    
    def __init__(self):
        self.historical_data = deque(maxlen=1000)
        self.winning_patterns = {}
        
    async def score_token(self, mint: str, pool_data: Dict) -> float:
        """Score token from 0-1 based on multiple factors"""
        score = 0.0
        config = MODE_CONFIG
        
        # Base score adjustment for mode
        if AGGRESSIVE_MODE:
            score += 0.10  # Start with baseline boost in aggressive mode
        
        # 1. Liquidity Score
        lp_sol = pool_data.get("liquidity", 0)
        lp_usd = lp_sol * 150  # Assuming SOL = $150
        
        if AGGRESSIVE_MODE:
            # Aggressive: Accept lower liquidity
            if lp_usd >= config['min_liquidity_usd']:
                score += 0.25
            elif lp_usd >= config['min_liquidity_usd'] * 0.5:
                score += 0.15
        else:
            # Safe: Require higher liquidity
            if lp_usd >= config['min_liquidity_usd'] * 2:
                score += 0.30
            elif lp_usd >= config['min_liquidity_usd']:
                score += 0.20
            else:
                return 0  # Reject in safe mode
        
        # 2. PumpFun bonus
        try:
            if mint in pumpfun_tokens:
                score += 0.20
                if pumpfun_tokens[mint].get("migrated", False):
                    score += 0.30  # Migration is high value
        except:
            pass
        
        # 3. Migration watch list bonus
        if mint in migration_watch_list:
            score += 0.25
        
        # 4. Holder Distribution Score
        holders = await self.get_holder_metrics(mint)
        if holders:
            concentration = holders.get("top10_percent", 100)
            if concentration <= config['max_top_holder_percent']:
                score += 0.20
            elif AGGRESSIVE_MODE and concentration <= config['max_top_holder_percent'] * 1.5:
                score += 0.10
        
        # 5. Volume check
        volume_usd = pool_data.get("volume_24h", 0)
        if volume_usd >= config['min_volume_usd']:
            score += 0.15
        
        return min(1.0, score)
    
    async def get_holder_metrics(self, mint: str) -> Optional[Dict]:
        """Analyze holder distribution"""
        try:
            # Placeholder - implement actual holder analysis
            return {"top10_percent": 45, "unique_holders": 150}
        except:
            return None

# ============================================
# JITO MEV BUNDLE SUPPORT WITH FIX
# ============================================

# FIX 2: Verify JITO_TIP_AMOUNT conversion
JITO_TIP_AMOUNT = int(float(os.getenv("JITO_TIP", "0.001")) * 1e9)  # Convert to lamports properly

class JitoClient:
    """Jito bundle support for MEV protection and priority"""
    
    def __init__(self):
        self.block_engine_url = os.getenv("JITO_URL", "https://mainnet.block-engine.jito.wtf/api/v1")
        self.next_leader = None
        self.tip_amount = JITO_TIP_AMOUNT  # Use the fixed conversion
        self.update_leader_schedule()
    
    def update_leader_schedule(self):
        """Get next Jito leader for bundle submission"""
        self.next_leader = "somevalidator.xyz"
    
    async def send_bundle(self, transactions: List[Any], tip: int = None) -> bool:
        """Send bundle of transactions to Jito"""
        if tip is None:
            # Adjust tip based on mode
            if AGGRESSIVE_MODE:
                tip = self.tip_amount * 2  # Double tip in aggressive mode for speed
            else:
                tip = self.tip_amount
            
        try:
            bundle = {
                "transactions": transactions,
                "tip": tip,
                "leader": self.next_leader
            }
            
            async with httpx.AsyncClient(verify=certifi.where()) as client:
                response = await client.post(
                    f"{self.block_engine_url}/bundles",
                    json=bundle,
                    timeout=5
                )
                
                if response.status_code == 200:
                    logging.info(f"[JITO] Bundle sent with tip: {tip/1e9:.5f} SOL")
                    return True
                else:
                    logging.error(f"[JITO] Bundle failed: {response.text}")
                    return False
        except Exception as e:
            logging.error(f"[JITO] Error: {e}")
            return False

# ============================================
# PORTFOLIO RISK MANAGER - DUAL MODE
# ============================================

class PortfolioRiskManager:
    """Risk management adjusted for aggressive or safe mode"""
    
    def __init__(self):
        self.session_start_balance = None
        self.peak_balance = None
        self.trades_today = 0
        self.losses_today = 0
        self.consecutive_losses = 0
        self.last_reset = datetime.now()
        self.last_drawdown_alert = 0
        self.actual_trades_executed = 0
        
        # Use mode-specific limits
        config = MODE_CONFIG
        self.max_drawdown = config['max_drawdown']
        self.max_daily_loss = config['max_daily_loss']
        self.max_consecutive_losses = config['max_consecutive_losses']
        self.max_trades_per_day = 200 if AGGRESSIVE_MODE else 50
        self.stop_loss_pct = config['stop_loss_pct']
        
        self.position_scaling_enabled = True
        
    async def check_risk_limits(self) -> bool:
        """Return True if safe to trade - adjusted for mode"""
        try:
            from utils import rpc, keypair
            balance = rpc.get_balance(keypair.pubkey()).value / 1e9
            
            # Initialize if first run
            if self.session_start_balance is None:
                self.session_start_balance = balance
                self.peak_balance = balance
                logging.info(f"[RISK] Session started with {balance:.2f} SOL")
            
            # Reset daily counters if new day
            if datetime.now().date() > self.last_reset.date():
                self.trades_today = 0
                self.losses_today = 0
                self.consecutive_losses = 0
                self.actual_trades_executed = 0
                self.last_reset = datetime.now()
                self.session_start_balance = balance
                logging.info("[RISK] Daily counters reset")
            
            # Update peak balance
            if balance > self.peak_balance:
                self.peak_balance = balance
                logging.info(f"[RISK] New peak balance: {balance:.2f} SOL")
            
            # Mode-specific risk handling
            if AGGRESSIVE_MODE:
                # Aggressive: More lenient, don't stop trading
                if self.peak_balance > 0:
                    drawdown = (self.peak_balance - balance) / self.peak_balance
                    if drawdown > self.max_drawdown:
                        current_time = time.time()
                        if current_time - self.last_drawdown_alert > 1800:
                            await send_telegram_alert(
                                f"{MODE_CONFIG['alert_prefix']}\n"
                                f"High drawdown: {drawdown*100:.1f}%\n"
                                f"But continuing for recovery!"
                            )
                            self.last_drawdown_alert = current_time
                        # Reduce position sizes but don't stop
                        self.position_scaling_enabled = False
                
                # Always allow trading in aggressive mode
                return True
                
            else:
                # Safe mode: Strict risk checks
                if self.peak_balance > 0:
                    drawdown = (self.peak_balance - balance) / self.peak_balance
                    if drawdown > self.max_drawdown:
                        current_time = time.time()
                        if current_time - self.last_drawdown_alert > 3600:
                            await send_telegram_alert(
                                f"{MODE_CONFIG['alert_prefix']}\n"
                                f"⚠️ Drawdown: {drawdown*100:.1f}%\n"
                                f"Reducing position sizes..."
                            )
                            self.last_drawdown_alert = current_time
                        self.position_scaling_enabled = False
                        return True  # Still trade but with reduced size
                
                # Check daily loss
                if self.session_start_balance > 0:
                    daily_loss = (self.session_start_balance - balance) / self.session_start_balance
                    if daily_loss > self.max_daily_loss:
                        await send_telegram_alert(
                            f"{MODE_CONFIG['alert_prefix']}\n"
                            f"⛔ Daily loss limit: {daily_loss*100:.1f}%\n"
                            f"Pausing for safety"
                        )
                        return False
                
                # Check consecutive losses
                if self.consecutive_losses >= self.max_consecutive_losses:
                    logging.warning(f"[RISK] {self.consecutive_losses} consecutive losses, cooling off")
                    await asyncio.sleep(60)
                    self.consecutive_losses = 0
            
            # Re-enable scaling if recovered
            if not self.position_scaling_enabled:
                if self.peak_balance > 0:
                    current_drawdown = (self.peak_balance - balance) / self.peak_balance
                    if current_drawdown < 0.2:
                        self.position_scaling_enabled = True
                        await send_telegram_alert(f"{MODE_CONFIG['alert_prefix']}\n✅ Risk levels normalized")
            
            return True
            
        except Exception as e:
            logging.error(f"[RISK] Error checking limits: {e}")
            return True
    
    def record_trade(self, profit: float):
        """Record trade result for risk tracking"""
        if abs(profit) > 0.001:
            self.trades_today += 1
            self.actual_trades_executed += 1
            
            if AGGRESSIVE_MODE:
                aggressive_metrics["trades_executed"] += 1
                
                # Track best/worst trades
                if profit > aggressive_metrics["best_trade"]:
                    aggressive_metrics["best_trade"] = profit
                if profit < aggressive_metrics["worst_trade"]:
                    aggressive_metrics["worst_trade"] = profit
            
            if profit < -0.01:
                self.losses_today += 1
                self.consecutive_losses += 1
                
                if AGGRESSIVE_MODE:
                    aggressive_metrics["losses"] += 1
                    
            elif profit > 0.01:
                self.consecutive_losses = 0
                
                if AGGRESSIVE_MODE:
                    aggressive_metrics["wins"] += 1
    
    async def get_position_size_with_risk(self, mint: str, pool_liquidity: float, ai_score: float = 0.5) -> float:
        """Get position size considering risk parameters"""
        from utils import rpc, keypair
        balance = rpc.get_balance(keypair.pubkey()).value / 1e9
        
        # Use mode-aware position sizing
        size = calculate_position_size_fixed(balance, mint, pool_liquidity)
        
        # Apply risk-based adjustments
        if AGGRESSIVE_MODE:
            # Check if behind schedule (without spamming alerts)
            needs_boost = await check_aggressive_progress(force_alert=False)
            if needs_boost:
                size *= 1.2  # 20% boost
                logging.info(f"[RISK] Boosting position by 20% to catch up")
            
            # If scaling disabled due to drawdown, reduce but don't stop
            if not self.position_scaling_enabled:
                size *= 0.7
                logging.info(f"[RISK] Reducing position due to drawdown")
            
            return max(MODE_CONFIG['min_position_size'], size)
            
        else:
            # Safe mode adjustments
            if not self.position_scaling_enabled:
                size *= 0.5  # Halve position size
            
            # Reduce if many losses
            if self.losses_today > 5:
                size *= 0.7
                logging.info(f"[RISK] Reducing position due to {self.losses_today} losses")
            
            return max(MODE_CONFIG['min_position_size'], size)

# Initialize components
risk_manager = PortfolioRiskManager()
ai_scorer = AIScorer()
jito_client = JitoClient()

# ============================================
# ELITE BUY FUNCTION (Dual Mode)
# ============================================

async def elite_buy_token(mint: str, force_amount: float = None):
    """Elite buy with dual-mode support"""
    try:
        config = MODE_CONFIG
        
        # Check risk limits
        if not await risk_manager.check_risk_limits():
            logging.warning(f"[ELITE] Risk limits exceeded, skipping {mint[:8]}...")
            return False
        
        # Get liquidity data
        try:
            lp_data = await get_liquidity_and_ownership(mint)
            pool_liquidity = lp_data.get("liquidity", 0) if lp_data else 0
            pool_liquidity_usd = pool_liquidity * 150
        except:
            lp_data = {}
            pool_liquidity = 0
            pool_liquidity_usd = 0
        
        # Check mode-specific filters
        if pool_liquidity_usd < config['min_liquidity_usd']:
            logging.info(f"[FILTER] Liquidity too low: ${pool_liquidity_usd:.0f} < ${config['min_liquidity_usd']}")
            return False
        
        # Check holders if data available
        if lp_data and lp_data.get("top_holder_percent", 100) > config['max_top_holder_percent']:
            if not (AGGRESSIVE_MODE and lp_data.get("top_holder_percent") <= config['max_top_holder_percent'] * 1.5):
                logging.info(f"[FILTER] Top holder concentration too high: {lp_data.get('top_holder_percent')}%")
                return False
        
        # AI scoring
        ai_score = await ai_scorer.score_token(mint, lp_data)
        
        # Check minimum AI score
        if ai_score < config['min_ai_score']:
            logging.info(f"[FILTER] AI score too low: {ai_score:.2f} < {config['min_ai_score']}")
            return False
        
        # Determine position size
        if force_amount:
            amount_sol = force_amount
        else:
            amount_sol = await risk_manager.get_position_size_with_risk(mint, pool_liquidity, ai_score)
        
        if amount_sol < 0.01:
            logging.error(f"[ELITE] Position size too small: {amount_sol}")
            return False
        
        # Get Jito tip for MEV protection
        jito_tip = 0
        if os.getenv("USE_JITO_BUNDLES", "true").lower() == "true":
            if AGGRESSIVE_MODE:
                jito_tip = await jito_client.tip_amount * 2 / 1e9  # Double tip for speed
            else:
                jito_tip = jito_client.tip_amount / 1e9
        
        # Execute buy
        logging.info(f"[ELITE] Executing {config['alert_prefix'].split()[0]} buy for {mint[:8]}... with {amount_sol:.3f} SOL")
        
        original_amount = os.getenv("BUY_AMOUNT_SOL")
        os.environ["BUY_AMOUNT_SOL"] = str(amount_sol)
        
        result = await original_buy_token(mint)
        
        if original_amount:
            os.environ["BUY_AMOUNT_SOL"] = original_amount
        
        if result:
            risk_manager.record_trade(0)  # Record with 0 profit initially
            
            await send_telegram_alert(
                f"{config['alert_prefix']}\n"
                f"Token: {mint[:8]}...\n"
                f"Amount: {amount_sol:.3f} SOL\n"
                f"AI Score: {ai_score:.2f}\n"
                f"Liquidity: ${pool_liquidity_usd:.0f}\n"
                f"Risk Status: {'🟢 Normal' if risk_manager.position_scaling_enabled else '🟡 Cautious'}"
            )
            
            logging.info(f"[ELITE] SUCCESS! Bought {mint[:8]}...")
        else:
            risk_manager.record_trade(-amount_sol * 0.01)  # Small loss for failed trade
            logging.error(f"[ELITE] FAILED for {mint[:8]}...")
        
        return result
        
    except Exception as e:
        logging.error(f"[ELITE BUY] Error: {e}")
        return False

# ============================================
# SMART EXIT STRATEGY (Dual Mode)
# ============================================

class SmartExitStrategy:
    async def calculate_exit_strategy(self, mint: str, entry_price: float) -> Dict:
        """Calculate mode-specific exit strategy"""
        config = MODE_CONFIG
        
        return {
            "target_1": entry_price * config['take_profit_1'],
            "target_1_percent": config['partial_exit_1'],
            "target_2": entry_price * config['take_profit_2'],
            "target_2_percent": config['partial_exit_2'],
            "target_3": entry_price * config['take_profit_3'],
            "target_3_percent": config['partial_exit_3'],
            "stop_loss": entry_price * (1 - config['stop_loss_pct']),
            "strategy": "AGGRESSIVE" if AGGRESSIVE_MODE else "SAFE"
        }

exit_strategy = SmartExitStrategy()

# ============================================
# WEB SERVER WITH WEBHOOK
# ============================================

app = FastAPI()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
AUTHORIZED_USER_ID = int(os.getenv("TELEGRAM_USER_ID") or os.getenv("TELEGRAM_CHAT_ID", 0))

@app.get("/")
async def health_check():
    """Health check endpoint for Render"""
    mode = "AGGRESSIVE" if is_aggressive_mode_active() else "SAFE"
    return {
        "status": f"🚀 Dual Mode Sniper Active - {mode} Mode",
        "mode": mode,
        "features": "All Systems Operational",
        "risk_settings": f"Drawdown: {risk_manager.max_drawdown*100:.0f}%, Daily Loss: {risk_manager.max_daily_loss*100:.0f}%"
    }

@app.get("/status")
async def status():
    """Status endpoint with metrics"""
    return {
        "bot": "running" if is_bot_running() else "paused",
        "mode": "aggressive" if is_aggressive_mode_active() else "safe",
        "trades_today": risk_manager.trades_today,
        "actual_trades": risk_manager.actual_trades_executed,
        "consecutive_losses": risk_manager.consecutive_losses,
        "risk_status": "normal" if risk_manager.position_scaling_enabled else "cautious",
        "pumpfun_tracking": len(pumpfun_tokens),
        "migration_watch": len(migration_watch_list)
    }

@app.post("/webhook")
@app.post("/")
async def telegram_webhook(request: Request):
    """Handle Telegram commands"""
    global AGGRESSIVE_MODE, AGGRESSIVE_START_TIME, risk_manager, MODE_CONFIG
    
    try:
        data = await request.json()
        message = data.get("message") or data.get("edited_message")
        if not message:
            return {"ok": True}
        
        user_id = message["from"]["id"]
        text = message.get("text", "")
        
        if user_id != AUTHORIZED_USER_ID:
            return {"ok": True}
        
        logging.info(f"[TELEGRAM] Command: {text}")
        
        # Command routing
        if text == "/start":
            if is_bot_running():
                await send_telegram_alert("✅ Bot already running")
            else:
                start_bot()
                mode = "AGGRESSIVE" if is_aggressive_mode_active() else "SAFE"
                await send_telegram_alert(f"✅ Bot started in {mode} mode! 💰")
                
        elif text == "/stop":
            if not is_bot_running():
                await send_telegram_alert("⏸ Bot already paused")
            else:
                stop_bot()
                await stop_all_tasks()
                await send_telegram_alert("🛑 Bot stopped")
                
        elif text == "/status":
            try:
                status_msg = get_bot_status_message()
                
                # Add mode-specific stats
                mode_stats = f"\n📊 MODE: {'AGGRESSIVE' if AGGRESSIVE_MODE else 'SAFE'}\n"
                config = MODE_CONFIG
                mode_stats += f"• Position Size: {config['min_position_size']:.2f}-{config['max_position_size']:.2f} SOL\n"
                mode_stats += f"• Min Liquidity: ${config['min_liquidity_usd']}\n"
                mode_stats += f"• Min AI Score: {config['min_ai_score']}\n"
                mode_stats += f"• Stop Loss: {config['stop_loss_pct']*100:.0f}%\n"
                mode_stats += f"• Targets: {config['take_profit_1']}x, {config['take_profit_2']}x, {config['take_profit_3']}x\n"
                
                # Add aggressive progress if active
                if is_aggressive_mode_active():
                    hours_elapsed = (time.time() - AGGRESSIVE_START_TIME) / 3600
                    hours_remaining = AGGRESSIVE_DURATION_HOURS - hours_elapsed
                    
                    mode_stats += f"\n⚡ AGGRESSIVE PROGRESS ⚡\n"
                    mode_stats += f"• Time Left: {hours_remaining:.1f}h\n"
                    mode_stats += f"• Progress: {aggressive_metrics['current_multiplier']:.2f}x/{TARGET_MULTIPLIER}x\n"
                    mode_stats += f"• Trades: {aggressive_metrics['trades_executed']}\n"
                    mode_stats += f"• Win Rate: {(aggressive_metrics['wins']/max(1, aggressive_metrics['trades_executed'])*100):.0f}%\n"
                
                # Add risk status
                risk_status = f"\n⚡ RISK STATUS:\n"
                risk_status += f"• Trades Today: {risk_manager.trades_today}\n"
                risk_status += f"• Losses Today: {risk_manager.losses_today}\n"
                risk_status += f"• Consecutive Losses: {risk_manager.consecutive_losses}\n"
                risk_status += f"• Scaling: {'ON' if risk_manager.position_scaling_enabled else 'CAUTIOUS'}\n"
                
                await send_telegram_alert(f"{status_msg}{mode_stats}{risk_status}")
                
            except Exception as e:
                logging.error(f"Status error: {e}")
                await send_telegram_alert("📊 Bot status temporarily unavailable")
                    
        elif text.startswith("/forcebuy "):
            parts = text.split(" ")
            if len(parts) >= 2:
                mint = parts[1].strip()
                config = MODE_CONFIG
                await send_telegram_alert(f"{config['alert_prefix']}\nForce buying: {mint}")
                asyncio.create_task(start_sniper_with_forced_token(mint))
                
        elif text == "/wallet":
            summary = get_wallet_summary()
            await send_telegram_alert(f"👛 Wallet:\n{summary}")
            
        elif text == "/aggressive":
            # Toggle aggressive mode
            AGGRESSIVE_MODE = not AGGRESSIVE_MODE
            os.environ["AGGRESSIVE_MODE"] = "true" if AGGRESSIVE_MODE else "false"
            
            if AGGRESSIVE_MODE:
                # Starting aggressive mode
                AGGRESSIVE_START_TIME = time.time()
                aggressive_metrics["start_balance"] = None
                aggressive_metrics["trades_executed"] = 0
                aggressive_metrics["wins"] = 0
                aggressive_metrics["losses"] = 0
                
                # Reinitialize components with new mode
                MODE_CONFIG = get_mode_config()
                risk_manager.__init__()
                
                await send_telegram_alert(
                    f"🚨 AGGRESSIVE MODE ACTIVATED 🚨\n\n"
                    f"Target: {TARGET_MULTIPLIER}x in {AGGRESSIVE_DURATION_HOURS}h\n"
                    f"Position Sizes: {MODE_CONFIG['min_position_size']*100:.0f}-{MODE_CONFIG['max_position_size']*100:.0f}%\n"
                    f"Min Liquidity: ${MODE_CONFIG['min_liquidity_usd']}\n"
                    f"Risk Limits: Relaxed\n\n"
                    f"LET'S GO! 🚀"
                )
            else:
                # Switching to safe mode
                MODE_CONFIG = get_mode_config()
                risk_manager.__init__()
                
                await send_telegram_alert(
                    f"🟢 SAFE MODE ACTIVATED 🟢\n\n"
                    f"Target: Steady 2 SOL/day\n"
                    f"Position Sizes: {MODE_CONFIG['min_position_size']*100:.0f}-{MODE_CONFIG['max_position_size']*100:.0f}%\n"
                    f"Min Liquidity: ${MODE_CONFIG['min_liquidity_usd']}\n"
                    f"Risk Limits: Normal"
                )
                
        elif text == "/progress":
            if is_aggressive_mode_active():
                await check_aggressive_progress(force_alert=True)
            else:
                await send_telegram_alert("Aggressive mode not active. Use /aggressive to enable.")
            
        elif text == "/config":
            config = MODE_CONFIG
            mode = "AGGRESSIVE" if AGGRESSIVE_MODE else "SAFE"
            config_msg = f"""
⚙️ Current Configuration ({mode} MODE):
━━━━━━━━━━━━━━━━━━━━━━━━━
FILTERS:
• Min Liquidity: ${config['min_liquidity_usd']}
• Min Volume: ${config['min_volume_usd']}
• Min Holders: {config['min_holder_count']}
• Max Top Holder: {config['max_top_holder_percent']}%
• Min AI Score: {config['min_ai_score']}

POSITION SIZING:
• Min Size: {config['min_position_size']:.2f} SOL
• Max Size: {config['max_position_size']:.2f} SOL

PROFIT TARGETS:
• Target 1: {config['take_profit_1']}x ({config['partial_exit_1']}%)
• Target 2: {config['take_profit_2']}x ({config['partial_exit_2']}%)
• Target 3: {config['take_profit_3']}x ({config['partial_exit_3']}%)
• Stop Loss: {config['stop_loss_pct']*100:.0f}%

RISK LIMITS:
• Max Drawdown: {config['max_drawdown']*100:.0f}%
• Max Daily Loss: {config['max_daily_loss']*100:.0f}%
• Max Consecutive Losses: {config['max_consecutive_losses']}
━━━━━━━━━━━━━━━━━━━━━━━━━
"""
            await send_telegram_alert(config_msg)
            
        elif text == "/help":
            help_text = """
📚 DUAL MODE SNIPER COMMANDS:
━━━━━━━━━━━━━━━━━━━━━━━━━
/start - Start the bot
/stop - Stop the bot  
/status - Get bot status
/wallet - Check wallet balance
/forcebuy <MINT> - Force buy a token
/aggressive - Toggle mode (Aggressive/Safe)
/progress - Check aggressive progress
/config - Show current configuration
/help - Show this message

Current Mode: """ + ("🚨 AGGRESSIVE" if AGGRESSIVE_MODE else "🟢 SAFE")
            
            await send_telegram_alert(help_text)
            
        return {"ok": True}
        
    except Exception as e:
        logging.error(f"Webhook error: {e}")
        return {"ok": True}

# ============================================
# ELITE SNIPER LAUNCHER
# ============================================

async def start_elite_sniper():
    """Start the elite sniper with dual mode support"""
    
    log_configuration()
    
    mode = "AGGRESSIVE (12-HOUR SPRINT)" if is_aggressive_mode_active() else "SAFE (STEADY PROFIT)"
    config = MODE_CONFIG
    
    features_list = [
        f"✅ {mode} Mode Active",
        "✅ Smart Token Detection",
        "✅ PumpFun Migration Sniper",
        "✅ Dynamic Position Sizing",
        "✅ Multi-DEX Support",
        "✅ Auto Profit Taking",
        "✅ Risk Management",
        f"✅ Mode: {config['alert_prefix'].split()[0]}"
    ]
    
    await send_telegram_alert(
        f"💰 DUAL MODE SNIPER STARTING 💰\n\n"
        f"Mode: {mode}\n"
        f"Features Active:\n" + "\n".join(features_list) + "\n\n"
        f"Position Sizes: {config['min_position_size']:.2f}-{config['max_position_size']:.2f} SOL\n"
        f"Min Liquidity: ${config['min_liquidity_usd']}\n"
        f"Profit Targets: {config['take_profit_1']}x, {config['take_profit_2']}x, {config['take_profit_3']}x\n\n"
        f"Initializing systems..."
    )
    
    # Initialize aggressive metrics if in aggressive mode
    if is_aggressive_mode_active():
        try:
            from utils import rpc, keypair
            balance = rpc.get_balance(keypair.pubkey()).value / 1e9
            aggressive_metrics["start_balance"] = balance
            aggressive_metrics["target_balance"] = balance * TARGET_MULTIPLIER
            await send_telegram_alert(
                f"🚨 AGGRESSIVE MODE 🚨\n"
                f"Starting Balance: {balance:.2f} SOL\n"
                f"Target: {aggressive_metrics['target_balance']:.2f} SOL ({TARGET_MULTIPLIER}x)\n"
                f"Timeframe: {AGGRESSIVE_DURATION_HOURS} hours"
            )
        except:
            pass
    
    # Replace buy function with elite version
    import utils
    utils.buy_token = elite_buy_token
    try:
        import sniper_logic
        sniper_logic.buy_token = elite_buy_token
    except:
        pass
    
    # Start core listeners
    tasks = []
    tasks.extend([
        asyncio.create_task(mempool_listener("Raydium")),
        asyncio.create_task(mempool_listener("PumpFun")),
        asyncio.create_task(mempool_listener("Moonshot")),
        asyncio.create_task(trending_scanner())
    ])
    
    # Add Momentum Scanner
    try:
        from momentum_scanner import momentum_scanner
        if os.getenv("MOMENTUM_SCANNER", "true").lower() == "true":
            tasks.append(asyncio.create_task(momentum_scanner()))
            await send_telegram_alert("🔥 MOMENTUM SCANNER: ACTIVE")
    except Exception as e:
        logging.warning(f"Momentum scanner not available: {e}")
    
    # Add PumpFun migration monitor
    if os.getenv("ENABLE_PUMPFUN_MIGRATION", "true").lower() == "true":
        tasks.append(asyncio.create_task(pumpfun_migration_monitor()))
        await send_telegram_alert("🎯 PumpFun Migration Monitor: ACTIVE")
    
    # Add DexScreener monitor
    try:
        from dexscreener_monitor import start_dexscreener_monitor
        tasks.append(asyncio.create_task(start_dexscreener_monitor()))
    except:
        pass
    
    # Add monitoring tasks
    tasks.append(asyncio.create_task(performance_monitor()))
    tasks.append(asyncio.create_task(risk_performance_monitor()))
    
    # Aggressive progress monitoring
    if is_aggressive_mode_active():
        tasks.append(asyncio.create_task(aggressive_progress_monitor()))
    
    await send_telegram_alert(
        f"🚀 {config['alert_prefix']} 🚀\n\n"
        f"Bot READY!\n"
        f"Active Strategies: {len(tasks)}\n\n"
        f"Hunting for profits... 💰"
    )
    
    # Run all tasks
    await asyncio.gather(*tasks)

# ============================================
# MONITORING TASKS
# ============================================

async def aggressive_progress_monitor():
    """Monitor aggressive mode progress"""
    while is_aggressive_mode_active():
        await asyncio.sleep(1800)  # Every 30 minutes
        
        needs_boost = await check_aggressive_progress(force_alert=True)
        
        if needs_boost:
            # Alert that we need more aggression
            await send_telegram_alert(
                f"📈 NEED MORE AGGRESSION\n"
                f"Behind schedule - consider:\n"
                f"• Taking more trades\n"
                f"• Increasing position sizes\n"
                f"• Lowering filter requirements"
            )

async def risk_performance_monitor():
    """Monitor risk metrics"""
    while True:
        try:
            await asyncio.sleep(60)  # Check every minute
            
            from utils import rpc, keypair
            balance = rpc.get_balance(keypair.pubkey()).value / 1e9
            
            # Check for profit milestones
            if risk_manager.session_start_balance:
                session_profit = balance - risk_manager.session_start_balance
                
                if session_profit > 5 and risk_manager.actual_trades_executed == 5:
                    await send_telegram_alert(
                        f"🎯 Early profit!\n"
                        f"Profit: +{session_profit:.2f} SOL\n"
                        f"Only 5 trades executed!"
                    )
                    
        except Exception as e:
            logging.error(f"[Risk Monitor] Error: {e}")
            await asyncio.sleep(60)

async def performance_monitor():
    """Track and report performance"""
    while True:
        await asyncio.sleep(3600 if not AGGRESSIVE_MODE else 1800)  # Every 30min in aggressive
        
        try:
            from utils import rpc, keypair
            balance = rpc.get_balance(keypair.pubkey()).value / 1e9
            
            mode = "AGGRESSIVE" if AGGRESSIVE_MODE else "SAFE"
            config = MODE_CONFIG
            
            report = f"""
📊 PERFORMANCE REPORT - {mode} MODE 📊
━━━━━━━━━━━━━━━━━━━━━━━━━
Balance: {balance:.2f} SOL
Trades Today: {risk_manager.trades_today}
Consecutive Losses: {risk_manager.consecutive_losses}
Risk Status: {'🟢 Normal' if risk_manager.position_scaling_enabled else '🟡 Cautious'}
"""
            
            if AGGRESSIVE_MODE and aggressive_metrics["start_balance"]:
                report += f"""
━━━━━━━━━━━━━━━━━━━━━━━━━
AGGRESSIVE STATS:
Progress: {aggressive_metrics['current_multiplier']:.2f}x / {TARGET_MULTIPLIER}x
Trades: {aggressive_metrics['trades_executed']}
Win Rate: {(aggressive_metrics['wins']/max(1, aggressive_metrics['trades_executed'])*100):.0f}%
"""
            
            await send_telegram_alert(report)
            
        except Exception as e:
            logging.error(f"[Performance Monitor] Error: {e}")

# ============================================
# MAIN ENTRY
# ============================================

async def run_bot_with_web_server():
    """Run bot with web server"""
    asyncio.create_task(start_elite_sniper())
    
    if BOT_TOKEN:
        try:
            webhook_url = f"https://sniper-bot-web.onrender.com/webhook"
            
            async with httpx.AsyncClient(verify=certifi.where()) as client:
                response = await client.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
                    json={"url": webhook_url}
                )
                if response.status_code == 200:
                    logging.info(f"[TELEGRAM] Webhook set to {webhook_url}")
        except Exception as e:
            logging.error(f"[TELEGRAM] Webhook setup error: {e}")
    
    port = int(os.getenv("PORT", 10000))
    config = uvicorn.Config(
        app, 
        host="0.0.0.0", 
        port=port,
        log_level="warning"
    )
    server = uvicorn.Server(config)
    
    logging.info(f"Starting web server on port {port}")
    await server.serve()

async def main():
    """Main entry point"""
    
    if not os.getenv("HELIUS_API"):
        print("ERROR: HELIUS_API not set")
        return
    
    if not os.getenv("SOLANA_PRIVATE_KEY"):
        print("ERROR: SOLANA_PRIVATE_KEY not set")
        return
    
    mode = "AGGRESSIVE" if AGGRESSIVE_MODE else "SAFE"
    config = MODE_CONFIG
    
    if AGGRESSIVE_MODE:
        print(f"""
╔══════════════════════════════════════════╗
║    🚨 AGGRESSIVE MODE ACTIVATED 🚨      ║
║    Target: {TARGET_MULTIPLIER}x in {AGGRESSIVE_DURATION_HOURS:.0f} hours          ║
║                                          ║
║  Position Sizes: 10-25% per trade       ║
║  Min Liquidity: ${config['min_liquidity_usd']}                     ║
║  Stop Loss: {config['stop_loss_pct']*100:.0f}%                         ║
║  Targets: {config['take_profit_1']}x, {config['take_profit_2']}x, {config['take_profit_3']}x            ║
║                                          ║
║  After {AGGRESSIVE_DURATION_HOURS:.0f} hours, set:                   ║
║  AGGRESSIVE_MODE=false                  ║
║  To revert to safe trading              ║
╚══════════════════════════════════════════╝
        """)
    else:
        print(f"""
╔══════════════════════════════════════════╗
║    🟢 SAFE MODE - STEADY PROFITS 🟢     ║
║                                          ║
║  Position Sizes: 5-10% per trade        ║
║  Min Liquidity: ${config['min_liquidity_usd']}                    ║
║  Stop Loss: {config['stop_loss_pct']*100:.0f}%                         ║
║  Targets: {config['take_profit_1']}x, {config['take_profit_2']}x, {config['take_profit_3']}x             ║
║  Target: 2 SOL/day (~$300)              ║
╚══════════════════════════════════════════╝
        """)
    
    logging.info("=" * 50)
    logging.info(f"STARTING IN {mode} MODE!")
    logging.info("=" * 50)
    
    await run_bot_with_web_server()

# ============================================
# SIGNAL HANDLERS
# ============================================

import signal
import sys

def signal_handler(sig, frame):
    """Handle shutdown signals gracefully"""
    logging.info("Shutdown signal received, cleaning up...")
    asyncio.create_task(cleanup())
    sys.exit(0)

async def cleanup():
    """Clean up resources on shutdown"""
    try:
        await stop_all_tasks()
        
        # Final report
        if risk_manager.session_start_balance:
            from utils import rpc, keypair
            try:
                final_balance = rpc.get_balance(keypair.pubkey()).value / 1e9
                session_profit = final_balance - risk_manager.session_start_balance
                
                final_stats = (
                    f"📊 FINAL SESSION STATS\n"
                    f"Mode: {'AGGRESSIVE' if AGGRESSIVE_MODE else 'SAFE'}\n"
                    f"Session P&L: {session_profit:+.2f} SOL\n"
                    f"Final Balance: {final_balance:.2f} SOL\n"
                )
                
                if AGGRESSIVE_MODE and aggressive_metrics["start_balance"]:
                    final_stats += f"\n🚨 AGGRESSIVE RESULTS 🚨\n"
                    final_stats += f"Target: {aggressive_metrics['target_balance']:.2f} SOL\n"
                    final_stats += f"Achieved: {aggressive_metrics['current_multiplier']:.2f}x\n"
                
                await send_telegram_alert(final_stats)
            except:
                pass
    except:
        pass

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ============================================
# ENTRY POINT
# ============================================

if __name__ == "__main__":
    log_level = logging.DEBUG if os.getenv("DEBUG", "false").lower() == "true" else logging.INFO
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Bot stopped by user")
    except Exception as e:
        logging.error(f"Fatal error: {e}")
        import traceback
        traceback.print_exc()
