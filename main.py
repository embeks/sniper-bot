"""
Main Orchestrator - Raydium Graduation Strategy
"""

import asyncio
import logging
import signal
import time
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
    PARTIAL_TAKE_PROFIT, GRADUATION_BUY_AMOUNT
)

from wallet import WalletManager
from raydium_graduation_monitor import RadiumGraduationMonitor
from jupiter_trader import JupiterTrader
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
        
        # Price tracking
        self.last_valid_price = 0
        self.last_price_update = time.time()
        self.consecutive_stale_reads = 0
        self.last_valid_balance = tokens
        
        # Graduation specific - store entry price in USD
        self.entry_price_usd = 0
        
        # Build profit targets from environment
        self.profit_targets = []
        
        # Check configured targets (2x, 3x, 5x only)
        if 200.0 in PARTIAL_TAKE_PROFIT:
            self.profit_targets.append({
                'target': 100,  # 100% gain = 2x
                'sell_percent': PARTIAL_TAKE_PROFIT[200.0] * 100,
                'name': '2x'
            })
        
        if 300.0 in PARTIAL_TAKE_PROFIT:
            self.profit_targets.append({
                'target': 200,  # 200% gain = 3x
                'sell_percent': PARTIAL_TAKE_PROFIT[300.0] * 100,
                'name': '3x'
            })
        
        if 500.0 in PARTIAL_TAKE_PROFIT:
            self.profit_targets.append({
                'target': 400,  # 400% gain = 5x
                'sell_percent': PARTIAL_TAKE_PROFIT[500.0] * 100,
                'name': '5x'
            })
        
        # Sort targets by percentage (ascending)
        self.profit_targets.sort(key=lambda x: x['target'])

class SniperBot:
    """Main sniper bot orchestrator for Raydium graduations"""
    
    def __init__(self):
        """Initialize all components"""
        logger.info("=" * 60)
        logger.info("🎓 INITIALIZING GRADUATION SNIPER - RAYDIUM STRATEGY")
        logger.info("=" * 60)
        
        # Core components
        self.wallet = WalletManager()
        self.scanner = None
        self.scanner_task = None
        self.telegram = None
        self.telegram_polling_task = None
        self.tracker = PerformanceTracker()
        
        # Initialize Jupiter trader
        from solana.rpc.api import Client
        from config import RPC_ENDPOINT
        client = Client(RPC_ENDPOINT.replace('wss://', 'https://').replace('ws://', 'http://'))
        self.trader = JupiterTrader(self.wallet, client)
        
        # Positions and stats
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
        
        # Track consecutive losses for circuit breaker
        self.consecutive_losses = 0
        self.session_loss_count = 0
        
        self.telegram_enabled = ENABLE_TELEGRAM_NOTIFICATIONS
        
        self._log_startup_info()
    
    def _log_startup_info(self):
        """Log startup information"""
        sol_balance = self.wallet.get_sol_balance()
        tradeable_balance = max(0, sol_balance - MIN_SOL_BALANCE)
        max_trades = int(tradeable_balance / GRADUATION_BUY_AMOUNT) if tradeable_balance > 0 else 0
        actual_trades = min(max_trades, MAX_POSITIONS) if max_trades > 0 else 0
        
        logger.info(f"📊 STARTUP STATUS - GRADUATION STRATEGY:")
        logger.info(f"  • Wallet: {self.wallet.pubkey}")
        logger.info(f"  • Balance: {sol_balance:.4f} SOL")
        logger.info(f"  • Strategy: Catch PumpFun → Raydium graduations")
        logger.info(f"  • Entry at: $69k market cap")
        logger.info(f"  • Max positions: {MAX_POSITIONS}")
        logger.info(f"  • Buy amount: {GRADUATION_BUY_AMOUNT} SOL")
        logger.info(f"  • Stop loss: -{STOP_LOSS_PERCENTAGE}%")
        logger.info(f"  • Targets: 2x/3x/5x")
        logger.info(f"  • Available trades: {actual_trades}")
        logger.info(f"  • Mode: {'DRY RUN' if DRY_RUN else 'LIVE TRADING'}")
        logger.info("=" * 60)
    
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
                    "🎓 Graduation Bot Started\n"
                    f"💰 Balance: {sol_balance:.4f} SOL\n"
                    f"🎯 Strategy: Raydium Graduations\n"
                    f"📈 Entry: $69k market cap\n"
                    f"🎯 Targets: 2x/3x/5x\n"
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
        
        # Reset circuit breaker on fresh start
        self.consecutive_losses = 0
        
        if not self.scanner:
            self.scanner = RadiumGraduationMonitor(self.on_graduation_detected)
        
        if self.scanner_task and not self.scanner_task.done():
            self.scanner_task.cancel()
            try:
                await self.scanner_task
            except asyncio.CancelledError:
                pass
            await asyncio.sleep(0.1)
        
        self.scanner_task = asyncio.create_task(self.scanner.start())
        logger.info("✅ Graduation scanner started")
    
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
    
    async def on_graduation_detected(self, graduation_data: Dict):
        """Handle graduation detection - buy graduated tokens"""
        detection_start = time.time()
        
        try:
            mint = graduation_data['mint']
            
            # Validation checks
            if not self.running or self.paused:
                return
            
            if mint in BLACKLISTED_TOKENS:
                return
            
            if len(self.positions) >= MAX_POSITIONS:
                logger.warning(f"Max positions reached ({MAX_POSITIONS})")
                return
            
            if mint in self.positions:
                return
            
            if not self.wallet.can_trade():
                current_time = time.time()
                if current_time - self._last_balance_warning > 60:
                    logger.warning(f"Insufficient balance for trading")
                    self._last_balance_warning = current_time
                return
            
            # Circuit breaker check
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
            
            # Log detection
            detection_time_ms = (time.time() - detection_start) * 1000
            self.tracker.log_token_detection(mint, 'raydium_graduation', detection_time_ms)
            
            logger.info(f"🎓 Processing graduation: {mint}")
            logger.info(f"   Detection latency: {detection_time_ms:.0f}ms")
            logger.info(f"   Initial liquidity: ${graduation_data.get('initial_liquidity_usd', 69000):,.0f}")
            
            cost_breakdown = self.tracker.log_buy_attempt(mint, GRADUATION_BUY_AMOUNT, 100)
            
            # Execute buy via Jupiter
            execution_start = time.time()
            
            if DRY_RUN:
                logger.info(f"[DRY RUN] Would buy {mint[:8]}... for {GRADUATION_BUY_AMOUNT} SOL")
                signature = f"dry_run_buy_{mint[:10]}"
                bought_tokens = 1000000
            else:
                signature = await self.trader.create_buy_transaction(
                    mint=mint,
                    sol_amount=GRADUATION_BUY_AMOUNT,
                    slippage=100  # 10% slippage for graduations
                )
                
                bought_tokens = 0
                if signature:
                    await asyncio.sleep(3)  # Wait for confirmation
                    bought_tokens = self.wallet.get_token_balance(mint)
                    if bought_tokens == 0:
                        # Estimate based on $69k market cap
                        bought_tokens = GRADUATION_BUY_AMOUNT * 250 / 0.000069  # Rough estimate
            
            if signature:
                execution_time_ms = (time.time() - execution_start) * 1000
                
                self.tracker.log_buy_executed(
                    mint=mint,
                    amount_sol=GRADUATION_BUY_AMOUNT,
                    signature=signature,
                    tokens_received=bought_tokens,
                    execution_time_ms=execution_time_ms
                )
                
                # Create position
                position = Position(mint, GRADUATION_BUY_AMOUNT, bought_tokens)
                position.buy_signature = signature
                position.initial_tokens = bought_tokens
                position.remaining_tokens = bought_tokens
                position.last_valid_balance = bought_tokens
                position.entry_time = time.time()
                position.entry_price_usd = 0.000069  # $69k market cap entry
                
                self.positions[mint] = position
                self.total_trades += 1
                
                logger.info(f"✅ GRADUATION BUY EXECUTED: {mint[:8]}...")
                logger.info(f"   Amount: {GRADUATION_BUY_AMOUNT} SOL")
                logger.info(f"   Tokens: {bought_tokens:,.0f}")
                logger.info(f"   Entry MC: $69,000")
                logger.info(f"   Active positions: {len(self.positions)}/{MAX_POSITIONS}")
                
                if self.telegram:
                    await self.telegram.notify_buy(mint, GRADUATION_BUY_AMOUNT, signature)
                
                # Start monitoring
                position.monitor_task = asyncio.create_task(self._monitor_position(mint))
                logger.info(f"📊 Started monitoring graduated position {mint[:8]}...")
            else:
                self.tracker.log_buy_failed(mint, GRADUATION_BUY_AMOUNT, "Transaction failed")
                
        except Exception as e:
            logger.error(f"Failed to process graduation: {e}")
            self.tracker.log_buy_failed(mint, GRADUATION_BUY_AMOUNT, str(e))
    
    async def _monitor_position(self, mint: str):
        """Monitor position with Jupiter price feeds"""
        try:
            position = self.positions.get(mint)
            if not position:
                return
            
            # Grace period
            logger.info(f"⏳ Grace period {SELL_DELAY_SECONDS}s for {mint[:8]}...")
            await asyncio.sleep(SELL_DELAY_SECONDS)
            
            logger.info(f"📈 Starting active monitoring for graduated token {mint[:8]}...")
            
            check_count = 0
            last_notification_pnl = 0
            consecutive_data_failures = 0
            
            while mint in self.positions and position.status == 'active':
                check_count += 1
                age = time.time() - position.entry_time
                
                # Check age limit
                if age > MAX_POSITION_AGE_SECONDS:
                    logger.warning(f"⏰ MAX AGE REACHED for {mint[:8]}... ({age:.0f}s)")
                    await self._close_position_full(mint, reason="max_age")
                    break
                
                try:
                    # Get current price from Jupiter
                    price_data = await self.trader.get_price(mint)
                    
                    if price_data:
                        current_price = price_data['price']
                        
                        # Calculate P&L based on price change
                        if position.entry_price_usd > 0:
                            price_change = ((current_price / position.entry_price_usd) - 1) * 100
                        else:
                            # Assume entry at $0.000069 per token (69k market cap)
                            price_change = ((current_price / 0.000069) - 1) * 100
                        
                        position.pnl_percent = price_change
                        position.current_price = current_price
                        
                        consecutive_data_failures = 0
                        position.last_valid_price = current_price
                        position.last_price_update = time.time()
                        
                        if check_count % 10 == 1:
                            self.tracker.log_position_update(mint, price_change, current_price, age)
                        
                        if check_count % 3 == 1:
                            logger.info(
                                f"📊 {mint[:8]}... | P&L: {price_change:+.1f}% | "
                                f"Price: ${current_price:.8f} | "
                                f"Sold: {position.total_sold_percent}% | Age: {age:.0f}s"
                            )
                        
                        # Telegram updates
                        if self.telegram and abs(price_change - last_notification_pnl) >= 50:
                            update_msg = (
                                f"🎓 Graduation Update {mint[:8]}...\n"
                                f"P&L: {price_change:+.1f}%\n"
                                f"Price: ${current_price:.8f}\n"
                                f"Remaining: {100 - position.total_sold_percent}%"
                            )
                            await self.telegram.send_message(update_msg)
                            last_notification_pnl = price_change
                        
                        # Check stop loss FIRST
                        if price_change <= -STOP_LOSS_PERCENTAGE and position.total_sold_percent < 100:
                            logger.warning(f"🛑 STOP LOSS HIT for {mint[:8]}...")
                            await self._close_position_full(mint, reason="stop_loss")
                            break
                        
                        # Then check profit targets
                        for target in position.profit_targets:
                            target_name = target['name']
                            target_pnl = target['target']
                            sell_percent = target['sell_percent']
                            
                            if price_change >= target_pnl and target_name not in position.partial_sells:
                                logger.info(f"🎯 {target_name} TARGET HIT for graduated token {mint[:8]}...")
                                
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
                                    
                                    # Reset consecutive losses on profit
                                    self.consecutive_losses = 0
                                    
                                    if position.total_sold_percent >= 100:
                                        logger.info(f"✅ Position fully closed")
                                        position.status = 'completed'
                                        break
                    else:
                        consecutive_data_failures += 1
                        if consecutive_data_failures > DATA_FAILURE_TOLERANCE:
                            logger.warning(f"Failed to get price for {mint[:8]}... - using last valid")
                            if position.last_valid_price == 0:
                                logger.error(f"No valid price data for {mint[:8]}...")
                                await self._close_position_full(mint, reason="no_price_data")
                                break
                
                except Exception as e:
                    logger.error(f"Error checking {mint[:8]}...: {e}")
                
                await asyncio.sleep(MONITOR_CHECK_INTERVAL)
            
            # Clean up
            if mint in self.positions and position.status == 'completed':
                del self.positions[mint]
                logger.info(f"Position {mint[:8]}... removed after completion")
                
        except Exception as e:
            logger.error(f"Monitor error for {mint[:8]}...: {e}")
            if mint in self.positions:
                await self._close_position_full(mint, reason="monitor_error")
    
    async def _execute_partial_sell(self, mint: str, sell_percent: float, target_name: str, current_pnl: float) -> bool:
        """Execute a partial sell"""
        try:
            position = self.positions.get(mint)
            if not position:
                return False
            
            current_balance = self.wallet.get_token_balance(mint)
            
            if current_balance == 0:
                logger.warning(f"Using recorded balance {position.last_valid_balance}")
                current_balance = position.last_valid_balance
            elif current_balance > position.initial_tokens * 2:
                logger.warning(f"Suspicious balance, using recorded {position.last_valid_balance}")
                current_balance = position.last_valid_balance
            else:
                position.last_valid_balance = current_balance
            
            if current_balance <= 0:
                return False
            
            remaining_balance = position.remaining_tokens
            ui_tokens_to_sell = remaining_balance * (sell_percent / 100)
            
            logger.info(f"💰 Executing {target_name} partial sell for graduated token {mint[:8]}...")
            logger.info(f"   Selling: {sell_percent}% ({ui_tokens_to_sell:,.2f} tokens)")
            
            # Calculate expected SOL based on target
            base_sol_for_portion = GRADUATION_BUY_AMOUNT * (sell_percent / 100)
            
            if target_name == '2x':
                multiplier = 2.0
            elif target_name == '3x':
                multiplier = 3.0
            elif target_name == '5x':
                multiplier = 5.0
            else:
                multiplier = 1 + (current_pnl / 100)
            
            sol_received = base_sol_for_portion * multiplier
            profit_sol = sol_received - base_sol_for_portion
            
            if DRY_RUN:
                logger.info(f"[DRY RUN] Would sell {ui_tokens_to_sell:,.2f} tokens")
                signature = f"dry_run_sell_{target_name}_{mint[:10]}"
            else:
                token_decimals = self.wallet.get_token_decimals(mint)
                
                signature = await self.trader.create_sell_transaction(
                    mint=mint,
                    token_amount=ui_tokens_to_sell,
                    slippage=100,
                    token_decimals=token_decimals
                )
            
            if signature and not signature.startswith("1111111"):
                position.sell_signatures.append(signature)
                position.remaining_tokens -= ui_tokens_to_sell
                position.realized_pnl_sol += profit_sol
                self.total_realized_sol += profit_sol
                
                self.tracker.log_partial_sell(
                    mint=mint,
                    target_name=target_name,
                    percent_sold=sell_percent,
                    tokens_sold=ui_tokens_to_sell,
                    sol_received=sol_received,
                    pnl_sol=profit_sol
                )
                
                logger.info(f"✅ {target_name} SELL EXECUTED for graduation")
                logger.info(f"   Est. received: {sol_received:.4f} SOL")
                logger.info(f"   Est. profit: {profit_sol:+.4f} SOL")
                
                if self.telegram:
                    msg = (
                        f"🎓 {target_name} TARGET HIT! (Graduation)\n"
                        f"Token: {mint[:16]}...\n"
                        f"Sold: {sell_percent}% of position\n"
                        f"P&L: {current_pnl:+.1f}%\n"
                        f"Est. profit: {profit_sol:+.4f} SOL\n"
                        f"TX: https://solscan.io/tx/{signature}"
                    )
                    await self.telegram.send_message(msg)
                
                return True
            else:
                logger.error(f"Failed to execute {target_name} sell")
                return False
                
        except Exception as e:
            logger.error(f"Partial sell error: {e}")
            return False
    
    async def _close_position_full(self, mint: str, reason: str = "manual"):
        """Close remaining position"""
        try:
            position = self.positions.get(mint)
            if not position or position.status != 'active':
                return
            
            position.status = 'closing'
            ui_token_balance = position.remaining_tokens
            
            if ui_token_balance <= 0:
                logger.warning(f"No tokens remaining for {mint[:8]}...")
                position.status = 'closed'
                if mint in self.positions:
                    del self.positions[mint]
                return
            
            remaining_percent = 100 - position.total_sold_percent
            hold_time = time.time() - position.entry_time
            
            logger.info(f"📤 Closing remaining {remaining_percent}% of graduated position {mint[:8]}...")
            
            # Calculate expected SOL
            base_sol_for_portion = GRADUATION_BUY_AMOUNT * (remaining_percent / 100)
            
            if position.pnl_percent > 0:
                multiplier = min(1 + (position.pnl_percent / 100), 5.0)
            else:
                multiplier = max(1 + (position.pnl_percent / 100), 0.5)
            
            sol_received = base_sol_for_portion * multiplier
            final_pnl = sol_received - base_sol_for_portion
            
            if DRY_RUN:
                logger.info(f"[DRY RUN] Would sell {ui_token_balance:,.2f} tokens")
                signature = f"dry_run_close_{mint[:10]}"
            else:
                actual_balance = self.wallet.get_token_balance(mint)
                if actual_balance > 0:
                    ui_token_balance = actual_balance
                
                token_decimals = self.wallet.get_token_decimals(mint)
                
                signature = await self.trader.create_sell_transaction(
                    mint=mint,
                    token_amount=ui_token_balance,
                    slippage=100,
                    token_decimals=token_decimals
                )
            
            if signature and not signature.startswith("1111111"):
                position.sell_signatures.append(signature)
                position.status = 'closed'
                
                if position.pnl_percent > 0:
                    self.profitable_trades += 1
                    self.consecutive_losses = 0
                else:
                    self.consecutive_losses += 1
                    self.session_loss_count += 1
                    
                self.total_pnl += position.pnl_percent
                
                position.realized_pnl_sol += final_pnl
                self.total_realized_sol += final_pnl
                
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
                
                logger.info(f"✅ GRADUATION POSITION CLOSED: {mint[:8]}...")
                logger.info(f"   Reason: {reason}")
                logger.info(f"   P&L: {position.pnl_percent:+.1f}%")
                logger.info(f"   Est. realized: {position.realized_pnl_sol:+.4f} SOL")
                
                if self.telegram:
                    emoji = "💰" if position.realized_pnl_sol > 0 else "🔴"
                    msg = (
                        f"{emoji} GRADUATION CLOSED\n"
                        f"Token: {mint[:16]}\n"
                        f"Reason: {reason}\n"
                        f"P&L: {position.pnl_percent:+.1f}%\n"
                        f"Est. realized: {position.realized_pnl_sol:+.4f} SOL"
                    )
                    await self.telegram.send_message(msg)
            else:
                logger.error(f"❌ Close transaction failed")
                position.status = 'close_failed'
            
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
            
            self.scanner = RadiumGraduationMonitor(self.on_graduation_detected)
            self.scanner_task = asyncio.create_task(self.scanner.start())
            
            logger.info("✅ Graduation bot running - Raydium Strategy")
            logger.info(f"🎓 Monitoring for PumpFun → Raydium graduations at $69k")
            logger.info(f"⏱️ Grace period: {SELL_DELAY_SECONDS}s, Max hold: {MAX_POSITION_AGE_SECONDS}s")
            
            last_stats_time = time.time()
            
            while self.running and not self.shutdown_requested:
                await asyncio.sleep(10)
                
                # Periodic stats
                if time.time() - last_stats_time > 60:
                    if self.positions:
                        logger.info(f"📊 ACTIVE GRADUATED POSITIONS: {len(self.positions)}")
                        for mint, pos in self.positions.items():
                            age = time.time() - pos.entry_time
                            targets_hit = ', '.join(pos.partial_sells.keys()) if pos.partial_sells else 'None'
                            logger.info(
                                f"  • {mint[:8]}... | P&L: {pos.pnl_percent:+.1f}% | "
                                f"Sold: {pos.total_sold_percent}% | Targets: {targets_hit} | "
                                f"Age: {age:.0f}s"
                            )
                    
                    # Get scanner stats
                    if self.scanner:
                        scanner_stats = self.scanner.get_stats()
                        logger.info(f"🎓 GRADUATION STATS:")
                        logger.info(f"  • Detected: {scanner_stats.get('graduations_detected', 0)}")
                        logger.info(f"  • Processed: {scanner_stats.get('graduations_processed', 0)}")
                    
                    perf_stats = self.tracker.get_session_stats()
                    if perf_stats['total_buys'] > 0:
                        logger.info(f"📊 SESSION PERFORMANCE:")
                        logger.info(f"  • Trades: {perf_stats['total_buys']} buys, {perf_stats['total_sells']} sells")
                        logger.info(f"  • Win rate: {perf_stats['win_rate_percent']:.1f}%")
                        logger.info(f"  • P&L: {perf_stats['total_pnl_sol']:+.4f} SOL")
                    
                    if self.total_realized_sol != 0:
                        logger.info(f"💰 Total realized: {self.total_realized_sol:+.4f} SOL")
                    
                    last_stats_time = time.time()
                
                # Check scanner health
                if self.scanner_task and self.scanner_task.done():
                    if not self.shutdown_requested:
                        exc = self.scanner_task.exception()
                        if exc:
                            logger.error(f"Scanner died: {exc}")
                            logger.info("Restarting scanner...")
                            self.scanner_task = asyncio.create_task(self.scanner.start())
            
            # Idle if shutdown requested
            if self.shutdown_requested:
                logger.info("Bot stopped - idling")
                while self.shutdown_requested:
                    await asyncio.sleep(10)
                    if not self.shutdown_requested:
                        logger.info("Resuming from idle...")
                        if not self.scanner_task or self.scanner_task.done():
                            self.scanner_task = asyncio.create_task(self.scanner.start())
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
        
        self.tracker.log_session_summary()
        
        if self.telegram and not self.shutdown_requested:
            await self.telegram.send_message(
                f"🛑 Bot shutting down\n"
                f"Total realized: {self.total_realized_sol:+.4f} SOL\n"
                f"Session losses: {self.session_loss_count}"
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
            logger.info(f"  • Session losses: {self.session_loss_count}")
        
        logger.info("✅ Shutdown complete")

# Main entry point
if __name__ == "__main__":
    import os
    from aiohttp import web
    
    port = int(os.getenv("PORT", "10000"))
    
    async def health_handler(request):
        return web.Response(text="Graduation Bot is running", status=200)
    
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
