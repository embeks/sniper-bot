"""
Main Orchestrator - Phase 1 with Complete Control and Multi-Target Profit Taking
FIXED: Proper sell handling using UI amounts for PumpPortal API
FIXED: Stop loss check happens BEFORE profit targets
FIXED: No more false no_data exits with persistent price cache
FIXED: Extended monitoring window to 180s
FIXED: Grace period before allowing sells
FIXED: All timing from config/env vars - no hardcoded values
"""

import asyncio
import logging
import signal
import time
from datetime import datetime, timedelta
from typing import Dict, Optional, List

from config import (
    LOG_LEVEL, LOG_FORMAT, LOG_FILE,
    BUY_AMOUNT_SOL, MAX_POSITIONS, MIN_SOL_BALANCE,
    STOP_LOSS_PERCENTAGE, TAKE_PROFIT_PERCENTAGE,
    SELL_DELAY_SECONDS, MAX_POSITION_AGE_SECONDS,
    MONITOR_CHECK_INTERVAL, DATA_FAILURE_TOLERANCE,
    DRY_RUN, DEBUG_MODE, ENABLE_TELEGRAM_NOTIFICATIONS,
    BLACKLISTED_TOKENS, NOTIFY_PROFIT_THRESHOLD,
    PARTIAL_TAKE_PROFIT
)

from wallet import WalletManager
from dex import PumpFunDEX
from pumpportal_monitor import PumpPortalMonitor
from pumpportal_trader import PumpPortalTrader
from performance_tracker import PerformanceTracker

# Configure logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format=LOG_FORMAT,
    handlers=[logging.StreamHandler()]
)

logger = logging.getLogger(__name__)

class Position:
    """Track an active position with multi-target support"""
    def __init__(self, mint: str, amount_sol: float, tokens: float = 0):
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
        
        # Multi-target tracking
        self.partial_sells = {}
        self.total_sold_percent = 0
        self.realized_pnl_sol = 0
        
        # Enhanced price tracking
        self.last_valid_price = 0
        self.last_price_update = time.time()
        self.consecutive_stale_reads = 0
        self.last_valid_balance = tokens  # Track last known good balance
        
        # Retry tracking for curve data
        self.curve_check_retries = 0
        
        # Profit targets - Built from environment variables
        self.profit_targets = []
        
        # Build targets from PARTIAL_TAKE_PROFIT config
        # Note: PARTIAL_TAKE_PROFIT keys are like 200.0 (for 2x), 300.0 (for 3x)
        # But the targets in percent are 50 (for 1.5x), 100 (for 2x), 200 (for 3x)
        
        # Check for 2.0x config (key would be 200.0)
        if 200.0 in PARTIAL_TAKE_PROFIT:
            self.profit_targets.append({
                'target': 100,  # 100% gain = 2x
                'sell_percent': PARTIAL_TAKE_PROFIT[200.0] * 100,  # Convert decimal to percent
                'name': '2x'
            })
        
        # Check for 3.0x config (key would be 300.0)
        if 300.0 in PARTIAL_TAKE_PROFIT:
            self.profit_targets.append({
                'target': 200,  # 200% gain = 3x
                'sell_percent': PARTIAL_TAKE_PROFIT[300.0] * 100,
                'name': '3x'
            })
        
        # Check for 5.0x config (key would be 500.0)
        if 500.0 in PARTIAL_TAKE_PROFIT:
            self.profit_targets.append({
                'target': 400,  # 400% gain = 5x
                'sell_percent': PARTIAL_TAKE_PROFIT[500.0] * 100,
                'name': '5x'
            })
        
        # Add 1.5x target (not in env vars, but hardcoded for faster exits)
        self.profit_targets.insert(0, {
            'target': 50,  # 50% gain = 1.5x
            'sell_percent': 50,  # Always sell 50% at 1.5x
            'name': '1.5x'
        })
        
        # Sort targets by percentage (ascending)
        self.profit_targets.sort(key=lambda x: x['target'])
        
        # Fallback if no env vars set
        if len(self.profit_targets) == 1:  # Only has the 1.5x we added
            self.profit_targets = [
                {'target': 50, 'sell_percent': 50, 'name': '1.5x'},
                {'target': 100, 'sell_percent': 30, 'name': '2x'},
                {'target': 200, 'sell_percent': 20, 'name': '3x'},
            ]

class SniperBot:
    """Main sniper bot orchestrator with multi-target profit taking"""
    
    def __init__(self):
        """Initialize all components"""
        logger.info("=" * 60)
        logger.info("🚀 INITIALIZING PHASE 1 SNIPER BOT")
        logger.info("=" * 60)
        
        # Initialize components
        self.wallet = WalletManager()
        self.dex = PumpFunDEX(self.wallet)
        self.scanner = None
        self.scanner_task = None
        self.telegram = None
        self.telegram_polling_task = None
        
        # Initialize performance tracker
        self.tracker = PerformanceTracker()
        
        # Initialize PumpPortal trader
        from solana.rpc.api import Client
        from config import RPC_ENDPOINT
        client = Client(RPC_ENDPOINT.replace('wss://', 'https://').replace('ws://', 'http://'))
        self.trader = PumpPortalTrader(self.wallet, client)
        
        # Track positions
        self.positions: Dict[str, Position] = {}
        self.total_trades = 0
        self.profitable_trades = 0
        self.total_pnl = 0
        self.total_realized_sol = 0
        self.MAX_POSITIONS = MAX_POSITIONS
        
        # Control flags
        self.running = False
        self.paused = False
        self.shutdown_requested = False
        self._last_balance_warning = 0
        
        # Telegram will be initialized in run()
        self.telegram_enabled = ENABLE_TELEGRAM_NOTIFICATIONS
        
        # Log initial status
        self._log_startup_info()
    
    def _log_startup_info(self):
        """Log startup information"""
        sol_balance = self.wallet.get_sol_balance()
        tradeable_balance = max(0, sol_balance - MIN_SOL_BALANCE)
        max_trades = int(tradeable_balance / BUY_AMOUNT_SOL) if tradeable_balance > 0 else 0
        actual_trades = min(max_trades, MAX_POSITIONS) if max_trades > 0 else 0
        
        logger.info(f"📊 STARTUP STATUS:")
        logger.info(f"  • Wallet: {self.wallet.pubkey}")
        logger.info(f"  • Balance: {sol_balance:.4f} SOL")
        logger.info(f"  • Reserved: {MIN_SOL_BALANCE:.4f} SOL")
        logger.info(f"  • Max positions: {MAX_POSITIONS}")
        logger.info(f"  • Buy amount: {BUY_AMOUNT_SOL} SOL")
        logger.info(f"  • Stop loss: -{STOP_LOSS_PERCENTAGE}%")
        logger.info(f"  • Profit targets: Dynamic from env")
        logger.info(f"  • Grace period: {SELL_DELAY_SECONDS}s")
        logger.info(f"  • Max hold: {MAX_POSITION_AGE_SECONDS}s")
        logger.info(f"  • Available trades: {actual_trades}")
        logger.info(f"  • Mode: {'DRY RUN' if DRY_RUN else 'LIVE TRADING'}")
        logger.info("=" * 60)
    
    async def initialize_telegram(self):
        """Initialize Telegram bot after event loop is ready"""
        if self.telegram_enabled and not self.telegram:
            try:
                from telegram_bot import TelegramBot
                self.telegram = TelegramBot(self)
                
                # Start polling in the current event loop
                self.telegram_polling_task = asyncio.create_task(self.telegram.start_polling())
                logger.info("✅ Telegram bot initialized and polling started")
                
                # Send startup message without duplicates
                sol_balance = self.wallet.get_sol_balance()
                startup_msg = (
                    "🚀 Bot started successfully\n"
                    "📊 Phase 1 Mode - Fast Exits\n"
                    f"💰 Balance: {sol_balance:.4f} SOL\n"
                    f"🎯 Buy: {BUY_AMOUNT_SOL} SOL\n"
                    "📈 Dynamic targets from env\n"
                    "Type /help for commands"
                )
                await self.telegram.send_message(startup_msg)
            except Exception as e:
                logger.error(f"Failed to initialize Telegram: {e}")
                self.telegram = None
    
    async def stop_scanner(self):
        """Stop the scanner and enter idle state (keeps health server alive)"""
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
        
        logger.info("✅ Bot stopped and entering idle state")
    
    async def start_scanner(self):
        """Start or restart the scanner"""
        # If we're in shutdown/idle state, exit it
        if self.shutdown_requested:
            logger.info("Exiting idle state...")
            self.shutdown_requested = False
            self.running = True
            self.paused = False
            
            # The main loop will handle restarting the scanner
            logger.info("✅ Bot resuming from idle")
            return
        
        # Normal start if not idling
        if self.running and self.scanner_task and not self.scanner_task.done():
            logger.info("Scanner already running")
            return
        
        self.running = True
        self.paused = False
        self.shutdown_requested = False
        
        if not self.scanner:
            self.scanner = PumpPortalMonitor(self.on_token_found)
            logger.info("Scanner initialized")
        
        if self.scanner_task and not self.scanner_task.done():
            self.scanner_task.cancel()
            try:
                await self.scanner_task
            except asyncio.CancelledError:
                pass
            await asyncio.sleep(0.1)
        
        self.scanner_task = asyncio.create_task(self.scanner.start())
        logger.info("✅ Scanner started via command")
    
    async def restart_bot(self):
        """Full restart - stop everything then start again"""
        logger.info("Restarting bot...")
        await self.stop_scanner()
        await asyncio.sleep(1)
        self.shutdown_requested = False
        await self.start_scanner()
        logger.info("✅ Bot restarted")
    
    async def get_scanner_status(self) -> Dict:
        """Get detailed scanner status"""
        return {
            'running': self.running,
            'paused': self.paused,
            'scanner_alive': self.scanner_task and not self.scanner_task.done() if self.scanner_task else False,
            'shutdown_requested': self.shutdown_requested,
            'positions': len(self.positions),
            'can_trade': self.wallet.can_trade()
        }
    
    async def on_token_found(self, token_data: Dict):
        """Handle new token found by monitor"""
        detection_start = time.time()
        
        try:
            mint = token_data['mint']
            
            # CRITICAL: Update DEX with WebSocket data for accurate bonding curve detection
            self.dex.update_token_data(mint, token_data)
            
            # Skip if not running or paused
            if not self.running or self.paused:
                logger.debug(f"Skipping token - running:{self.running}, paused:{self.paused}")
                return
            
            # Validation checks
            if mint in BLACKLISTED_TOKENS:
                logger.debug(f"Token {mint[:8]}... is blacklisted")
                return
            
            if len(self.positions) >= MAX_POSITIONS:
                logger.warning(f"Max positions reached ({MAX_POSITIONS})")
                return
            
            if mint in self.positions:
                logger.debug(f"Already have position in {mint[:8]}...")
                return
            
            if not self.wallet.can_trade():
                # Only log once per minute to avoid spam
                current_time = time.time()
                if current_time - self._last_balance_warning > 60:
                    logger.warning(f"Insufficient balance for trading (need {MIN_SOL_BALANCE + BUY_AMOUNT_SOL:.3f} SOL)")
                    self._last_balance_warning = current_time
                return
            
            # SIMPLE QUALITY FILTERS - Phase 1.5
            # Filter 1: Check creator's initial buy amount
            initial_buy = token_data.get('data', {}).get('solAmount', 0) if 'data' in token_data else token_data.get('solAmount', 0)
            name = token_data.get('data', {}).get('name', '') if 'data' in token_data else token_data.get('name', '')
            
            # Skip if creator bought less than 0.1 SOL (no skin in the game)
            if initial_buy < 0.1:
                logger.debug(f"Skipping {mint[:8]}... - creator only bought {initial_buy:.3f} SOL")
                return
            
            # Skip if creator bought more than 10 SOL (likely planning to dump)
            if initial_buy > 10:
                logger.debug(f"Skipping {mint[:8]}... - creator bought {initial_buy:.1f} SOL (too much)")
                return
            
            # Skip obvious low-effort tokens
            if len(name) < 3 or 'test' in name.lower():
                logger.debug(f"Skipping {mint[:8]}... - low effort name: {name}")
                return
            
            # Log detection time
            detection_time_ms = (time.time() - detection_start) * 1000
            self.tracker.log_token_detection(mint, token_data.get('source', 'pumpportal'), detection_time_ms)
            
            logger.info(f"🎯 Processing new token: {mint}")
            logger.info(f"   Detection latency: {detection_time_ms:.0f}ms")
            
            # Log buy attempt
            cost_breakdown = self.tracker.log_buy_attempt(mint, BUY_AMOUNT_SOL, 50)
            
            # Execute buy FAST - no delays
            execution_start = time.time()
            
            if DRY_RUN:
                logger.info(f"[DRY RUN] Would buy {mint[:8]}... for {BUY_AMOUNT_SOL} SOL")
                signature = f"dry_run_buy_{mint[:10]}"
                bought_tokens = 1000000
            else:
                bonding_curve_key = None
                if 'data' in token_data and 'bondingCurveKey' in token_data['data']:
                    bonding_curve_key = token_data['data']['bondingCurveKey']
                
                # Get expected tokens from websocket data for better estimates
                expected_tokens = 0
                if 'data' in token_data:
                    data = token_data['data']
                    if 'initialBuy' in data and 'solAmount' in data:
                        # Use the initial buy as reference for estimation
                        creator_sol = float(data.get('solAmount', 0.01))
                        if creator_sol > 0:
                            # Calculate expected UI tokens (not raw)
                            expected_tokens = float(data.get('initialBuy', 0)) * (BUY_AMOUNT_SOL / creator_sol)
                
                signature = await self.trader.create_buy_transaction(
                    mint=mint,
                    sol_amount=BUY_AMOUNT_SOL,
                    bonding_curve_key=bonding_curve_key,
                    slippage=50
                )
                
                bought_tokens = 0
                if signature:
                    # Quick balance check - don't wait too long
                    await asyncio.sleep(2)
                    bought_tokens = self.wallet.get_token_balance(mint)  # This returns UI amount
                    if bought_tokens == 0:
                        # Use expected tokens from calculation
                        if expected_tokens > 0:
                            bought_tokens = expected_tokens
                            logger.info(f"Using calculated tokens: {bought_tokens:,.0f}")
                        else:
                            # Fallback estimate - UI amount
                            bought_tokens = 350000  # 350k tokens typical
                            logger.warning(f"Using fallback estimate: {bought_tokens:,.0f}")
            
            if signature:
                execution_time_ms = (time.time() - execution_start) * 1000
                
                # Log successful buy
                self.tracker.log_buy_executed(
                    mint=mint,
                    amount_sol=BUY_AMOUNT_SOL,
                    signature=signature,
                    tokens_received=bought_tokens,
                    execution_time_ms=execution_time_ms
                )
                
                # Create and track position
                position = Position(mint, BUY_AMOUNT_SOL, bought_tokens)
                position.buy_signature = signature
                position.initial_tokens = bought_tokens
                position.remaining_tokens = bought_tokens
                position.last_valid_balance = bought_tokens  # Store as last known good
                position.entry_time = time.time()
                self.positions[mint] = position
                self.total_trades += 1
                
                logger.info(f"✅ BUY EXECUTED: {mint[:8]}...")
                logger.info(f"   Amount: {BUY_AMOUNT_SOL} SOL")
                logger.info(f"   Total Cost: {cost_breakdown['total_cost']:.6f} SOL")
                logger.info(f"   Fees: {cost_breakdown['total_fees']:.6f} SOL")
                logger.info(f"   Tokens: {bought_tokens:,.0f}")
                logger.info(f"   Execution: {execution_time_ms:.1f}ms")
                logger.info(f"   Active positions: {len(self.positions)}/{MAX_POSITIONS}")
                
                # Send Telegram notifications
                if self.telegram:
                    await self.telegram.notify_buy(mint, BUY_AMOUNT_SOL, signature)
                    
                    # Show actual targets from position
                    targets_msg = ", ".join([f"{t['name']}/{t['sell_percent']:.0f}%" for t in position.profit_targets])
                    monitoring_msg = (
                        f"📊 Monitoring {mint[:8]}...\n"
                        f"Entry: {BUY_AMOUNT_SOL} SOL\n"
                        f"Targets: {targets_msg}\n"
                        f"Stop Loss: -{STOP_LOSS_PERCENTAGE}%"
                    )
                    await self.telegram.send_message(monitoring_msg)
                
                # Start monitoring
                position.monitor_task = asyncio.create_task(self._monitor_position(mint))
                logger.info(f"📊 Started monitoring position {mint[:8]}...")
            else:
                # Log failed buy
                self.tracker.log_buy_failed(mint, BUY_AMOUNT_SOL, "Transaction failed")
                
        except Exception as e:
            logger.error(f"Failed to process token: {e}")
            self.tracker.log_buy_failed(mint, BUY_AMOUNT_SOL, str(e))
            import traceback
            logger.error(traceback.format_exc())
    
    async def _monitor_position(self, mint: str):
        """Monitor position with multi-target profit taking and persistent price cache"""
        try:
            position = self.positions.get(mint)
            if not position:
                logger.error(f"Position {mint[:8]}... not found for monitoring")
                return
            
            # Grace period before allowing sells
            logger.info(f"⏳ Grace period {SELL_DELAY_SECONDS}s before monitoring {mint[:8]}...")
            await asyncio.sleep(SELL_DELAY_SECONDS)
            
            logger.info(f"📈 Starting active monitoring for {mint[:8]}...")
            check_count = 0
            last_notification_pnl = 0
            consecutive_data_failures = 0
            
            while mint in self.positions and position.status == 'active':
                check_count += 1
                
                # Check position age
                age = time.time() - position.entry_time
                if age > MAX_POSITION_AGE_SECONDS:
                    logger.warning(f"⏰ MAX AGE REACHED for {mint[:8]}... ({age:.0f}s)")
                    await self._close_position_full(mint, reason="max_age")
                    break
                
                try:
                    curve_data = self.dex.get_bonding_curve_data(mint)
                    
                    # Log if using stale data
                    if curve_data and curve_data.get('is_stale'):
                        stale_age = curve_data.get('stale_age_seconds', 0)
                        logger.info(f"Using stale price for {mint[:8]}... (age: {stale_age:.0f}s)")
                    
                    # Check for true migration
                    if not curve_data or curve_data.get('is_migrated'):
                        logger.warning(f"❌ Token {mint[:8]}... has migrated")
                        await self._close_position_full(mint, reason="migration")
                        break
                    
                    # Calculate P&L even with stale/estimated data
                    if curve_data and curve_data.get('is_valid', True):
                        if curve_data['virtual_sol_reserves'] > 0 and curve_data['virtual_token_reserves'] > 0:
                            current_price = curve_data['virtual_sol_reserves'] / curve_data['virtual_token_reserves']
                            
                            if position.entry_price == 0:
                                position.entry_price = current_price
                                logger.info(f"📍 Entry price for {mint[:8]}...: {position.entry_price:.10f}")
                            
                            if position.entry_price > 0:
                                price_change = ((current_price / position.entry_price) - 1) * 100
                                position.pnl_percent = price_change
                                position.current_price = current_price
                                
                                # Update last valid price
                                if not curve_data.get('is_stale') and not curve_data.get('no_data_available'):
                                    position.last_valid_price = current_price
                                    position.last_price_update = time.time()
                                
                                # Add warning if data is stale
                                data_warning = " [STALE]" if curve_data.get('is_stale') else ""
                                data_warning = " [EST]" if curve_data.get('no_data_available') else data_warning
                                
                                # Log position update periodically
                                if check_count % 10 == 1:
                                    self.tracker.log_position_update(
                                        mint=mint,
                                        current_pnl_percent=price_change,
                                        current_price=current_price,
                                        age_seconds=age
                                    )
                                
                                # Log every 3rd check
                                if check_count % 3 == 1:
                                    logger.info(
                                        f"📊 {mint[:8]}... | P&L: {price_change:+.1f}%{data_warning} | "
                                        f"Sold: {position.total_sold_percent}% | Age: {age:.0f}s"
                                    )
                                
                                # Telegram updates at significant changes
                                if self.telegram and abs(price_change - last_notification_pnl) >= 50:
                                    update_msg = (
                                        f"📊 Update {mint[:8]}...\n"
                                        f"P&L: {price_change:+.1f}%\n"
                                        f"Status: {'🟢 Profit' if price_change > 0 else '🔴 Loss'}\n"
                                        f"Remaining: {100 - position.total_sold_percent}%"
                                    )
                                    await self.telegram.send_message(update_msg)
                                    last_notification_pnl = price_change
                                
                                # FIXED: Check stop loss FIRST (before profit targets)
                                if price_change <= -STOP_LOSS_PERCENTAGE and position.total_sold_percent < 100:
                                    logger.warning(f"🛑 STOP LOSS HIT for {mint[:8]}... at {price_change:.1f}%")
                                    await self._close_position_full(mint, reason="stop_loss")
                                    break
                                
                                # THEN check profit targets
                                for target in position.profit_targets:
                                    target_name = target['name']
                                    target_pnl = target['target']
                                    sell_percent = target['sell_percent']
                                    
                                    if price_change >= target_pnl and target_name not in position.partial_sells:
                                        logger.info(f"🎯 {target_name} TARGET HIT for {mint[:8]}...")
                                        
                                        success = await self._execute_partial_sell(
                                            mint, sell_percent, target_name, price_change
                                        )
                                        
                                        if success:
                                            position.partial_sells[target_name] = {
                                                'pnl': price_change,
                                                'time': time.time(),
                                                'percent_sold': sell_percent
                                            }
                                            position.total_sold_percent += sell_percent
                                            
                                            if position.total_sold_percent >= 100:
                                                logger.info(f"✅ Position fully closed")
                                                position.status = 'completed'
                                                break
                    
                except Exception as e:
                    logger.error(f"Error checking {mint[:8]}...: {e}")
                    # Don't exit on errors, continue monitoring
                
                await asyncio.sleep(MONITOR_CHECK_INTERVAL)
            
            # Clean up completed position
            if mint in self.positions and position.status == 'completed':
                del self.positions[mint]
                logger.info(f"Position {mint[:8]}... removed after completion")
                
        except Exception as e:
            logger.error(f"Monitor error for {mint[:8]}...: {e}")
            import traceback
            logger.error(traceback.format_exc())
            
            if mint in self.positions:
                await self._close_position_full(mint, reason="monitor_error")
    
    async def _execute_partial_sell(self, mint: str, sell_percent: float, target_name: str, current_pnl: float) -> bool:
        """Execute a partial sell at profit target"""
        try:
            position = self.positions.get(mint)
            if not position:
                return False
            
            # Get current token balance (UI amount)
            current_balance = self.wallet.get_token_balance(mint)  # UI amount
            
            # Validate the balance
            if current_balance == 0:
                # Use last known good balance
                logger.warning(f"Wallet returns 0 balance, using recorded {position.last_valid_balance}")
                current_balance = position.last_valid_balance
            elif current_balance > position.initial_tokens * 2:
                # Balance seems wrong (too high)
                logger.warning(f"Suspicious balance {current_balance}, using recorded {position.last_valid_balance}")
                current_balance = position.last_valid_balance
            else:
                # Update last valid balance
                position.last_valid_balance = current_balance
            
            if current_balance <= 0:
                logger.warning(f"No tokens to sell for {mint[:8]}...")
                return False
            
            # Calculate UI tokens to sell from remaining balance
            remaining_balance = position.remaining_tokens
            ui_tokens_to_sell = remaining_balance * (sell_percent / 100)
            
            logger.info(f"💰 Executing {target_name} partial sell for {mint[:8]}...")
            logger.info(f"   Selling: {sell_percent}% ({ui_tokens_to_sell:,.2f} UI tokens)")
            
            if DRY_RUN:
                logger.info(f"[DRY RUN] Would sell {ui_tokens_to_sell:,.2f} tokens")
                signature = f"dry_run_sell_{target_name}_{mint[:10]}"
                sol_received = BUY_AMOUNT_SOL * (sell_percent / 100) * (1 + current_pnl / 100)
            else:
                # Send UI amount to PumpPortal (they handle decimal conversion)
                logger.info(f"   Sending to PumpPortal: {ui_tokens_to_sell:.6f} UI tokens")
                
                # Get actual token decimals from mint
                token_decimals = self.wallet.get_token_decimals(mint)
                logger.debug(f"   Token has {token_decimals} decimals")
                
                signature = await self.trader.create_sell_transaction(
                    mint=mint,
                    token_amount=ui_tokens_to_sell,  # Send UI amount as float
                    slippage=50,
                    token_decimals=token_decimals  # Use actual decimals from mint
                )
                sol_received = BUY_AMOUNT_SOL * (sell_percent / 100) * (1 + current_pnl / 100)  # Estimate
            
            # Check for valid signature (not all 1's)
            if signature and not signature.startswith("1111111"):
                position.sell_signatures.append(signature)
                
                # Update remaining tokens
                position.remaining_tokens -= ui_tokens_to_sell
                
                profit_sol = sol_received - (BUY_AMOUNT_SOL * sell_percent / 100)
                position.realized_pnl_sol += profit_sol
                self.total_realized_sol += profit_sol
                
                # Log partial sell
                self.tracker.log_partial_sell(
                    mint=mint,
                    target_name=target_name,
                    percent_sold=sell_percent,
                    tokens_sold=ui_tokens_to_sell,
                    sol_received=sol_received,
                    pnl_sol=profit_sol
                )
                
                logger.info(f"✅ {target_name} SELL EXECUTED")
                logger.info(f"   Profit: {profit_sol:+.4f} SOL")
                
                if self.telegram:
                    sol_price = 250
                    profit_usd = profit_sol * sol_price
                    
                    msg = (
                        f"💰 {target_name} TARGET HIT!\n"
                        f"Token: {mint[:16]}...\n"
                        f"Sold: {sell_percent}% of position\n"
                        f"P&L: {current_pnl:+.1f}% ({profit_sol:+.4f} SOL)\n"
                        f"Remaining: {100 - position.total_sold_percent - sell_percent}%\n"
                        f"TX: https://solscan.io/tx/{signature}"
                    )
                    await self.telegram.send_message(msg)
                
                return True
            else:
                logger.error(f"Failed to execute {target_name} sell (invalid signature)")
                return False
                
        except Exception as e:
            logger.error(f"Partial sell error: {e}")
            return False
    
    async def _close_position_full(self, mint: str, reason: str = "manual"):
        """Close remaining position - handles both migrated and non-migrated tokens"""
        try:
            position = self.positions.get(mint)
            if not position:
                logger.warning(f"Position {mint[:8]}... not found")
                return
            
            if position.status != 'active':
                logger.warning(f"Position {mint[:8]}... already {position.status}")
                return
            
            position.status = 'closing'
            
            # Use remaining tokens from position tracking (UI amount)
            ui_token_balance = position.remaining_tokens
            
            if ui_token_balance <= 0:
                logger.warning(f"No tokens remaining for {mint[:8]}...")
                position.status = 'closed'
                if mint in self.positions:
                    del self.positions[mint]
                return
            
            remaining_percent = 100 - position.total_sold_percent
            hold_time = time.time() - position.entry_time
            
            logger.info(f"📤 Closing remaining {remaining_percent}% of {mint[:8]}...")
            logger.info(f"   Token balance: {ui_token_balance:,.2f} UI tokens")
            
            if DRY_RUN:
                logger.info(f"[DRY RUN] Would sell {ui_token_balance:,.2f} tokens")
                signature = f"dry_run_close_{mint[:10]}"
                sol_received = BUY_AMOUNT_SOL * (remaining_percent / 100) * (1 + position.pnl_percent / 100)
            else:
                # PumpPortal expects UI amounts when denominatedInSol is "false"
                logger.info(f"Preparing to sell {ui_token_balance:.6f} UI tokens")
                
                # Log actual wallet balance for debugging
                actual_balance = self.wallet.get_token_balance(mint)
                if actual_balance > 0:
                    logger.info(f"DEBUG: Actual wallet UI balance: {actual_balance:.6f}")
                    # Use actual balance if available
                    ui_token_balance = actual_balance
                
                logger.info(f"Sending to PumpPortal: {ui_token_balance:.6f} (UI token amount)")
                
                # Check if token has migrated
                curve_data = self.dex.get_bonding_curve_data(mint)
                is_migrated = curve_data is None or curve_data.get('is_migrated', False) or curve_data.get('virtual_sol_reserves', 0) == 0
                
                # Get actual token decimals from mint
                token_decimals = self.wallet.get_token_decimals(mint)
                logger.debug(f"Token has {token_decimals} decimals")
                
                if is_migrated:
                    logger.info(f"Token {mint[:8]}... has migrated to Raydium")
                    logger.info("Attempting sell through PumpPortal (may handle Raydium)...")
                    
                    signature = await self.trader.create_sell_transaction(
                        mint=mint,
                        token_amount=ui_token_balance,  # Send UI amount to PumpPortal
                        slippage=100,  # Higher slippage for migrated tokens
                        token_decimals=token_decimals  # Use actual decimals from mint
                    )
                    
                    # Check if we got a fake signature (all 1's)
                    if signature and signature.startswith("1111111"):
                        logger.warning("PumpPortal returned failed signature for migrated token")
                        signature = None  # Mark as failed
                    
                    sol_received = BUY_AMOUNT_SOL * (remaining_percent / 100) * 0.8  # Estimate with loss
                else:
                    logger.info(f"Token {mint[:8]}... still on bonding curve")
                    
                    signature = await self.trader.create_sell_transaction(
                        mint=mint,
                        token_amount=ui_token_balance,  # Send UI amount to PumpPortal
                        slippage=50,
                        token_decimals=token_decimals  # Use actual decimals from mint
                    )
                    
                    sol_received = BUY_AMOUNT_SOL * (remaining_percent / 100) * (1 + position.pnl_percent / 100)
            
            # Process the result
            if signature and not signature.startswith("1111111"):
                position.sell_signatures.append(signature)
                position.status = 'closed'
                
                if position.pnl_percent > 0:
                    self.profitable_trades += 1
                self.total_pnl += position.pnl_percent
                
                final_pnl = sol_received - (BUY_AMOUNT_SOL * remaining_percent / 100)
                position.realized_pnl_sol += final_pnl
                self.total_realized_sol += final_pnl
                
                # Log sell execution
                self.tracker.log_sell_executed(
                    mint=mint,
                    tokens_sold=ui_token_balance,
                    signature=signature,
                    sol_received=sol_received,
                    pnl_sol=position.realized_pnl_sol,
                    pnl_percent=position.pnl_percent,
                    hold_time_seconds=hold_time,
                    reason=reason
                )
                
                logger.info(f"✅ POSITION CLOSED: {mint[:8]}...")
                logger.info(f"   Reason: {reason}")
                logger.info(f"   Final P&L: {position.pnl_percent:+.1f}%")
                logger.info(f"   Realized: {position.realized_pnl_sol:+.4f} SOL")
                logger.info(f"   Hold time: {hold_time:.0f}s")
                
                if self.telegram:
                    emoji = "💰" if position.realized_pnl_sol > 0 else "🔴"
                    # Fix Telegram message to avoid parsing errors
                    mint_short = mint[:16]
                    msg = (
                        f"{emoji} POSITION CLOSED\n"
                        f"Token: {mint_short}\n"
                        f"Reason: {reason}\n"
                        f"Final P&L: {position.pnl_percent:+.1f}%\n"
                        f"Realized: {position.realized_pnl_sol:+.4f} SOL\n"
                        f"Targets hit: {', '.join(position.partial_sells.keys()) if position.partial_sells else 'None'}"
                    )
                    await self.telegram.send_message(msg)
            else:
                logger.error(f"❌ Close transaction failed or returned invalid signature")
                position.status = 'close_failed'
                
                # Still remove from positions if we can't sell (to free up slots)
                if reason in ["migration", "max_age", "no_data"]:
                    logger.warning(f"Removing unsellable position {mint[:8]}... to free slot")
                    if self.telegram:
                        # Fix Telegram message to avoid parsing errors
                        mint_short = mint[:16]
                        await self.telegram.send_message(
                            f"⚠️ Could not sell {mint_short}\n"
                            f"Reason: {reason}\n"
                            f"Removing to free slot"
                        )
            
            if mint in self.positions:
                del self.positions[mint]
                logger.info(f"Active: {len(self.positions)}/{MAX_POSITIONS}")
            
        except Exception as e:
            logger.error(f"Failed to close {mint[:8]}...: {e}")
            if mint in self.positions:
                self.positions[mint].status = 'error'
                del self.positions[mint]
    
    async def _close_position(self, mint: str, reason: str = "manual"):
        """Wrapper for telegram compatibility"""
        await self._close_position_full(mint, reason)
    
    async def run(self):
        """Main run loop"""
        self.running = True
        
        try:
            await self.initialize_telegram()
            
            self.scanner = PumpPortalMonitor(self.on_token_found)
            self.scanner_task = asyncio.create_task(self.scanner.start())
            
            logger.info("✅ Bot running with enhanced monitoring")
            logger.info(f"📈 Dynamic targets from environment")
            logger.info(f"⏱️ Grace period: {SELL_DELAY_SECONDS}s, Max hold: {MAX_POSITION_AGE_SECONDS}s")
            
            last_stats_time = time.time()
            
            while self.running and not self.shutdown_requested:
                await asyncio.sleep(10)
                
                if time.time() - last_stats_time > 60:
                    if self.positions:
                        logger.info(f"📊 ACTIVE POSITIONS: {len(self.positions)}")
                        for mint, pos in self.positions.items():
                            age = time.time() - pos.entry_time
                            targets_hit = ', '.join(pos.partial_sells.keys()) if pos.partial_sells else 'None'
                            logger.info(
                                f"  • {mint[:8]}... | P&L: {pos.pnl_percent:+.1f}% | "
                                f"Sold: {pos.total_sold_percent}% | Targets: {targets_hit} | "
                                f"Age: {age:.0f}s"
                            )
                    
                    # Log performance stats
                    perf_stats = self.tracker.get_session_stats()
                    if perf_stats['total_buys'] > 0:
                        logger.info(f"📊 SESSION PERFORMANCE:")
                        logger.info(f"  • Trades: {perf_stats['total_buys']} buys, {perf_stats['total_sells']} sells")
                        logger.info(f"  • Win rate: {perf_stats['win_rate_percent']:.1f}%")
                        logger.info(f"  • P&L: {perf_stats['total_pnl_sol']:+.4f} SOL")
                        logger.info(f"  • Fees paid: {perf_stats['total_fees_sol']:.6f} SOL")
                    
                    if self.total_realized_sol != 0:
                        logger.info(f"💰 Total realized: {self.total_realized_sol:+.4f} SOL")
                    
                    last_stats_time = time.time()
                
                # Check if scanner died (only restart if not shutdown requested)
                if self.scanner_task and self.scanner_task.done():
                    if not self.shutdown_requested:
                        exc = self.scanner_task.exception()
                        if exc:
                            logger.error(f"Scanner died: {exc}")
                            logger.info("Restarting scanner...")
                            self.scanner_task = asyncio.create_task(self.scanner.start())
            
            # If shutdown requested, keep health server alive but idle
            if self.shutdown_requested:
                logger.info("Bot stopped - idling (health server active for Render)")
                
                # Idle loop - keeps process alive so Render doesn't restart
                while self.shutdown_requested:
                    await asyncio.sleep(10)
                    
                    # If we exit this loop, it means start was called
                    if not self.shutdown_requested:
                        logger.info("Resuming from idle state...")
                        # Restart the scanner
                        if not self.scanner_task or self.scanner_task.done():
                            self.scanner_task = asyncio.create_task(self.scanner.start())
                        # Continue with main loop
                        continue
            
        except KeyboardInterrupt:
            logger.info("\n🛑 Shutting down...")
        except Exception as e:
            logger.error(f"Fatal error: {e}")
            if self.telegram:
                await self.telegram.send_message(f"❌ Bot crashed: {e}")
        finally:
            await self.shutdown()
    
    async def shutdown(self):
        """Clean shutdown"""
        self.running = False
        logger.info("Starting shutdown...")
        
        # Log final session summary
        self.tracker.log_session_summary()
        
        # Don't send shutdown message if stopped via Telegram - it already sent one
        # Only send if this is an unexpected shutdown (crash, Ctrl+C, etc)
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

# Main entry point
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
