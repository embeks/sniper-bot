"""
Main Orchestrator - PROBE/CONFIRM + DYNAMIC TPS + HEALTH CHECK + TRAILING STOP
✅ NEW: Split entry (0.03 SOL probe + 0.05 SOL confirm)
✅ NEW: Health check moved to 8-12s
✅ NEW: Dynamic TP levels based on entry MC
✅ NEW: Trailing stop after first TP
✅ NEW: Timer extended to 25-33s with auto-extend
"""

import asyncio
import logging
import signal
import time
import random
from datetime import datetime
from typing import Dict, Optional, List

from config import (
    LOG_LEVEL, LOG_FORMAT,
    BUY_AMOUNT_SOL, MAX_POSITIONS, MIN_SOL_BALANCE,
    STOP_LOSS_PERCENTAGE, TAKE_PROFIT_PERCENTAGE,
    SELL_DELAY_SECONDS, MAX_POSITION_AGE_SECONDS,
    MONITOR_CHECK_INTERVAL, DATA_FAILURE_TOLERANCE,
    DRY_RUN, ENABLE_TELEGRAM_NOTIFICATIONS,
    BLACKLISTED_TOKENS, NOTIFY_PROFIT_THRESHOLD,
    PARTIAL_TAKE_PROFIT, LIQUIDITY_MULTIPLIER,
    MIN_LIQUIDITY_SOL, MAX_SLIPPAGE_PERCENT,
    VELOCITY_MIN_SOL_PER_SECOND, VELOCITY_MIN_BUYERS, VELOCITY_MAX_TOKEN_AGE,
    TIMER_EXIT_BASE_SECONDS, TIMER_EXIT_VARIANCE_SECONDS,
    TIMER_EXTENSION_SECONDS, TIMER_EXTENSION_PNL_THRESHOLD, TIMER_MAX_EXTENSIONS,
    FAIL_FAST_CHECK_TIME, FAIL_FAST_PNL_THRESHOLD, FAIL_FAST_VELOCITY_THRESHOLD,
    VELOCITY_MIN_RECENT_1S_SOL, VELOCITY_MIN_RECENT_3S_SOL, VELOCITY_MAX_DROP_PERCENT
)

# ✅ NEW: Import probe/confirm settings
from config import (
    ENABLE_PROBE_ENTRY, PROBE_AMOUNT_SOL, CONFIRM_AMOUNT_SOL,
    PROBE_SKIP_HELIUS, CONFIRM_DELAY_SECONDS,
    CONFIRM_MIN_VELOCITY_RATIO, CONFIRM_MIN_BUYER_DELTA
)

# ✅ NEW: Import health check settings
from config import (
    HEALTH_CHECK_START, HEALTH_CHECK_END,
    HEALTH_CHECK_VELOCITY_THRESHOLD, HEALTH_CHECK_MC_THRESHOLD
)

# ✅ NEW: Import dynamic TP settings
from config import (
    ENABLE_DYNAMIC_TPS, TP_LEVELS_EARLY, TP_LEVELS_MID, TP_LEVELS_LATE,
    TP_SELL_PERCENTS, TP_COOLDOWN_SECONDS
)

# ✅ NEW: Import trailing stop settings
from config import (
    ENABLE_TRAILING_STOP, TRAILING_STOP_DRAWDOWN
)

# ✅ NEW: Import timer auto-extend
from config import TIMER_AUTO_EXTEND_TO

# Import profit protection settings
try:
    from config import EXTREME_TP_PERCENT, TRAIL_START_PERCENT, TRAIL_GIVEBACK_PERCENT
except ImportError:
    EXTREME_TP_PERCENT = 150.0
    TRAIL_START_PERCENT = 100.0
    TRAIL_GIVEBACK_PERCENT = 50.0

from wallet import WalletManager
from dex import PumpFunDEX
from pumpportal_monitor import PumpPortalMonitor
from pumpportal_trader import PumpPortalTrader
from performance_tracker import PerformanceTracker
from curve_reader import BondingCurveReader
from velocity_checker import VelocityChecker

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format=LOG_FORMAT,
    handlers=[logging.StreamHandler()]
)

logger = logging.getLogger(__name__)

class Position:
    """Track an active position with probe/confirm and advanced exit strategies"""
    def __init__(self, mint: str, amount_sol: float, tokens: float = 0, entry_market_cap: float = 0):
        self.mint = mint
        self.amount_sol = amount_sol
        self.initial_tokens = tokens
        self.remaining_tokens = tokens
        self.entry_time = time.time()
        self.entry_price = 0
        self.current_price = 0
        self.pnl_percent = 0
        self.pnl_usd = 0
        self.status = 'active'
        self.buy_signature = None
        self.sell_signatures = []
        self.monitor_task = None
        self.exit_time = None
        self.extensions_used = 0
        self.max_pnl_reached = 0
        self.fail_fast_checked = False
        self.partial_sells = {}
        self.pending_sells = set()
        self.pending_token_amounts = {}
        self.total_sold_percent = 0
        self.realized_pnl_sol = 0
        self.is_closing = False
        self.retry_counts = {}
        self.last_valid_price = 0
        self.last_price_update = time.time()
        self.consecutive_stale_reads = 0
        self.last_valid_balance = tokens
        self.curve_check_retries = 0
        self.consecutive_no_movement = 0
        self.last_checked_price = 0
        self.entry_market_cap = entry_market_cap
        self.current_market_cap = entry_market_cap
        self.entry_sol_in_curve = 0
        
        # Existing source tracking
        self.has_chain_price = False
        self.last_price_source = "unknown"
        self.sl_chain_debounce = 0
        
        self.entry_token_price_sol = 0
        
        # ✅ NEW: Probe/confirm tracking
        self.probe_completed = False
        self.confirm_completed = False
        self.probe_tokens = 0
        self.probe_sol = 0
        
        # ✅ NEW: Health check tracking
        self.health_checked = False
        
        # ✅ NEW: Trailing stop tracking
        self.first_tp_taken = False
        self.peak_mc_since_first_tp = 0
        
        # ✅ NEW: TP cooldown
        self.tp_cooldown_until = 0

class SniperBot:
    """Main sniper bot with probe/confirm entry and advanced exits"""
    
    def __init__(self):
        """Initialize all components"""
        logger.info("=" * 60)
        logger.info("🚀 INITIALIZING SNIPER BOT - PROBE/CONFIRM + DYNAMIC TPS")
        logger.info("=" * 60)
        
        self.wallet = WalletManager()
        self.dex = PumpFunDEX(self.wallet)
        self.scanner = None
        self.scanner_task = None
        self.telegram = None
        self.telegram_polling_task = None
        self.tracker = PerformanceTracker()
        
        from solana.rpc.api import Client
        from config import RPC_ENDPOINT, PUMPFUN_PROGRAM_ID
        
        rpc_client = Client(RPC_ENDPOINT.replace('wss://', 'https://').replace('ws://', 'http://'))
        self.curve_reader = BondingCurveReader(rpc_client, PUMPFUN_PROGRAM_ID)
        
        self.velocity_checker = VelocityChecker(
            min_sol_per_second=VELOCITY_MIN_SOL_PER_SECOND,
            min_unique_buyers=VELOCITY_MIN_BUYERS,
            max_token_age_seconds=VELOCITY_MAX_TOKEN_AGE,
            min_recent_1s_sol=VELOCITY_MIN_RECENT_1S_SOL,
            min_recent_3s_sol=VELOCITY_MIN_RECENT_3S_SOL,
            max_drop_percent=VELOCITY_MAX_DROP_PERCENT,
            min_snapshots=1  
        )
        
        client = Client(RPC_ENDPOINT.replace('wss://', 'https://').replace('ws://', 'http://'))
        self.trader = PumpPortalTrader(self.wallet, client)
        
        self.positions: Dict[str, Position] = {}
        self.pending_buys = 0
        self.total_trades = 0
        self.profitable_trades = 0
        self.total_pnl = 0
        self.total_realized_sol = 0
        self.MAX_POSITIONS = MAX_POSITIONS
        
        self.running = False
        self.paused = False
        self.shutdown_requested = False
        self._last_balance_warning = 0
        
        self.consecutive_losses = 0
        self.session_loss_count = 0
        
        self.telegram_enabled = ENABLE_TELEGRAM_NOTIFICATIONS
        
        self._log_startup_info()
    
    def _log_startup_info(self):
        """Log startup information"""
        sol_balance = self.wallet.get_sol_balance()
        tradeable_balance = max(0, sol_balance - MIN_SOL_BALANCE)
        max_trades = int(tradeable_balance / BUY_AMOUNT_SOL) if tradeable_balance > 0 else 0
        actual_trades = min(max_trades, MAX_POSITIONS) if max_trades > 0 else 0
        
        logger.info(f"📊 STARTUP STATUS:")
        logger.info(f"  • Strategy: PROBE/CONFIRM + DYNAMIC TPS + HEALTH CHECK + TRAILING")
        
        if ENABLE_PROBE_ENTRY:
            logger.info(f"  • Entry: {PROBE_AMOUNT_SOL} SOL probe + {CONFIRM_AMOUNT_SOL} SOL confirm = {BUY_AMOUNT_SOL} SOL total")
            logger.info(f"  • Probe: Skip Helius for speed")
            logger.info(f"  • Confirm: After {CONFIRM_DELAY_SECONDS}s + momentum + Helius check")
        else:
            logger.info(f"  • Entry: Single {BUY_AMOUNT_SOL} SOL buy")
        
        logger.info(f"  • Velocity gate: ≥{VELOCITY_MIN_SOL_PER_SECOND} SOL/s avg, ≥{VELOCITY_MIN_BUYERS} buyers")
        logger.info(f"  • Health check: {HEALTH_CHECK_START}-{HEALTH_CHECK_END}s (velocity {HEALTH_CHECK_VELOCITY_THRESHOLD*100:.0f}%, MC {HEALTH_CHECK_MC_THRESHOLD*100:.0f}%)")
        logger.info(f"  • Fail-fast: {FAIL_FAST_CHECK_TIME}s @ {FAIL_FAST_PNL_THRESHOLD}% or {FAIL_FAST_VELOCITY_THRESHOLD}% velocity")
        
        if ENABLE_DYNAMIC_TPS:
            logger.info(f"  • Dynamic TPs: Early {TP_LEVELS_EARLY}, Mid {TP_LEVELS_MID}, Late {TP_LEVELS_LATE}")
            logger.info(f"  • TP sizes: {TP_SELL_PERCENTS}%")
        
        if ENABLE_TRAILING_STOP:
            logger.info(f"  • Trailing stop: -{TRAILING_STOP_DRAWDOWN}% from peak after first TP")
        
        logger.info(f"  • Timer: {TIMER_EXIT_BASE_SECONDS}s ±{TIMER_EXIT_VARIANCE_SECONDS}s → auto-extend to {TIMER_AUTO_EXTEND_TO}s if strong")
        logger.info(f"  • Wallet: {self.wallet.pubkey}")
        logger.info(f"  • Balance: {sol_balance:.4f} SOL")
        logger.info(f"  • Max positions: {MAX_POSITIONS}")
        logger.info(f"  • Available trades: {actual_trades}")
        logger.info(f"  • Mode: {'DRY RUN' if DRY_RUN else 'LIVE TRADING'}")
        logger.info("=" * 60)
    
    def _calculate_mc_from_curve(self, curve_data: dict, sol_price_usd: float = 250) -> float:
        """Calculate market cap from bonding curve data"""
        try:
            v_sol = curve_data.get('sol_in_curve', 0)
            v_tokens = curve_data.get('virtual_token_reserves', 0)
            
            if v_sol == 0 or v_tokens == 0:
                return 0
            
            price_sol = (v_sol * 1e9) / v_tokens
            total_supply = 1_000_000_000
            market_cap_usd = total_supply * price_sol * sol_price_usd
            
            return market_cap_usd
        except Exception as e:
            logger.error(f"MC calculation error: {e}")
            return 0
    
    def _calculate_token_price_from_mc(self, market_cap_usd: float, sol_price_usd: float = 250) -> float:
        """Calculate token price in SOL from market cap"""
        try:
            if market_cap_usd == 0:
                return 0
            
            total_supply = 1_000_000_000
            price_per_token_usd = market_cap_usd / total_supply
            price_per_token_sol = price_per_token_usd / sol_price_usd
            
            return price_per_token_sol
        except Exception as e:
            logger.error(f"Token price calculation error: {e}")
            return 0
    
    def _get_current_token_price(self, mint: str, curve_data: dict) -> Optional[float]:
        """Calculate current token price - returns lamports per atomic"""
        try:
            if not curve_data:
                return None
            
            price_lamports_per_atomic = curve_data.get('price_lamports_per_atomic', 0)
            
            if price_lamports_per_atomic is None or price_lamports_per_atomic <= 0:
                logger.debug(f"Invalid price from curve data")
                return None
            
            logger.debug(
                f"Current price for {mint[:8]}...: "
                f"{price_lamports_per_atomic:.10f} lamports/atomic"
            )
            
            return price_lamports_per_atomic
            
        except Exception as e:
            logger.error(f"Error getting token price for {mint[:8]}: {e}")
            return None
    
    async def _get_transaction_deltas(self, signature: str, mint: str) -> dict:
        """Read transaction metadata from blockchain"""
        try:
            from solders.signature import Signature as SoldersSignature
            
            tx_sig = SoldersSignature.from_string(signature)
            
            tx_response = self.trader.client.get_transaction(
                tx_sig,
                encoding="jsonParsed",
                max_supported_transaction_version=0
            )
            
            if not tx_response or not tx_response.value:
                return {"confirmed": False, "sol_delta": 0.0, "token_delta": 0.0}
            
            tx = tx_response.value
            
            if tx.transaction.meta is None or tx.transaction.meta.err is not None:
                return {"confirmed": False, "sol_delta": 0.0, "token_delta": 0.0}
            
            meta = tx.transaction.meta
            my_pubkey_str = str(self.wallet.pubkey)
            
            sol_delta = 0.0
            account_keys = [str(key) for key in tx.transaction.transaction.message.account_keys]
            
            try:
                wallet_index = account_keys.index(my_pubkey_str)
                pre_sol_lamports = meta.pre_balances[wallet_index]
                post_sol_lamports = meta.post_balances[wallet_index]
                sol_delta = (post_sol_lamports - pre_sol_lamports) / 1e9
            except (ValueError, IndexError) as e:
                logger.warning(f"Wallet not found in transaction accounts: {e}")
            
            token_delta = 0.0
            pre_token_amount = 0.0
            post_token_amount = 0.0
            
            for balance in (meta.pre_token_balances or []):
                if balance.mint == mint and balance.owner == my_pubkey_str:
                    ui_amount = balance.ui_token_amount.ui_amount
                    pre_token_amount = float(ui_amount) if ui_amount is not None else 0.0
                    break
            
            for balance in (meta.post_token_balances or []):
                if balance.mint == mint and balance.owner == my_pubkey_str:
                    ui_amount = balance.ui_token_amount.ui_amount
                    post_token_amount = float(ui_amount) if ui_amount is not None else 0.0
                    break
            
            token_delta = post_token_amount - pre_token_amount
            
            return {"confirmed": True, "sol_delta": sol_delta, "token_delta": token_delta}
            
        except Exception as e:
            logger.error(f"Error getting transaction deltas: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            return {"confirmed": False, "sol_delta": 0.0, "token_delta": 0.0}
    
    # ✅ NEW: Helper function for velocity EWMA
    def _calculate_velocity_ewma(self, mint: str, alpha: float = 0.5, window: float = 3.0) -> float:
        """Calculate exponential weighted moving average of velocity"""
        try:
            history = self.velocity_checker.velocity_history.get(mint, [])
            if len(history) < 2:
                return 0.0
            
            current_time = time.time()
            cutoff_time = current_time - window
            
            # Filter to window
            recent = [h for h in history if h['timestamp'] >= cutoff_time]
            if len(recent) < 2:
                return 0.0
            
            # Calculate velocities
            velocities = []
            for i in range(1, len(recent)):
                dt = recent[i]['timestamp'] - recent[i-1]['timestamp']
                if dt > 0:
                    dsol = recent[i]['sol_raised'] - recent[i-1]['sol_raised']
                    velocities.append(dsol / dt)
            
            if not velocities:
                return 0.0
            
            # Simple EWMA
            ewma = velocities[0]
            for v in velocities[1:]:
                ewma = alpha * v + (1 - alpha) * ewma
            
            return ewma
        except Exception as e:
            logger.debug(f"EWMA calculation error: {e}")
            return 0.0
    
    # ✅ NEW: Helper function for higher highs check
    def _check_higher_highs(self, mint: str, window: float = 3.0) -> bool:
        """Check if MC is making higher highs in recent window"""
        try:
            history = self.velocity_checker.velocity_history.get(mint, [])
            if len(history) < 3:
                return False
            
            current_time = time.time()
            cutoff_time = current_time - window
            
            recent = [h for h in history if h['timestamp'] >= cutoff_time]
            if len(recent) < 3:
                return False
            
            # Check if each successive high is higher
            highs = [h['sol_raised'] for h in recent[-3:]]
            return highs[-1] > highs[-2] > highs[-3]
        except Exception as e:
            logger.debug(f"Higher highs check error: {e}")
            return False
    
    async def initialize_telegram(self):
        """Initialize Telegram bot after event loop is ready"""
        if self.telegram_enabled and not self.telegram:
            try:
                from telegram_bot import TelegramBot
                self.telegram = TelegramBot(self)
                self.telegram_polling_task = asyncio.create_task(self.telegram.start_polling())
                logger.info("✅ Telegram bot initialized")
                
                sol_balance = self.wallet.get_sol_balance()
                startup_msg = (
                    "🚀 Bot started - PROBE/CONFIRM STRATEGY\n"
                    f"💰 Balance: {sol_balance:.4f} SOL\n"
                    f"🎯 Entry: {PROBE_AMOUNT_SOL} probe + {CONFIRM_AMOUNT_SOL} confirm\n"
                    f"⚡ Health: {HEALTH_CHECK_START}-{HEALTH_CHECK_END}s\n"
                    f"⏱️ Timer: {TIMER_EXIT_BASE_SECONDS}→{TIMER_AUTO_EXTEND_TO}s\n"
                    "Type /help for commands"
                )
                await self.telegram.send_message(startup_msg)
            except Exception as e:
                logger.error(f"Failed to initialize Telegram: {e}")
                self.telegram = None
    
    async def stop_scanner(self):
        """Stop the scanner"""
        self.running = False
        self.shutdown_requested = True
        
        if self.scanner_task and not self.scanner_task.done():
            self.scanner_task.cancel()
            try:
                await self.scanner_task
            except asyncio.CancelledError:
                pass
            logger.info("Scanner task cancelled")
        
        if self.scanner:
            self.scanner.stop()
            logger.info("Scanner stopped")
        
        logger.info("✅ Bot stopped")
    
    async def start_scanner(self):
        """Start the scanner"""
        if self.shutdown_requested:
            self.shutdown_requested = False
            self.running = True
            self.paused = False
            logger.info("✅ Bot resuming from idle")
            return
        
        if self.running and self.scanner_task and not self.scanner_task.done():
            logger.info("Scanner already running")
            return
        
        self.running = True
        self.paused = False
        self.shutdown_requested = False
        self.consecutive_losses = 0
        
        if not self.scanner:
            self.scanner = PumpPortalMonitor(self.on_token_found)
        
        if self.scanner_task and not self.scanner_task.done():
            self.scanner_task.cancel()
            try:
                await self.scanner_task
            except asyncio.CancelledError:
                pass
            await asyncio.sleep(0.1)
        
        self.scanner_task = asyncio.create_task(self.scanner.start())
        logger.info("✅ Scanner started")
    
    async def restart_bot(self):
        """Restart the bot"""
        logger.info("Restarting bot...")
        await self.stop_scanner()
        await asyncio.sleep(1)
        self.shutdown_requested = False
        await self.start_scanner()
        logger.info("✅ Bot restarted")
    
    async def get_scanner_status(self) -> Dict:
        """Get scanner status"""
        return {
            'running': self.running,
            'paused': self.paused,
            'scanner_alive': self.scanner_task and not self.scanner_task.done() if self.scanner_task else False,
            'shutdown_requested': self.shutdown_requested,
            'positions': len(self.positions),
            'can_trade': self.wallet.can_trade(),
            'consecutive_losses': self.consecutive_losses,
            'session_losses': self.session_loss_count
        }
    
    async def on_token_found(self, token_data: Dict):
        """
        Handle new token found - PROBE/CONFIRM ENTRY STRATEGY
        ✅ NEW: Split entry with momentum validation
        """
        detection_start = time.time()
        
        try:
            mint = token_data['mint']
            
            self.dex.update_token_data(mint, token_data)
            
            if mint in self.positions:
                self.dex.update_token_data(mint, token_data)
                logger.debug(f"Updated price data for existing position {mint[:8]}...")
            
            if not self.running or self.paused:
                return
            
            if mint in BLACKLISTED_TOKENS:
                return
            
            total_positions = len(self.positions) + self.pending_buys
            
            if total_positions >= MAX_POSITIONS:
                logger.warning(f"Max positions reached ({len(self.positions)} active + {self.pending_buys} pending = {total_positions}/{MAX_POSITIONS})")
                return
            
            if mint in self.positions:
                return
            
            if not self.wallet.can_trade():
                current_time = time.time()
                if current_time - self._last_balance_warning > 60:
                    logger.warning(f"Insufficient balance for trading")
                    self._last_balance_warning = current_time
                return
            
            if self.consecutive_losses >= 3:
                logger.warning(f"🛑 Circuit breaker activated - 3 consecutive losses")
                self.paused = True
                if self.telegram:
                    await self.telegram.send_message(
                        "🛑 Circuit breaker activated\n"
                        "3 consecutive losses detected\n"
                        "Bot paused - use /resume to continue"
                    )
                return
            
            initial_buy = token_data.get('data', {}).get('solAmount', 0) if 'data' in token_data else token_data.get('solAmount', 0)
            name = token_data.get('data', {}).get('name', '') if 'data' in token_data else token_data.get('name', '')
            
            if initial_buy < 0.1 or initial_buy > 10:
                return
            
            if len(name) < 3:
                return
            
            # Liquidity validation
            passed, reason, curve_data = self.curve_reader.validate_liquidity(
                mint=mint,
                buy_size_sol=BUY_AMOUNT_SOL,
                min_multiplier=LIQUIDITY_MULTIPLIER,
                min_absolute_sol=MIN_LIQUIDITY_SOL
            )
            
            if not passed:
                logger.warning(f"❌ Liquidity check failed for {mint[:8]}...: {reason}")
                return
            
            logger.info(f"✅ Liquidity validated: {curve_data['sol_raised']:.4f} SOL raised")
            
            # Get token age (for velocity check later)
            token_age = None
            if 'data' in token_data and 'age' in token_data['data']:
                token_age = token_data['data']['age']
            elif 'age' in token_data:
                token_age = token_data['age']
            elif 'token_age' in token_data:
                token_age = token_data['token_age']
            
            if token_age is None or token_age == 0:
                sol_raised = curve_data.get('sol_raised', 0)
                if sol_raised > 0:
                    token_age = min(sol_raised / 1.0, VELOCITY_MAX_TOKEN_AGE)
                else:
                    token_age = VELOCITY_MAX_TOKEN_AGE / 2
            
            # ✅ SKIP early velocity check - we'll check during momentum phase
            # This allows probe to execute first, then we check velocity for confirm decision
            
            # Store estimated entry price
            estimated_entry_price = curve_data.get('price_lamports_per_atomic', 0)
            
            token_decimals = self.wallet.get_token_decimals(mint)
            if isinstance(token_decimals, tuple):
                token_decimals = token_decimals[0]
            if not token_decimals or token_decimals == 0:
                token_decimals = 6
            
            estimated_slippage = self.curve_reader.estimate_slippage(mint, BUY_AMOUNT_SOL)
            if estimated_slippage and estimated_slippage > MAX_SLIPPAGE_PERCENT:
                logger.warning(f"⚠️ High estimated slippage ({estimated_slippage:.2f}%), skipping")
                return
            
            self.pending_buys += 1
            entry_market_cap = token_data.get('market_cap', 0)
            
            detection_time_ms = (time.time() - detection_start) * 1000
            self.tracker.log_token_detection(mint, token_data.get('source', 'pumpportal'), detection_time_ms)
            
            logger.info(f"🎯 Processing new token: {mint}")
            logger.info(f"   Detection latency: {detection_time_ms:.0f}ms")
            logger.info(f"   Estimated entry MC: ${entry_market_cap:,.0f}")
            
            # ==============================================
            # ✅ PHASE 1: PROBE ENTRY (Fast, No Helius)
            # ==============================================
            if ENABLE_PROBE_ENTRY:
                probe_sol = PROBE_AMOUNT_SOL
                
                logger.info(f"🔍 PROBE ENTRY: {probe_sol:.4f} SOL (no Helius check)")
                
                execution_start = time.time()
                bonding_curve_key = None
                if 'data' in token_data and 'bondingCurveKey' in token_data['data']:
                    bonding_curve_key = token_data['data']['bondingCurveKey']
                
                probe_signature = await self.trader.create_buy_transaction(
                    mint=mint,
                    sol_amount=probe_sol,
                    bonding_curve_key=bonding_curve_key,
                    slippage=30,
                    urgency="normal"
                )
                
                if not probe_signature:
                    logger.error("❌ Probe transaction failed")
                    self.pending_buys -= 1
                    return
                
                # Wait for probe settlement
                await asyncio.sleep(2.5)
                
                probe_tokens = self.wallet.get_token_balance(mint)
                if probe_tokens == 0:
                    logger.error("❌ Probe filled 0 tokens - aborting")
                    self.pending_buys -= 1
                    return
                
                # Calculate probe entry price
                token_atoms = int(probe_tokens * (10 ** token_decimals))
                lamports_spent = int(probe_sol * 1e9)
                probe_entry_price = lamports_spent / token_atoms
                
                logger.info(f"✅ PROBE FILLED: {probe_tokens:,.0f} tokens at {probe_entry_price:.10f} lamports/atomic")
                
                # ==============================================
                # ✅ PHASE 2: MOMENTUM CHECK (1.2s delay)
                # ==============================================
                await asyncio.sleep(CONFIRM_DELAY_SECONDS)
                
                logger.info(f"📊 MOMENTUM CHECK (after {CONFIRM_DELAY_SECONDS}s)...")
                
                # Get current velocity
                current_velocity = self.velocity_checker.get_current_velocity(mint)
                pre_buy_velocity = self.velocity_checker.get_pre_buy_velocity(mint)
                velocity_ratio = current_velocity / pre_buy_velocity if pre_buy_velocity > 0 else 0
                
                # Estimate buyer growth from SOL increase
                current_curve = self.curve_reader.get_curve_state(mint, use_cache=False)
                current_sol = current_curve.get('sol_raised', 0) if current_curve else 0
                buyer_delta = int((current_sol - curve_data.get('sol_raised', 0)) / 0.4) if current_sol > 0 else 0
                
                # ✅ Also check absolute velocity (not just ratio)
                # This catches tokens that had low velocity from the start
                velocity_check_passed, velocity_reason = self.velocity_checker.check_velocity(
                    mint=mint,
                    curve_data=current_curve if current_curve else curve_data,
                    token_age_seconds=token_age + CONFIRM_DELAY_SECONDS
                )
                
                momentum_passed = (
                    velocity_ratio >= CONFIRM_MIN_VELOCITY_RATIO and
                    buyer_delta >= CONFIRM_MIN_BUYER_DELTA and
                    velocity_check_passed  # Must also pass absolute velocity check
                )
                
                if not momentum_passed:
                    logger.warning(f"⚠️ MOMENTUM CHECK FAILED - managing probe only")
                    logger.info(f"   Velocity ratio: {velocity_ratio:.2f} (need {CONFIRM_MIN_VELOCITY_RATIO})")
                    logger.info(f"   Buyer delta: {buyer_delta} (need {CONFIRM_MIN_BUYER_DELTA})")
                    if not velocity_check_passed:
                        logger.info(f"   Velocity check: {velocity_reason}")
                    
                    # Create position with probe only
                    position = Position(mint, probe_sol, probe_tokens, entry_market_cap)
                    position.probe_completed = True
                    position.probe_tokens = probe_tokens
                    position.probe_sol = probe_sol
                    position.entry_token_price_sol = probe_entry_price
                    position.buy_signature = probe_signature
                    
                    variance = random.uniform(-TIMER_EXIT_VARIANCE_SECONDS, TIMER_EXIT_VARIANCE_SECONDS)
                    position.exit_time = position.entry_time + TIMER_EXIT_BASE_SECONDS + variance
                    position.tp_cooldown_until = time.time() + TP_COOLDOWN_SECONDS
                    
                    self.positions[mint] = position
                    self.total_trades += 1
                    self.pending_buys -= 1
                    
                    logger.info(f"📊 Managing probe-only position: {probe_tokens:,.0f} tokens")
                    
                    position.monitor_task = asyncio.create_task(self._monitor_position(mint))
                    return
                
                logger.info(f"✅ MOMENTUM PASSED - checking holders...")
                
                # ==============================================
                # ✅ PHASE 3: HELIUS HOLDER CHECK
                # ==============================================
                # Use existing Helius check from pumpportal_monitor
                holder_result = {'passed': True, 'holder_count': 10}  # Default pass
                
                try:
                    if self.scanner and hasattr(self.scanner, '_check_holders_helius'):
                        holder_result = await self.scanner._check_holders_helius(mint)
                except Exception as e:
                    logger.warning(f"Helius check failed: {e}, assuming passed")
                
                if not holder_result.get('passed', True):
                    logger.warning(f"⚠️ HOLDER CHECK FAILED - managing probe only")
                    logger.info(f"   Reason: {holder_result.get('reason', 'unknown')}")
                    
                    # Create position with probe only
                    position = Position(mint, probe_sol, probe_tokens, entry_market_cap)
                    position.probe_completed = True
                    position.probe_tokens = probe_tokens
                    position.probe_sol = probe_sol
                    position.entry_token_price_sol = probe_entry_price
                    position.buy_signature = probe_signature
                    
                    variance = random.uniform(-TIMER_EXIT_VARIANCE_SECONDS, TIMER_EXIT_VARIANCE_SECONDS)
                    position.exit_time = position.entry_time + TIMER_EXIT_BASE_SECONDS + variance
                    position.tp_cooldown_until = time.time() + TP_COOLDOWN_SECONDS
                    
                    self.positions[mint] = position
                    self.total_trades += 1
                    self.pending_buys -= 1
                    
                    logger.info(f"📊 Managing probe-only position: {probe_tokens:,.0f} tokens")
                    
                    position.monitor_task = asyncio.create_task(self._monitor_position(mint))
                    return
                
                # ==============================================
                # ✅ PHASE 4: CONFIRM ENTRY (60% remaining)
                # ==============================================
                confirm_sol = CONFIRM_AMOUNT_SOL
                
                logger.info(f"✅ HOLDER CHECK PASSED - confirming with {confirm_sol:.4f} SOL")
                
                confirm_signature = await self.trader.create_buy_transaction(
                    mint=mint,
                    sol_amount=confirm_sol,
                    bonding_curve_key=bonding_curve_key,
                    slippage=30,
                    urgency="normal"
                )
                
                if not confirm_signature:
                    logger.warning("⚠️ Confirm failed - managing probe only")
                    
                    # Create position with probe only
                    position = Position(mint, probe_sol, probe_tokens, entry_market_cap)
                    position.probe_completed = True
                    position.probe_tokens = probe_tokens
                    position.probe_sol = probe_sol
                    position.entry_token_price_sol = probe_entry_price
                    position.buy_signature = probe_signature
                    
                    variance = random.uniform(-TIMER_EXIT_VARIANCE_SECONDS, TIMER_EXIT_VARIANCE_SECONDS)
                    position.exit_time = position.entry_time + TIMER_EXIT_BASE_SECONDS + variance
                    position.tp_cooldown_until = time.time() + TP_COOLDOWN_SECONDS
                    
                    self.positions[mint] = position
                    self.total_trades += 1
                    self.pending_buys -= 1
                    
                    position.monitor_task = asyncio.create_task(self._monitor_position(mint))
                    return
                
                await asyncio.sleep(2.5)
                
                # Get total tokens after confirm
                total_tokens = self.wallet.get_token_balance(mint)
                confirm_tokens = total_tokens - probe_tokens
                
                # Calculate weighted average entry price
                total_sol = probe_sol + confirm_sol
                total_lamports = int(total_sol * 1e9)
                total_atoms = int(total_tokens * (10 ** token_decimals))
                avg_entry_price = total_lamports / total_atoms
                
                logger.info(f"✅ FULL POSITION: {total_tokens:,.0f} tokens at {avg_entry_price:.10f} lamports/atomic avg")
                logger.info(f"   Probe: {probe_tokens:,.0f} @ {probe_entry_price:.10f}")
                logger.info(f"   Confirm: {confirm_tokens:,.0f}")
                
                # Create position with full info
                position = Position(mint, total_sol, total_tokens, entry_market_cap)
                position.probe_completed = True
                position.confirm_completed = True
                position.probe_tokens = probe_tokens
                position.probe_sol = probe_sol
                position.entry_token_price_sol = avg_entry_price
                position.buy_signature = f"{probe_signature},{confirm_signature}"
                
                variance = random.uniform(-TIMER_EXIT_VARIANCE_SECONDS, TIMER_EXIT_VARIANCE_SECONDS)
                position.exit_time = position.entry_time + TIMER_EXIT_BASE_SECONDS + variance
                position.tp_cooldown_until = time.time() + TP_COOLDOWN_SECONDS
                
                if 'data' in token_data:
                    position.entry_sol_in_curve = token_data['data'].get('vSolInBondingCurve', 30)
                
                self.positions[mint] = position
                self.total_trades += 1
                self.pending_buys -= 1
                
                exit_in_seconds = position.exit_time - position.entry_time
                
                logger.info(f"✅ BUY EXECUTED: {mint[:8]}...")
                logger.info(f"   Strategy: Probe ({probe_sol:.4f}) + Confirm ({confirm_sol:.4f}) = {total_sol:.4f} SOL")
                logger.info(f"   Tokens: {total_tokens:,.0f}")
                logger.info(f"   Avg Entry Price: {avg_entry_price:.10f} lamports/atomic")
                logger.info(f"   ⏱️ Exit timer: {exit_in_seconds:.1f}s")
                logger.info(f"   Active positions: {len(self.positions)}/{MAX_POSITIONS}")
                
                if self.telegram:
                    await self.telegram.notify_buy(mint, total_sol, position.buy_signature)
                
                # Seed post-buy chain price
                await asyncio.sleep(0.8)
                seed = self.dex.get_bonding_curve_data(mint, prefer_chain=True)
                if seed and seed.get('source') == 'chain':
                    logger.info("🔎 Seeded post-buy price from [chain]")
                
                position.monitor_task = asyncio.create_task(self._monitor_position(mint))
                
            else:
                # ==============================================
                # FALLBACK: Single entry (if probe disabled)
                # ==============================================
                logger.info(f"💰 Single entry: {BUY_AMOUNT_SOL} SOL")
                
                execution_start = time.time()
                bonding_curve_key = None
                if 'data' in token_data and 'bondingCurveKey' in token_data['data']:
                    bonding_curve_key = token_data['data']['bondingCurveKey']
                
                signature = await self.trader.create_buy_transaction(
                    mint=mint,
                    sol_amount=BUY_AMOUNT_SOL,
                    bonding_curve_key=bonding_curve_key,
                    slippage=30,
                    urgency="normal"
                )
                
                if not signature:
                    self.pending_buys -= 1
                    return
                
                await asyncio.sleep(2.5)
                
                bought_tokens = self.wallet.get_token_balance(mint)
                if bought_tokens == 0:
                    self.pending_buys -= 1
                    return
                
                # Calculate entry price
                token_atoms = int(bought_tokens * (10 ** token_decimals))
                lamports_spent = int(BUY_AMOUNT_SOL * 1e9)
                actual_entry_price = lamports_spent / token_atoms
                
                position = Position(mint, BUY_AMOUNT_SOL, bought_tokens, entry_market_cap)
                position.entry_token_price_sol = actual_entry_price
                position.buy_signature = signature
                
                variance = random.uniform(-TIMER_EXIT_VARIANCE_SECONDS, TIMER_EXIT_VARIANCE_SECONDS)
                position.exit_time = position.entry_time + TIMER_EXIT_BASE_SECONDS + variance
                position.tp_cooldown_until = time.time() + TP_COOLDOWN_SECONDS
                
                self.positions[mint] = position
                self.total_trades += 1
                self.pending_buys -= 1
                
                logger.info(f"✅ BUY EXECUTED: {bought_tokens:,.0f} tokens")
                
                position.monitor_task = asyncio.create_task(self._monitor_position(mint))
                
        except Exception as e:
            self.pending_buys = max(0, self.pending_buys - 1)
            logger.error(f"Failed to process token: {e}")
            import traceback
            logger.error(traceback.format_exc())
    
    async def _monitor_position(self, mint: str):
        """
        Monitor position - ALL 5 EXIT LAYERS
        ✅ NEW: Health check, dynamic TPs, trailing stop, auto-extend timer
        """
        try:
            position = self.positions.get(mint)
            if not position:
                return
            
            entry_mc = position.entry_market_cap
            
            # ✅ Determine TP levels based on entry MC
            if entry_mc <= 12000:
                tp_levels = TP_LEVELS_EARLY
                logger.info(f"📈 Using EARLY TP levels (entry MC ${entry_mc:,.0f}): {tp_levels}")
            elif entry_mc <= 22000:
                tp_levels = TP_LEVELS_MID
                logger.info(f"📈 Using MID TP levels (entry MC ${entry_mc:,.0f}): {tp_levels}")
            else:
                tp_levels = TP_LEVELS_LATE
                logger.info(f"📈 Using LATE TP levels (entry MC ${entry_mc:,.0f}): {tp_levels}")
            
            logger.info(f"📊 TP sell sizes: {TP_SELL_PERCENTS}%")
            logger.info(f"📈 Starting monitoring for {mint[:8]}...")
            logger.info(f"   Entry Price: {position.entry_token_price_sol:.10f} lamports/atomic")
            logger.info(f"   Exit Time: {position.exit_time - position.entry_time:.1f}s from now")
            logger.info(f"   Your Tokens: {position.remaining_tokens:,.0f}")
            
            check_count = 0
            consecutive_data_failures = 0
            
            while mint in self.positions and position.status == 'active':
                check_count += 1
                current_time = time.time()
                age = current_time - position.entry_time
                time_until_exit = position.exit_time - current_time
                
                if age > MAX_POSITION_AGE_SECONDS:
                    logger.warning(f"⏰ MAX AGE REACHED for {mint[:8]}... ({age:.0f}s)")
                    await self._close_position_full(mint, reason="max_age")
                    break
                
                try:
                    prefer_chain = not position.has_chain_price
                    curve_data = self.dex.get_bonding_curve_data(mint, prefer_chain=prefer_chain)
                    
                    if not curve_data:
                        consecutive_data_failures += 1
                        logger.warning(f"No price data for {mint[:8]}... (failure {consecutive_data_failures}/{DATA_FAILURE_TOLERANCE})")
                        
                        if consecutive_data_failures > DATA_FAILURE_TOLERANCE:
                            logger.error(f"❌ Too many data failures for {mint[:8]}...")
                            if position.last_valid_price > 0:
                                logger.debug(f"Using last valid price: {position.last_valid_price:.10f}")
                            else:
                                await asyncio.sleep(1)
                                continue
                        else:
                            await asyncio.sleep(1)
                            continue
                    
                    source = curve_data.get('source', 'unknown')
                    position.last_price_source = source
                    if source == 'chain':
                        position.has_chain_price = True
                    
                    if curve_data.get('is_migrated'):
                        logger.warning(f"❌ Token {mint[:8]}... has migrated - exiting immediately")
                        await self._close_position_full(mint, reason="migration")
                        break
                    
                    current_sol_in_curve = curve_data.get('sol_in_curve', 0)
                    if current_sol_in_curve <= 0:
                        consecutive_data_failures += 1
                        await asyncio.sleep(1)
                        continue
                    
                    current_mc = self._calculate_mc_from_curve(curve_data)
                    current_token_price_sol = self._get_current_token_price(mint, curve_data)
                    
                    if current_token_price_sol is None or current_token_price_sol <= 0:
                        consecutive_data_failures += 1
                        await asyncio.sleep(1)
                        continue
                    
                    if position.entry_token_price_sol > 0:
                        price_change = ((current_token_price_sol / position.entry_token_price_sol) - 1) * 100
                    else:
                        price_change = 0
                    
                    position.pnl_percent = price_change
                    position.current_price = current_token_price_sol
                    position.current_market_cap = current_mc
                    position.max_pnl_reached = max(position.max_pnl_reached, price_change)
                    
                    # Update peak MC for trailing stop
                    if position.first_tp_taken:
                        position.peak_mc_since_first_tp = max(position.peak_mc_since_first_tp, current_mc)
                    
                    consecutive_data_failures = 0
                    position.last_valid_price = current_token_price_sol
                    position.last_price_update = time.time()
                    
                    self.velocity_checker.update_snapshot(
                        mint, 
                        current_sol_in_curve, 
                        int(current_sol_in_curve / 0.4)
                    )
                    
                    on_chain = (source == 'chain') and position.has_chain_price
                    
                    # ========================================
                    # EXIT LAYER 0: EXTREME TP (Existing)
                    # ========================================
                    if on_chain and not position.is_closing:
                        if price_change >= EXTREME_TP_PERCENT:
                            logger.info(
                                f"💰 EXTREME TAKE PROFIT on [chain]: {price_change:+.1f}% "
                                f"(threshold: {EXTREME_TP_PERCENT}%)"
                            )
                            await self._close_position_full(mint, reason="extreme_take_profit")
                            break
                    
                    # ========================================
                    # EXIT LAYER 0.5: TRAIL START (Existing)
                    # ========================================
                    if on_chain and not position.is_closing:
                        if position.max_pnl_reached >= TRAIL_START_PERCENT:
                            drop_from_peak = position.max_pnl_reached - price_change
                            if drop_from_peak >= TRAIL_GIVEBACK_PERCENT:
                                logger.warning(
                                    f"📉 TRAILING STOP (old) on [chain]: drop {drop_from_peak:.1f}pp "
                                    f"from +{position.max_pnl_reached:.1f}% peak"
                                )
                                await self._close_position_full(mint, reason="trailing_stop_old")
                                break
                    
                    # ========================================
                    # EXIT LAYER 1: FAIL-FAST (5s)
                    # ========================================
                    if (age >= FAIL_FAST_CHECK_TIME and 
                        not position.fail_fast_checked and 
                        not position.is_closing):

                        if not position.has_chain_price or source != 'chain':
                            logger.warning(
                                f"🚧 FAIL-FAST from [{source}] ignored until first [chain] tick"
                            )
                        else:
                            position.fail_fast_checked = True

                            if price_change < FAIL_FAST_PNL_THRESHOLD:
                                logger.warning(
                                    f"⚠️ FAIL-FAST: P&L {price_change:.1f}% < {FAIL_FAST_PNL_THRESHOLD}% at {age:.1f}s "
                                    f"(on [chain]) - exiting immediately"
                                )
                                await self._close_position_full(mint, reason="fail_fast_pnl")
                                break

                            pre_buy_velocity = self.velocity_checker.get_pre_buy_velocity(mint)
                            if pre_buy_velocity:
                                current_velocity = current_sol_in_curve / max(age, 0.1)
                                velocity_percent = (current_velocity / pre_buy_velocity) * 100

                                if velocity_percent < FAIL_FAST_VELOCITY_THRESHOLD:
                                    logger.warning(
                                        f"⚠️ FAIL-FAST: Velocity died ({velocity_percent:.1f}% of pre-buy) at {age:.1f}s - "
                                        f"exiting immediately"
                                    )
                                    await self._close_position_full(mint, reason="fail_fast_velocity")
                                    break
                                else:
                                    logger.info(
                                        f"✅ FAIL-FAST CHECK PASSED at {age:.1f}s: "
                                        f"P&L {price_change:+.1f}%, velocity {velocity_percent:.0f}%"
                                    )
                    
                    # ========================================
                    # EXIT LAYER 2: HEALTH CHECK (8-12s) ✅ NEW
                    # ========================================
                    if (HEALTH_CHECK_START <= age < HEALTH_CHECK_END and 
                        not position.health_checked and 
                        not position.is_closing):
                        
                        if not position.has_chain_price or source != 'chain':
                            logger.warning(
                                f"🚧 HEALTH CHECK from [{source}] ignored until first [chain] tick"
                            )
                        else:
                            position.health_checked = True
                            
                            # Calculate velocity EWMA
                            velocity_ewma = self._calculate_velocity_ewma(mint, alpha=0.5, window=3.0)
                            pre_buy_velocity = self.velocity_checker.get_pre_buy_velocity(mint)
                            velocity_ratio = velocity_ewma / pre_buy_velocity if pre_buy_velocity > 0 else 0
                            
                            mc_ratio = current_mc / entry_mc if entry_mc > 0 else 1.0
                            
                            if velocity_ratio < HEALTH_CHECK_VELOCITY_THRESHOLD or mc_ratio < HEALTH_CHECK_MC_THRESHOLD:
                                logger.warning(
                                    f"⚠️ HEALTH CHECK FAILED at {age:.1f}s: "
                                    f"Velocity {velocity_ratio:.0%} | MC ratio {mc_ratio:.0%}"
                                )
                                await self._close_position_full(mint, reason="health_check_failed")
                                break
                            else:
                                logger.info(
                                    f"✅ HEALTH CHECK PASSED at {age:.1f}s: "
                                    f"Velocity {velocity_ratio:.0%}, MC ratio {mc_ratio:.0%}"
                                )
                    
                    # ========================================
                    # EXIT LAYER 3: SCALED TPS (MC-based) ✅ NEW
                    # ========================================
                    if ENABLE_DYNAMIC_TPS and age > position.tp_cooldown_until:
                        for i, (tp_mult, tp_size) in enumerate(zip(tp_levels, TP_SELL_PERCENTS)):
                            tp_name = f"TP_{tp_mult}x"
                            
                            if current_mc >= entry_mc * tp_mult and tp_name not in position.partial_sells:
                                logger.info(f"💰 {tp_name} HIT: ${current_mc:,.0f} >= ${entry_mc * tp_mult:,.0f}")
                                await self._execute_partial_sell(mint, tp_size, tp_name, price_change)
                                
                                if i == 0:
                                    position.first_tp_taken = True
                                    position.peak_mc_since_first_tp = current_mc
                                    logger.info(f"🎯 First TP taken - trailing stop now active")
                    
                    # ========================================
                    # EXIT LAYER 4: TRAILING STOP ✅ NEW
                    # ========================================
                    if (ENABLE_TRAILING_STOP and 
                        position.first_tp_taken and 
                        position.peak_mc_since_first_tp > 0 and
                        not position.is_closing):
                        
                        mc_drawdown_pct = ((position.peak_mc_since_first_tp - current_mc) / 
                                           position.peak_mc_since_first_tp * 100)
                        
                        if mc_drawdown_pct >= TRAILING_STOP_DRAWDOWN:
                            logger.warning(
                                f"📉 TRAILING STOP: -{mc_drawdown_pct:.1f}% from peak "
                                f"(${position.peak_mc_since_first_tp:,.0f} → ${current_mc:,.0f})"
                            )
                            await self._close_position_full(mint, reason="trailing_stop")
                            break
                    
                    # ========================================
                    # EXIT LAYER 5: TIMER ✅ UPDATED
                    # ========================================
                    # Auto-extend logic
                    if 24.5 <= age < 25.5 and time_until_exit > 0:
                        velocity_ewma = self._calculate_velocity_ewma(mint)
                        pre_buy = self.velocity_checker.get_pre_buy_velocity(mint)
                        is_rising = velocity_ewma > pre_buy * 1.2 if pre_buy > 0 else False
                        is_making_hh = self._check_higher_highs(mint, window=3.0)
                        
                        if is_rising and is_making_hh:
                            position.exit_time = position.entry_time + TIMER_AUTO_EXTEND_TO
                            logger.info(f"🚀 Auto-extending to {TIMER_AUTO_EXTEND_TO}s (strong momentum)")
                    
                    if time_until_exit <= 0 and not position.is_closing:
                        logger.info(f"⏰ TIMER EXIT at {age:.1f}s")
                        logger.info(f"   Final P&L: {price_change:+.1f}%")
                        logger.info(f"   Max P&L reached: {position.max_pnl_reached:+.1f}%")
                        await self._close_position_full(mint, reason="timer_exit")
                        break
                    
                    # Periodic logging
                    if check_count % 3 == 1:
                        logger.info(
                            f"⏱️ {mint[:8]}... | P&L: {price_change:+.1f}% | "
                            f"MC: ${current_mc:,.0f} | Exit: {time_until_exit:.0f}s"
                        )
                    
                    # ========================================
                    # RUG TRAP (Existing, requires chain)
                    # ========================================
                    rug_threshold = -60 if age < 3.0 else -40
                    if price_change <= rug_threshold and not position.is_closing:
                        if not position.has_chain_price or source != 'chain':
                            logger.warning(f"🚧 RUG signal from [{source}] ignored until first [chain] tick")
                        else:
                            logger.warning(
                                f"🚨 RUG TRAP TRIGGERED ({price_change:.1f}%) on [chain] "
                                f"(age {age:.1f}s, threshold {rug_threshold}%)"
                            )
                            await self._close_position_full(mint, reason="rug_trap")
                            break
                    
                    # STOP LOSS (Existing, requires chain)
                    if price_change <= -STOP_LOSS_PERCENTAGE and not position.is_closing:
                        if not position.has_chain_price or source != 'chain':
                            logger.warning(f"🚧 STOP LOSS signal from [{source}] ignored until first [chain] tick")
                        else:
                            logger.warning(f"🛑 STOP LOSS HIT for {mint[:8]}... (on [chain] source)")
                            logger.warning(f"   P&L: {price_change:.1f}% <= -{STOP_LOSS_PERCENTAGE}%")
                            await self._close_position_full(mint, reason="stop_loss")
                            break
                
                except Exception as e:
                    logger.error(f"Error checking {mint[:8]}...: {e}")
                    import traceback
                    logger.error(traceback.format_exc())
                
                await asyncio.sleep(MONITOR_CHECK_INTERVAL)
            
            if mint in self.positions and position.status == 'completed':
                del self.positions[mint]
                logger.info(f"Position {mint[:8]}... removed after completion")
                self.velocity_checker.clear_history(mint)
                
        except Exception as e:
            logger.error(f"Monitor error for {mint[:8]}...: {e}")
            if mint in self.positions:
                await self._close_position_full(mint, reason="monitor_error")
    
    async def _execute_partial_sell(self, mint: str, sell_percent: float, target_name: str, current_pnl: float) -> bool:
        """Execute partial sell (existing logic preserved)"""
        try:
            position = self.positions.get(mint)
            if not position:
                return False
            
            from decimal import Decimal, ROUND_DOWN
            token_decimals = self.wallet.get_token_decimals(mint)
            raw = Decimal(str(position.remaining_tokens)) * Decimal(str(sell_percent)) / Decimal("100")
            units = (raw * (Decimal(10) ** token_decimals)).quantize(Decimal("1"), rounding=ROUND_DOWN)
            ui_tokens_to_sell = float(units / (Decimal(10) ** token_decimals))
            
            if ui_tokens_to_sell <= 0:
                logger.warning(f"{target_name}: 0 tokens after flooring, skipping")
                return False
            
            logger.info(f"💰 Executing {target_name} partial sell for {mint[:8]}...")
            logger.info(f"   Selling: {sell_percent}% ({ui_tokens_to_sell:,.2f} tokens)")
            logger.info(f"   P&L: {current_pnl:+.1f}%")
            
            pre_sol_balance = self.wallet.get_sol_balance()
            pre_token_balance = self.wallet.get_token_balance(mint)
            
            signature = await self.trader.create_sell_transaction(
                mint=mint,
                token_amount=ui_tokens_to_sell,
                slippage=50,
                token_decimals=token_decimals,
                urgency="high"
            )
            
            if signature and not signature.startswith("1111111"):
                asyncio.create_task(
                    self._confirm_sell_background(
                        signature, mint, target_name, sell_percent,
                        ui_tokens_to_sell, current_pnl,
                        pre_sol_balance, pre_token_balance
                    )
                )
                
                logger.info(f"✅ {target_name} sell submitted, confirming in background...")
                return True
            else:
                logger.error(f"Failed to submit {target_name} sell")
                return False
                
        except Exception as e:
            logger.error(f"Partial sell error: {e}")
            return False
    
    async def _confirm_sell_background(
        self, signature: str, mint: str, target_name: str,
        sell_percent: float, tokens_sold: float, current_pnl: float,
        pre_sol_balance: float, pre_token_balance: float
    ):
        """Confirm partial sell in background (existing logic preserved)"""
        try:
            position = self.positions.get(mint)
            if not position:
                logger.warning(f"Position {mint[:8]} disappeared during confirmation")
                return
            
            logger.info(f"⏳ Confirming {target_name} sell for {mint[:8]}...")
            logger.info(f"🔗 Solscan: https://solscan.io/tx/{signature}")
            
            first_seen = None
            start = time.time()
            confirmed = False
            
            while time.time() - start < 25:
                try:
                    from solders.signature import Signature as SoldersSignature
                    status = self.trader.client.get_signature_statuses([SoldersSignature.from_string(signature)])
                    if status and status.value and status.value[0]:
                        if first_seen is None:
                            first_seen = time.time() - start
                        
                        confirmation_status = status.value[0].confirmation_status
                        if confirmation_status in ["confirmed", "finalized"]:
                            if status.value[0].err:
                                logger.error(f"❌ {target_name} sell FAILED: {status.value[0].err}")
                                break
                            else:
                                confirmed = True
                                break
                except Exception as e:
                    logger.debug(f"Status check error: {e}")
                
                await asyncio.sleep(1)
            
            if confirmed:
                txd = await self._get_transaction_deltas(signature, mint)
                if txd["confirmed"]:
                    actual_sol_received = txd["sol_delta"] if txd["sol_delta"] > 0 else None
                    actual_tokens_sold = abs(txd["token_delta"]) if txd["token_delta"] < 0 else None
                else:
                    actual_sol_received, actual_tokens_sold = None, None

                if actual_sol_received is None:
                    await asyncio.sleep(2)
                    post_sol_balance = self.wallet.get_sol_balance()
                    actual_sol_received = post_sol_balance - pre_sol_balance

                if actual_tokens_sold is None:
                    await asyncio.sleep(2)
                    current_token_balance = self.wallet.get_token_balance(mint)
                    balance_decrease = pre_token_balance - current_token_balance
                    actual_tokens_sold = max(0.0, balance_decrease)
                    position.remaining_tokens = max(0.0, current_token_balance)
                else:
                    position.remaining_tokens = max(0.0, position.remaining_tokens - actual_tokens_sold)
                
                base_sol_for_portion = position.amount_sol * (sell_percent / 100)
                actual_profit_sol = actual_sol_received - base_sol_for_portion
                
                position.sell_signatures.append(signature)
                position.realized_pnl_sol += actual_profit_sol
                self.total_realized_sol += actual_profit_sol
                
                position.partial_sells[target_name] = {
                    'pnl': current_pnl,
                    'time': time.time(),
                    'percent_sold': sell_percent
                }
                position.total_sold_percent += sell_percent
                
                if target_name in position.pending_sells:
                    position.pending_sells.remove(target_name)
                if target_name in position.pending_token_amounts:
                    del position.pending_token_amounts[target_name]
                
                self.consecutive_losses = 0
                if target_name in position.retry_counts:
                    del position.retry_counts[target_name]
                
                self.tracker.log_partial_sell(
                    mint=mint,
                    target_name=target_name,
                    percent_sold=sell_percent,
                    tokens_sold=actual_tokens_sold,
                    sol_received=actual_sol_received,
                    pnl_sol=actual_profit_sol
                )
                
                logger.info(f"✅ {target_name} CONFIRMED for {mint[:8]}")
                logger.info(f"   Received: {actual_sol_received:.4f} SOL")
                logger.info(f"   Profit: {actual_profit_sol:+.4f} SOL")
                
                if position.total_sold_percent >= 100:
                    logger.info(f"✅ Position fully closed")
                    position.status = 'completed'
                    
        except Exception as e:
            logger.error(f"Confirmation error for {mint[:8]}: {e}")
    
    async def _close_position_full(self, mint: str, reason: str = "manual"):
        """Close remaining position (existing logic preserved)"""
        try:
            position = self.positions.get(mint)
            if not position or position.status != 'active':
                return
            
            if position.is_closing:
                logger.debug(f"Position {mint[:8]} already closing")
                return
            
            position.is_closing = True
            position.status = 'closing'
            ui_token_balance = position.remaining_tokens
            
            if ui_token_balance <= 0:
                logger.warning(f"No tokens remaining for {mint[:8]}...")
                position.status = 'closed'
                if mint in self.positions:
                    del self.positions[mint]
                    self.velocity_checker.clear_history(mint)
                return
            
            hold_time = time.time() - position.entry_time
            
            logger.info(f"📤 Closing position {mint[:8]}...")
            logger.info(f"   Reason: {reason}")
            logger.info(f"   Hold time: {hold_time:.1f}s")
            logger.info(f"   P&L: {position.pnl_percent:+.1f}%")
            logger.info(f"   Max P&L reached: {position.max_pnl_reached:+.1f}%")
            
            pre_sol_balance = self.wallet.get_sol_balance()
            
            actual_balance = self.wallet.get_token_balance(mint)
            if actual_balance > 0:
                ui_token_balance = actual_balance
                logger.info(f"💰 Selling actual balance: {actual_balance:,.2f} tokens")
            
            curve_data = self.dex.get_bonding_curve_data(mint)
            is_migrated = curve_data is None or curve_data.get('is_migrated', False)
            
            token_decimals = self.wallet.get_token_decimals(mint)
            
            signature = await self.trader.create_sell_transaction(
                mint=mint,
                token_amount=ui_token_balance,
                slippage=100 if is_migrated else 50,
                token_decimals=token_decimals,
                urgency="critical"
            )
            
            if not signature or signature.startswith("1111111"):
                logger.error(f"❌ Close transaction failed")
                position.status = 'close_failed'
                
                if mint in self.positions:
                    del self.positions[mint]
                    self.velocity_checker.clear_history(mint)
                return
            
            # Robust confirmation (existing logic)
            logger.info(f"⏳ Confirming full close for {mint[:8]}...")
            logger.info(f"🔗 Solscan: https://solscan.io/tx/{signature}")
            
            first_seen = None
            start = time.time()
            confirmed = False
            
            try:
                while time.time() - start < 25:
                    try:
                        from solders.signature import Signature as SoldersSignature
                        sig_list = [SoldersSignature.from_string(signature)]
                        status = self.trader.client.get_signature_statuses(sig_list)
                        
                        if status and status.value and status.value[0]:
                            if first_seen is None:
                                first_seen = time.time() - start
                            
                            status_obj = status.value[0]
                            confirmation_status = status_obj.confirmation_status
                            
                            if confirmation_status and str(confirmation_status) in ["confirmed", "finalized"]:
                                if status_obj.err:
                                    logger.error(f"❌ Sell FAILED on-chain: {status_obj.err}")
                                    break
                                else:
                                    confirmed = True
                                    break
                    except Exception as e:
                        logger.debug(f"Status check error: {e}")
                    
                    await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Confirmation loop crashed: {e}")
            
            # Get actual results
            actual_sol_received = 0
            
            if confirmed:
                txd = await self._get_transaction_deltas(signature, mint)
                if txd["confirmed"]:
                    actual_sol_received = txd["sol_delta"] if txd["sol_delta"] > 0 else 0
            
            if not confirmed or actual_sol_received == 0:
                await asyncio.sleep(2)
                post_sol_balance = self.wallet.get_sol_balance()
                actual_sol_received = post_sol_balance - pre_sol_balance
            
            final_pnl_sol = actual_sol_received - position.amount_sol
            
            position.sell_signatures.append(signature)
            position.status = 'closed'
            
            if final_pnl_sol > 0:
                self.profitable_trades += 1
                self.consecutive_losses = 0
            else:
                self.consecutive_losses += 1
                self.session_loss_count += 1
            
            position.realized_pnl_sol = final_pnl_sol
            self.total_realized_sol += final_pnl_sol
            
            self.tracker.log_sell_executed(
                mint=mint,
                tokens_sold=ui_token_balance,
                signature=signature,
                sol_received=actual_sol_received,
                pnl_sol=final_pnl_sol,
                pnl_percent=position.pnl_percent,
                hold_time_seconds=hold_time,
                reason=reason
            )
            
            logger.info(f"✅ POSITION CLOSED: {mint[:8]}...")
            logger.info(f"   Reason: {reason}")
            logger.info(f"   Hold time: {hold_time:.1f}s")
            logger.info(f"   P&L: {position.pnl_percent:+.1f}%")
            logger.info(f"   Realized: {final_pnl_sol:+.4f} SOL")
            
            if self.telegram:
                emoji = "💰" if final_pnl_sol > 0 else "🔴"
                msg = (
                    f"{emoji} POSITION CLOSED\n"
                    f"Token: {mint[:16]}\n"
                    f"Reason: {reason}\n"
                    f"Hold: {hold_time:.1f}s\n"
                    f"P&L: {position.pnl_percent:+.1f}%\n"
                    f"Realized: {final_pnl_sol:+.4f} SOL"
                )
                if self.consecutive_losses >= 2:
                    msg += f"\n⚠️ Losses: {self.consecutive_losses}/3"
                await self.telegram.send_message(msg)
            
            if mint in self.positions:
                del self.positions[mint]
                self.velocity_checker.clear_history(mint)
                logger.info(f"Active: {len(self.positions)}/{MAX_POSITIONS}")
            
        except Exception as e:
            logger.error(f"Failed to close {mint[:8]}...: {e}")
            import traceback
            logger.error(traceback.format_exc())
            if mint in self.positions:
                self.positions[mint].status = 'error'
                del self.positions[mint]
                self.velocity_checker.clear_history(mint)
    
    async def _close_position(self, mint: str, reason: str = "manual"):
        """Wrapper for telegram compatibility"""
        await self._close_position_full(mint, reason)
    
    async def run(self):
        """Main run loop (existing logic preserved)"""
        self.running = True
        
        try:
            await self.initialize_telegram()
            
            self.scanner = PumpPortalMonitor(self.on_token_found)
            self.scanner_task = asyncio.create_task(self.scanner.start())
            
            logger.info("✅ Bot running with PROBE/CONFIRM + DYNAMIC TPS STRATEGY")
            logger.info(f"🎯 Entry: {PROBE_AMOUNT_SOL} probe + {CONFIRM_AMOUNT_SOL} confirm")
            logger.info(f"⚡ Health: {HEALTH_CHECK_START}-{HEALTH_CHECK_END}s")
            logger.info(f"⏱️ Timer: {TIMER_EXIT_BASE_SECONDS}→{TIMER_AUTO_EXTEND_TO}s")
            
            last_stats_time = time.time()
            
            while self.running and not self.shutdown_requested:
                await asyncio.sleep(10)
                
                if time.time() - last_stats_time > 60:
                    if self.positions:
                        logger.info(f"📊 ACTIVE POSITIONS: {len(self.positions)}")
                        for mint, pos in self.positions.items():
                            time_until_exit = pos.exit_time - time.time()
                            logger.info(
                                f"  • {mint[:8]}... | P&L: {pos.pnl_percent:+.1f}% | "
                                f"Max: {pos.max_pnl_reached:+.1f}% | "
                                f"Exit in: {time_until_exit:.1f}s"
                            )
                    
                    perf_stats = self.tracker.get_session_stats()
                    if perf_stats['total_buys'] > 0:
                        logger.info(f"📊 SESSION PERFORMANCE:")
                        logger.info(f"  • Trades: {perf_stats['total_buys']} buys, {perf_stats['total_sells']} sells")
                        logger.info(f"  • Win rate: {perf_stats['win_rate_percent']:.1f}%")
                        logger.info(f"  • P&L: {perf_stats['total_pnl_sol']:+.4f} SOL")
                    
                    if self.total_realized_sol != 0:
                        logger.info(f"💰 Total realized: {self.total_realized_sol:+.4f} SOL")
                    
                    last_stats_time = time.time()
            
        except KeyboardInterrupt:
            logger.info("\n🛑 Shutting down...")
        except Exception as e:
            logger.error(f"Fatal error: {e}")
            if self.telegram:
                await self.telegram.send_message(f"❌ Bot crashed: {e}")
        finally:
            await self.shutdown()
    
    async def shutdown(self):
        """Clean shutdown (existing logic preserved)"""
        self.running = False
        logger.info("Starting shutdown...")
        
        self.tracker.log_session_summary()
        
        if self.telegram and not self.shutdown_requested:
            await self.telegram.send_message(
                f"🛑 Bot shutting down\n"
                f"Total realized: {self.total_realized_sol:+.4f} SOL"
            )
        
        if self.scanner_task and not self.scanner_task.done():
            self.scanner_task.cancel()
            try:
                await self.scanner_task
            except asyncio.CancelledError:
                pass
        
        if self.scanner:
            self.scanner.stop()
        
        if self.positions:
            logger.info(f"Closing {len(self.positions)} positions...")
            for mint in list(self.positions.keys()):
                await self._close_position_full(mint, reason="shutdown")
        
        if self.telegram_polling_task and not self.telegram_polling_task.done():
            self.telegram_polling_task.cancel()
            try:
                await self.telegram_polling_task
            except asyncio.CancelledError:
                pass
        
        if self.telegram:
            self.telegram.stop()
        
        if self.total_trades > 0:
            win_rate = (self.profitable_trades / self.total_trades * 100)
            logger.info(f"📊 Final Stats:")
            logger.info(f"  • Trades: {self.total_trades}")
            logger.info(f"  • Win rate: {win_rate:.1f}%")
            logger.info(f"  • Realized: {self.total_realized_sol:+.4f} SOL")
        
        logger.info("✅ Shutdown complete")

if __name__ == "__main__":
    import os
    from aiohttp import web
    
    port = int(os.getenv("PORT", "10000"))
    
    async def health_handler(request):
        return web.Response(text="Bot is running", status=200)
    
    async def start_health_server():
        app = web.Application()
        app.router.add_get("/", health_handler)
        app.router.add_get("/health", health_handler)
        
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        logger.info(f"✅ Health server on port {port}")
        return runner
    
    async def main_with_health():
        health_runner = await start_health_server()
        
        try:
            bot = SniperBot()
            await bot.run()
        finally:
            await health_runner.cleanup()
    
    def signal_handler(sig, frame):
        logger.info("\nReceived interrupt signal")
        for task in asyncio.all_tasks():
            task.cancel()
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        asyncio.run(main_with_health())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        import traceback
        traceback.print_exc()
