"""
Main Orchestrator - Updated for DEXScreener + Jupiter
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
    STOP_LOSS_PERCENTAGE,
    SELL_DELAY_SECONDS, MAX_POSITION_AGE_SECONDS,
    MONITOR_CHECK_INTERVAL, DATA_FAILURE_TOLERANCE,
    DRY_RUN, ENABLE_TELEGRAM_NOTIFICATIONS,
    BLACKLISTED_TOKENS, NOTIFY_PROFIT_THRESHOLD,
    PARTIAL_TAKE_PROFIT, TRAILING_STOP_PERCENT, TRAILING_STOP_ACTIVATION
)

from wallet import WalletManager
from dexscreener_monitor import DexScreenerMonitor
from jupiter_swapper import JupiterSwapper
from performance_tracker import PerformanceTracker

# Configure logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format=LOG_FORMAT,
    handlers=[logging.StreamHandler()]
)

logger = logging.getLogger(__name__)

class Position:
    """Track an active position"""
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
        
        # DEX-specific fields
        self.pair_address = None
        self.dex_id = None
        self.entry_liquidity_usd = 0
        
        # Multi-target tracking
        self.partial_sells = {}
        self.pending_sells = set()
        self.pending_token_amounts = {}
        self.total_sold_percent = 0
        self.realized_pnl_sol = 0
        
        # Prevent multiple simultaneous closes
        self.is_closing = False
        
        # Retry tracking
        self.retry_counts = {}
        
        # Price tracking
        self.last_valid_price = 0
        self.last_price_update = time.time()
        self.highest_price = 0  # For trailing stop
        
        # Calculate entry price
        if tokens > 0:
            self.entry_token_price_sol = amount_sol / tokens
            self.entry_price = self.entry_token_price_sol
        else:
            self.entry_token_price_sol = 0
            self.entry_price = 0
        
        # Build profit targets
        self.profit_targets = []
        for multiplier, sell_fraction in sorted(PARTIAL_TAKE_PROFIT.items()):
            pnl_threshold = ((multiplier / 100) - 1) * 100
            self.profit_targets.append({
                'target': pnl_threshold,
                'sell_percent': sell_fraction * 100,
                'name': f'{int(multiplier/100)}x'
            })
        
        self.profit_targets.sort(key=lambda x: x['target'])
        
        if self.profit_targets:
            targets_str = ', '.join([f"{t['name']} at +{t['target']:.0f}% ({t['sell_percent']:.0f}%)" for t in self.profit_targets])
            logger.info(f"Profit targets: {targets_str}")

class SniperBot:
    """Main sniper bot - DEXScreener + Jupiter"""
    
    def __init__(self):
        """Initialize all components"""
        logger.info("=" * 60)
        logger.info("🚀 INITIALIZING SNIPER BOT - DEXSCREENER MODE")
        logger.info("=" * 60)
        
        # Core components
        self.wallet = WalletManager()
        self.scanner = None
        self.scanner_task = None
        self.telegram = None
        self.telegram_polling_task = None
        self.tracker = PerformanceTracker()
        
        # Initialize Jupiter swapper
        from solana.rpc.api import Client
        from config import RPC_ENDPOINT
        import config
        client = Client(RPC_ENDPOINT.replace('wss://', 'https://').replace('ws://', 'http://'))
        self.jupiter = JupiterSwapper(self.wallet, client, config)
        
        # Positions and stats
        self.positions: Dict[str, Position] = {}
        self.pending_buys = 0
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
        
        # Circuit breaker
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
        
        logger.info(f"📊 STARTUP STATUS - DEXSCREENER MODE:")
        logger.info(f"  • Strategy: Fresh DEX Pairs (Raydium/Meteora/Orca)")
        logger.info(f"  • Entry: $50k+ liquidity, <10min old, $5k+ volume")
        logger.info(f"  • Swaps: Jupiter v6 Aggregator")
        logger.info(f"  • Wallet: {self.wallet.pubkey}")
        logger.info(f"  • Balance: {sol_balance:.4f} SOL")
        logger.info(f"  • Max positions: {MAX_POSITIONS}")
        logger.info(f"  • Buy amount: {BUY_AMOUNT_SOL} SOL")
        logger.info(f"  • Stop loss: -{STOP_LOSS_PERCENTAGE}%")
        logger.info(f"  • Targets: 2x/3x/5x/10x")
        logger.info(f"  • Max hold: {MAX_POSITION_AGE_SECONDS}s ({MAX_POSITION_AGE_SECONDS//3600}h)")
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
                    "🚀 Bot started - DEXScreener Mode\n"
                    f"💰 Balance: {sol_balance:.4f} SOL\n"
                    f"🎯 Buy: {BUY_AMOUNT_SOL} SOL\n"
                    f"📈 Targets: 2x/3x/5x/10x\n"
                    f"💵 Entry: Fresh pairs, $50k+ LP\n"
                    f"⏱️ Max hold: {MAX_POSITION_AGE_SECONDS//3600}h\n"
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
            import config
            self.scanner = DexScreenerMonitor(config)
        
        if self.scanner_task and not self.scanner_task.done():
            self.scanner_task.cancel()
            try:
                await self.scanner_task
            except asyncio.CancelledError:
                pass
            await asyncio.sleep(0.1)
        
        self.scanner_task = asyncio.create_task(self.scanner.start(self.on_token_found))
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
    
    async def on_token_found(self, pair_data: Dict):
        """Handle new DEX pair found"""
        detection_start = time.time()
        
        try:
            mint = pair_data['token_address']
            
            # Validation checks
            if not self.running or self.paused:
                return
            
            if mint in BLACKLISTED_TOKENS:
                return
            
            total_positions = len(self.positions) + self.pending_buys
            
            if total_positions >= MAX_POSITIONS:
                logger.warning(f"Max positions reached ({total_positions}/{MAX_POSITIONS})")
                return
            
            if mint in self.positions:
                return
            
            if not self.wallet.can_trade():
                current_time = time.time()
                if current_time - self._last_balance_warning > 60:
                    logger.warning(f"Insufficient balance for trading")
                    self._last_balance_warning = current_time
                return
            
            # Circuit breaker
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
            
            self.pending_buys += 1
            logger.debug(f"Pending buys: {self.pending_buys}, Active: {len(self.positions)}")
            
            # Log detection
            detection_time_ms = (time.time() - detection_start) * 1000
            self.tracker.log_token_detection(mint, 'dexscreener', detection_time_ms)
            
            logger.info(f"🎯 Processing new DEX pair: {mint}")
            logger.info(f"   DEX: {pair_data['dex_id']}")
            logger.info(f"   Liquidity: ${pair_data['liquidity_usd']:,.0f}")
            logger.info(f"   Detection latency: {detection_time_ms:.0f}ms")
            
            cost_breakdown = self.tracker.log_buy_attempt(mint, BUY_AMOUNT_SOL, 50)
            
            # Execute buy via Jupiter
            execution_start = time.time()
            
            signature = await self.jupiter.buy_token(
                token_mint=mint,
                amount_sol=BUY_AMOUNT_SOL,
                slippage_bps=300  # 3% slippage
            )
            
            bought_tokens = 0
            if signature:
                await asyncio.sleep(3)
                bought_tokens = self.wallet.get_token_balance(mint)
            
            if signature and bought_tokens > 0:
                execution_time_ms = (time.time() - execution_start) * 1000
                
                self.tracker.log_buy_executed(
                    mint=mint,
                    amount_sol=BUY_AMOUNT_SOL,
                    signature=signature,
                    tokens_received=bought_tokens,
                    execution_time_ms=execution_time_ms
                )
                
                # Create position with DEX metadata
                position = Position(mint, BUY_AMOUNT_SOL, bought_tokens)
                position.buy_signature = signature
                position.pair_address = pair_data['pair_address']
                position.dex_id = pair_data['dex_id']
                position.entry_liquidity_usd = pair_data['liquidity_usd']
                
                self.positions[mint] = position
                self.total_trades += 1
                self.pending_buys -= 1
                
                logger.info(f"✅ BUY EXECUTED: {mint[:8]}...")
                logger.info(f"   Amount: {BUY_AMOUNT_SOL} SOL")
                logger.info(f"   Tokens: {bought_tokens:,.0f}")
                logger.info(f"   DEX: {pair_data['dex_id']}")
                logger.info(f"   Active positions: {len(self.positions)}/{MAX_POSITIONS}")
                
                if self.telegram:
                    await self.telegram.notify_buy(mint, BUY_AMOUNT_SOL, signature)
                
                # Start monitoring
                position.monitor_task = asyncio.create_task(self._monitor_position(mint))
            else:
                self.pending_buys -= 1
                self.tracker.log_buy_failed(mint, BUY_AMOUNT_SOL, "Transaction failed")
                
        except Exception as e:
            self.pending_buys = max(0, self.pending_buys - 1)
            logger.error(f"Failed to process pair: {e}")
            import traceback
            logger.error(traceback.format_exc())
            self.tracker.log_buy_failed(mint, BUY_AMOUNT_SOL, str(e))
    
    async def _monitor_position(self, mint: str):
        """Monitor position - simplified for DEX pairs"""
        try:
            position = self.positions.get(mint)
            if not position:
                return
            
            if SELL_DELAY_SECONDS > 0:
                logger.info(f"⏳ Grace period {SELL_DELAY_SECONDS}s for {mint[:8]}...")
                await asyncio.sleep(SELL_DELAY_SECONDS)
            
            logger.info(f"📈 Starting monitoring for {mint[:8]}...")
            
            check_count = 0
            
            while mint in self.positions and position.status == 'active':
                check_count += 1
                age = time.time() - position.entry_time
                
                # Check age limit
                if age > MAX_POSITION_AGE_SECONDS:
                    logger.warning(f"⏰ MAX AGE REACHED for {mint[:8]}... ({age:.0f}s)")
                    await self._close_position_full(mint, reason="max_age")
                    break
                
                try:
                    # Get current token balance
                    current_balance = self.wallet.get_token_balance(mint)
                    
                    if current_balance > 0:
                        # Calculate P&L based on balance change
                        # For DEX pairs, we estimate based on entry price
                        # This is simplified - in production you'd query DEXScreener for current price
                        
                        # Simple P&L estimate (placeholder - needs real price feed)
                        position.pnl_percent = 0  # TODO: Get real price from DEXScreener
                        
                        # Update highest price for trailing stop
                        if position.pnl_percent > position.highest_price:
                            position.highest_price = position.pnl_percent
                        
                        # Check trailing stop (after 2x)
                        if position.highest_price >= TRAILING_STOP_ACTIVATION:
                            trailing_trigger = position.highest_price - TRAILING_STOP_PERCENT
                            if position.pnl_percent <= trailing_trigger and not position.is_closing:
                                logger.warning(f"📉 TRAILING STOP for {mint[:8]}... (Peak: {position.highest_price:.1f}%)")
                                position.is_closing = True
                                await self._close_position_full(mint, reason="trailing_stop")
                                break
                        
                        # Check stop-loss
                        if position.pnl_percent <= -STOP_LOSS_PERCENTAGE and not position.is_closing:
                            logger.warning(f"🛑 STOP LOSS HIT for {mint[:8]}...")
                            position.is_closing = True
                            await self._close_position_full(mint, reason="stop_loss")
                            break
                        
                        if check_count % 10 == 1:
                            logger.info(f"📊 {mint[:8]}... | Age: {age:.0f}s | Balance: {current_balance:,.0f}")
                        
                        # Check profit targets
                        if not position.is_closing:
                            for target in position.profit_targets:
                                target_name = target['name']
                                target_pnl = target['target']
                                sell_percent = target['sell_percent']
                                
                                if (position.pnl_percent >= target_pnl and 
                                    target_name not in position.partial_sells and
                                    target_name not in position.pending_sells):
                                    
                                    logger.info(f"🎯 {target_name} TARGET HIT for {mint[:8]}...")
                                    
                                    position.pending_sells.add(target_name)
                                    
                                    try:
                                        success = await self._execute_partial_sell(
                                            mint, sell_percent, target_name, position.pnl_percent
                                        )
                                        if not success:
                                            position.pending_sells.discard(target_name)
                                        break
                                    except Exception as e:
                                        position.pending_sells.discard(target_name)
                                        logger.error(f"Sell execution error: {e}")
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
        """Execute a partial sell via Jupiter"""
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
                logger.warning(f"{target_name}: computed 0 tokens to sell; skipping")
                return False
            
            logger.info(f"💰 Executing {target_name} partial sell for {mint[:8]}...")
            logger.info(f"   Selling: {sell_percent}% ({ui_tokens_to_sell:,.2f} tokens)")
            
            pre_sol_balance = self.wallet.get_sol_balance()
            pre_token_balance = self.wallet.get_token_balance(mint)
            
            signature = await self.jupiter.sell_token(
                token_mint=mint,
                amount_tokens=ui_tokens_to_sell,
                slippage_bps=500  # 5% slippage
            )
            
            if signature and not signature.startswith("1111111"):
                asyncio.create_task(
                    self._confirm_sell_background(
                        signature, mint, target_name, sell_percent,
                        ui_tokens_to_sell, current_pnl,
                        pre_sol_balance, pre_token_balance
                    )
                )
                
                logger.info(f"✅ {target_name} sell submitted")
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
        """Confirm sell in background"""
        try:
            position = self.positions.get(mint)
            if not position:
                return
            
            logger.info(f"⏳ Confirming {target_name} sell for {mint[:8]}...")
            
            start = time.time()
            confirmed = False
            
            while time.time() - start < 25:
                try:
                    status = self.client.get_signature_statuses([signature])
                    if status and status.value and status.value[0]:
                        confirmation_status = status.value[0].confirmation_status
                        if confirmation_status in ["confirmed", "finalized"]:
                            if status.value[0].err:
                                logger.error(f"❌ {target_name} sell FAILED")
                                break
                            else:
                                confirmed = True
                                break
                except Exception as e:
                    logger.debug(f"Status check error: {e}")
                
                await asyncio.sleep(1)
            
            if confirmed:
                await asyncio.sleep(2)
                post_sol_balance = self.wallet.get_sol_balance()
                actual_sol_received = post_sol_balance - pre_sol_balance
                
                current_token_balance = self.wallet.get_token_balance(mint)
                actual_tokens_sold = max(0.0, pre_token_balance - current_token_balance)
                position.remaining_tokens = max(0.0, current_token_balance)
                
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
                
                self.consecutive_losses = 0
                
                logger.info(f"✅ {target_name} CONFIRMED for {mint[:8]}")
                logger.info(f"   Received: {actual_sol_received:.4f} SOL")
                logger.info(f"   Profit: {actual_profit_sol:+.4f} SOL")
                
                if position.total_sold_percent >= 100:
                    position.status = 'completed'
            else:
                logger.warning(f"❌ {target_name} sell timeout for {mint[:8]}")
                if target_name in position.pending_sells:
                    position.pending_sells.remove(target_name)
                
        except Exception as e:
            logger.error(f"Confirmation error: {e}")
    
    async def _close_position_full(self, mint: str, reason: str = "manual"):
        """Close remaining position via Jupiter"""
        try:
            position = self.positions.get(mint)
            if not position or position.status != 'active':
                return
            
            if position.is_closing:
                return
            
            position.is_closing = True
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
            
            logger.info(f"📤 Closing remaining {remaining_percent}% of {mint[:8]}...")
            
            pre_sol_balance = self.wallet.get_sol_balance()
            actual_balance = self.wallet.get_token_balance(mint)
            if actual_balance > 0:
                ui_token_balance = actual_balance
            
            signature = await self.jupiter.sell_token(
                token_mint=mint,
                amount_tokens=ui_token_balance,
                slippage_bps=1000  # 10% slippage for full close
            )
            
            if signature and not signature.startswith("1111111"):
                await asyncio.sleep(3)
                
                post_sol_balance = self.wallet.get_sol_balance()
                actual_sol_received = post_sol_balance - pre_sol_balance
                
                base_sol_for_portion = position.amount_sol * (remaining_percent / 100)
                final_pnl_sol = actual_sol_received - base_sol_for_portion
                
                position.sell_signatures.append(signature)
                position.status = 'closed'
                
                if position.pnl_percent > 0:
                    self.profitable_trades += 1
                    self.consecutive_losses = 0
                else:
                    self.consecutive_losses += 1
                    self.session_loss_count += 1
                
                position.realized_pnl_sol += final_pnl_sol
                self.total_realized_sol += final_pnl_sol
                
                logger.info(f"✅ POSITION CLOSED: {mint[:8]}...")
                logger.info(f"   Reason: {reason}")
                logger.info(f"   Realized: {position.realized_pnl_sol:+.4f} SOL")
                
                if self.telegram:
                    emoji = "💰" if position.realized_pnl_sol > 0 else "🔴"
                    msg = (
                        f"{emoji} POSITION CLOSED\n"
                        f"Token: {mint[:16]}\n"
                        f"Reason: {reason}\n"
                        f"Realized: {position.realized_pnl_sol:+.4f} SOL"
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
            await self.start_scanner()
            
            logger.info("✅ Bot running - DEXScreener Mode")
            
            last_stats_time = time.time()
            
            while self.running and not self.shutdown_requested:
                await asyncio.sleep(10)
                
                # Periodic stats
                if time.time() - last_stats_time > 60:
                    if self.positions:
                        logger.info(f"📊 ACTIVE POSITIONS: {len(self.positions)}")
                        for mint, pos in self.positions.items():
                            age = time.time() - pos.entry_time
                            logger.info(f"  • {mint[:8]}... | Age: {age:.0f}s")
                    
                    last_stats_time = time.time()
                
                # Check scanner health
                if self.scanner_task and self.scanner_task.done():
                    if not self.shutdown_requested:
                        exc = self.scanner_task.exception()
                        if exc:
                            logger.error(f"Scanner died: {exc}")
                            logger.info("Restarting scanner...")
                            self.scanner_task = asyncio.create_task(self.scanner.start(self.on_token_found))
            
            # Idle if shutdown requested
            if self.shutdown_requested:
                logger.info("Bot stopped - idling")
                while self.shutdown_requested:
                    await asyncio.sleep(10)
                    if not self.shutdown_requested:
                        logger.info("Resuming from idle...")
                        if not self.scanner_task or self.scanner_task.done():
                            self.scanner_task = asyncio.create_task(self.scanner.start(self.on_token_found))
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
        
        if self.scanner_task and not self.scanner_task.done():
            self.scanner_task.cancel()
        
        if self.scanner:
            self.scanner.stop()
        
        if self.positions:
            logger.info(f"Closing {len(self.positions)} positions...")
            for mint in list(self.positions.keys()):
                await self._close_position_full(mint, reason="shutdown")
        
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
