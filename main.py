"""
Main Orchestrator - Path B: MC-Based Entry & FIXED P&L Tracking
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
    """Track an active position with CORRECT P&L calculation"""
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
        
        # Multi-target tracking
        self.partial_sells = {}
        self.total_sold_percent = 0
        self.realized_pnl_sol = 0
        
        # Price tracking
        self.last_valid_price = 0
        self.last_price_update = time.time()
        self.consecutive_stale_reads = 0
        self.last_valid_balance = tokens
        self.curve_check_retries = 0
        
        # MC-based tracking for Path B
        self.entry_market_cap = entry_market_cap
        self.current_market_cap = entry_market_cap
        self.entry_sol_in_curve = 0
        
        # CRITICAL: Store entry token price for accurate P&L
        self.entry_token_price_sol = 0
        if entry_market_cap > 0 and tokens > 0:
            # Calculate entry price per token
            total_supply = 1_000_000_000
            sol_price_usd = 250
            self.entry_token_price_sol = (entry_market_cap / sol_price_usd) / total_supply
        
        # Build profit targets from environment
        self.profit_targets = []
        
        if 200.0 in PARTIAL_TAKE_PROFIT:
            self.profit_targets.append({
                'target': 100,
                'sell_percent': PARTIAL_TAKE_PROFIT[200.0] * 100,
                'name': '2x'
            })
        
        if 300.0 in PARTIAL_TAKE_PROFIT:
            self.profit_targets.append({
                'target': 200,
                'sell_percent': PARTIAL_TAKE_PROFIT[300.0] * 100,
                'name': '3x'
            })
        
        if 500.0 in PARTIAL_TAKE_PROFIT:
            self.profit_targets.append({
                'target': 400,
                'sell_percent': PARTIAL_TAKE_PROFIT[500.0] * 100,
                'name': '5x'
            })
        
        self.profit_targets.sort(key=lambda x: x['target'])

class SniperBot:
    """Main sniper bot orchestrator - Path B Strategy with FIXED P&L"""
    
    def __init__(self):
        """Initialize all components"""
        logger.info("=" * 60)
        logger.info("🚀 INITIALIZING SNIPER BOT - PATH B: FIXED P&L VERSION")
        logger.info("=" * 60)
        
        # Core components
        self.wallet = WalletManager()
        self.dex = PumpFunDEX(self.wallet)
        self.scanner = None
        self.scanner_task = None
        self.telegram = None
        self.telegram_polling_task = None
        self.tracker = PerformanceTracker()
        
        # Initialize trader
        from solana.rpc.api import Client
        from config import RPC_ENDPOINT
        client = Client(RPC_ENDPOINT.replace('wss://', 'https://').replace('ws://', 'http://'))
        self.trader = PumpPortalTrader(self.wallet, client)
        
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
        
        logger.info(f"📊 STARTUP STATUS - PATH B (FIXED P&L):")
        logger.info(f"  • Strategy: MC + Holder Verification")
        logger.info(f"  • Entry: $6k-$60k MC, 5+ holders (concentration check disabled)")
        logger.info(f"  • Wallet: {self.wallet.pubkey}")
        logger.info(f"  • Balance: {sol_balance:.4f} SOL")
        logger.info(f"  • Max positions: {MAX_POSITIONS}")
        logger.info(f"  • Buy amount: {BUY_AMOUNT_SOL} SOL")
        logger.info(f"  • Stop loss: -{STOP_LOSS_PERCENTAGE}%")
        logger.info(f"  • Targets: 2x/3x/5x")
        logger.info(f"  • Available trades: {actual_trades}")
        logger.info(f"  • Circuit breaker: 3 consecutive losses")
        logger.info(f"  • Mode: {'DRY RUN' if DRY_RUN else 'LIVE TRADING'}")
        logger.info("=" * 60)
    
    def _calculate_mc_from_curve(self, curve_data: dict, sol_price_usd: float = 250) -> float:
        """Calculate market cap from bonding curve data for logging purposes"""
        try:
            # Get price directly from curve data (already calculated by dex.py)
            price_per_token_lamports = curve_data.get('price_per_token', 0)
            
            if price_per_token_lamports == 0:
                return 0
            
            # Convert to SOL (1 SOL = 1e9 lamports)
            price_per_token_sol = price_per_token_lamports / 1e9
            
            # Total supply (PumpFun standard is 1 billion tokens)
            total_supply = 1_000_000_000
            
            # Market cap = total supply * price per token * SOL price in USD
            market_cap_usd = total_supply * price_per_token_sol * sol_price_usd
            
            return market_cap_usd
        except Exception as e:
            logger.error(f"MC calculation error: {e}")
            return 0
    
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
                    "🚀 Bot started - PATH B (FIXED P&L)\n"
                    f"💰 Balance: {sol_balance:.4f} SOL\n"
                    f"🎯 Buy: {BUY_AMOUNT_SOL} SOL\n"
                    f"📈 Targets: 2x/3x/5x\n"
                    f"💵 Entry: $6k-$60k MC\n"
                    f"👥 Min holders: 5\n"
                    f"🛑 Circuit breaker: 3 losses\n"
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
        """Handle new token found - PATH B with MC data"""
        detection_start = time.time()
        
        try:
            mint = token_data['mint']
            
            # Update DEX with WebSocket data
            self.dex.update_token_data(mint, token_data)
            
            # Update existing position prices
            if mint in self.positions:
                self.dex.update_token_data(mint, token_data)
                logger.debug(f"Updated price data for existing position {mint[:8]}...")
            
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
            
            # Quality filters
            initial_buy = token_data.get('data', {}).get('solAmount', 0) if 'data' in token_data else token_data.get('solAmount', 0)
            name = token_data.get('data', {}).get('name', '') if 'data' in token_data else token_data.get('name', '')
            
            if initial_buy < 0.1 or initial_buy > 10:
                return
            
            if len(name) < 3 or 'test' in name.lower():
                return
            
            # Extract entry market cap from token_data
            entry_market_cap = token_data.get('market_cap', 0)
            
            # Log detection
            detection_time_ms = (time.time() - detection_start) * 1000
            self.tracker.log_token_detection(mint, token_data.get('source', 'pumpportal'), detection_time_ms)
            
            logger.info(f"🎯 Processing new token: {mint}")
            logger.info(f"   Detection latency: {detection_time_ms:.0f}ms")
            logger.info(f"   Entry MC: ${entry_market_cap:,.0f}")
            
            cost_breakdown = self.tracker.log_buy_attempt(mint, BUY_AMOUNT_SOL, 50)
            
            # Execute buy
            execution_start = time.time()
            
            if DRY_RUN:
                logger.info(f"[DRY RUN] Would buy {mint[:8]}... for {BUY_AMOUNT_SOL} SOL")
                signature = f"dry_run_buy_{mint[:10]}"
                bought_tokens = 1000000
            else:
                bonding_curve_key = None
                if 'data' in token_data and 'bondingCurveKey' in token_data['data']:
                    bonding_curve_key = token_data['data']['bondingCurveKey']
                
                expected_tokens = 0
                if 'data' in token_data:
                    data = token_data['data']
                    if 'initialBuy' in data and 'solAmount' in data:
                        creator_sol = float(data.get('solAmount', 0.01))
                        if creator_sol > 0:
                            expected_tokens = float(data.get('initialBuy', 0)) * (BUY_AMOUNT_SOL / creator_sol)
                
                signature = await self.trader.create_buy_transaction(
                    mint=mint,
                    sol_amount=BUY_AMOUNT_SOL,
                    bonding_curve_key=bonding_curve_key,
                    slippage=50
                )
                
                bought_tokens = 0
                if signature:
                    await asyncio.sleep(2)
                    bought_tokens = self.wallet.get_token_balance(mint)
                    if bought_tokens == 0:
                        bought_tokens = expected_tokens if expected_tokens > 0 else 350000
            
            if signature:
                execution_time_ms = (time.time() - execution_start) * 1000
                
                self.tracker.log_buy_executed(
                    mint=mint,
                    amount_sol=BUY_AMOUNT_SOL,
                    signature=signature,
                    tokens_received=bought_tokens,
                    execution_time_ms=execution_time_ms
                )
                
                # Create position with entry MC
                position = Position(mint, BUY_AMOUNT_SOL, bought_tokens, entry_market_cap)
                position.buy_signature = signature
                position.initial_tokens = bought_tokens
                position.remaining_tokens = bought_tokens
                position.last_valid_balance = bought_tokens
                position.entry_time = time.time()
                
                # Store entry SOL in curve
                if 'data' in token_data:
                    position.entry_sol_in_curve = token_data['data'].get('vSolInBondingCurve', 30)
                
                self.positions[mint] = position
                self.total_trades += 1
                
                logger.info(f"✅ BUY EXECUTED: {mint[:8]}...")
                logger.info(f"   Amount: {BUY_AMOUNT_SOL} SOL")
                logger.info(f"   Tokens: {bought_tokens:,.0f}")
                logger.info(f"   Entry MC: ${entry_market_cap:,.0f}")
                logger.info(f"   Active positions: {len(self.positions)}/{MAX_POSITIONS}")
                
                if self.telegram:
                    await self.telegram.notify_buy(mint, BUY_AMOUNT_SOL, signature)
                
                # Start monitoring
                position.monitor_task = asyncio.create_task(self._monitor_position(mint))
                logger.info(f"📊 Started monitoring position {mint[:8]}...")
            else:
                self.tracker.log_buy_failed(mint, BUY_AMOUNT_SOL, "Transaction failed")
                
        except Exception as e:
            logger.error(f"Failed to process token: {e}")
            self.tracker.log_buy_failed(mint, BUY_AMOUNT_SOL, str(e))
    
    async def _monitor_position(self, mint: str):
        """Monitor position with CORRECTED P&L tracking"""
        try:
            position = self.positions.get(mint)
            if not position:
                return
            
            # Grace period
            logger.info(f"⏳ Grace period {SELL_DELAY_SECONDS}s for {mint[:8]}...")
            await asyncio.sleep(SELL_DELAY_SECONDS)
            
            logger.info(f"📈 Starting active monitoring for {mint[:8]}...")
            logger.info(f"   Entry MC: ${position.entry_market_cap:,.0f}")
            logger.info(f"   Entry Price: {position.entry_token_price_sol:.10f} SOL per token")
            logger.info(f"   Your Tokens: {position.remaining_tokens:,.0f}")
            
            check_count = 0
            last_notification_pnl = 0
            consecutive_data_failures = 0
            sol_price_usd = 250
            
            while mint in self.positions and position.status == 'active':
                check_count += 1
                age = time.time() - position.entry_time
                
                # Check age limit
                if age > MAX_POSITION_AGE_SECONDS:
                    logger.warning(f"⏰ MAX AGE REACHED for {mint[:8]}... ({age:.0f}s)")
                    await self._close_position_full(mint, reason="max_age")
                    break
                
                try:
                    # Get current price data
                    curve_data = self.dex.get_bonding_curve_data(mint)
                    
                    if curve_data and curve_data.get('is_migrated'):
                        logger.warning(f"❌ Token {mint[:8]}... has migrated")
                        await self._close_position_full(mint, reason="migration")
                        break
                    
                    if curve_data and curve_data.get('sol_in_curve', 0) > 0:
                        # Calculate current MC for logging
                        current_mc = self._calculate_mc_from_curve(curve_data, sol_price_usd)
                        position.current_market_cap = current_mc
                        
                        # Get current token price directly from bonding curve (already calculated by dex.py)
                        price_per_token_lamports = curve_data.get('price_per_token', 0)
                        current_token_price_sol = price_per_token_lamports / 1e9  # Convert lamports to SOL
                        
                        # Calculate YOUR position P&L using actual bonding curve prices
                        if current_token_price_sol > 0 and position.entry_token_price_sol > 0:
                            # Calculate YOUR position value
                            your_current_value_sol = position.remaining_tokens * current_token_price_sol
                            your_entry_value_sol = position.remaining_tokens * position.entry_token_price_sol
                            
                            # Calculate YOUR actual P&L
                            if your_entry_value_sol > 0:
                                position.pnl_percent = ((your_current_value_sol / your_entry_value_sol) - 1) * 100
                            else:
                                position.pnl_percent = 0
                            
                            # Store current price
                            position.current_price = current_token_price_sol
                        else:
                            position.pnl_percent = 0
                        
                        consecutive_data_failures = 0
                        position.last_price_update = time.time()
                        
                        if check_count % 10 == 1:
                            self.tracker.log_position_update(mint, position.pnl_percent, current_mc, age)
                        
                        if check_count % 3 == 1:
                            logger.info(
                                f"📊 {mint[:8]}... | YOUR P&L: {position.pnl_percent:+.1f}% | "
                                f"MC: ${position.entry_market_cap:,.0f}→${current_mc:,.0f} | "
                                f"Sold: {position.total_sold_percent}% | Age: {age:.0f}s"
                            )
                        
                        # Telegram updates
                        if self.telegram and abs(position.pnl_percent - last_notification_pnl) >= 50:
                            update_msg = (
                                f"📊 Update {mint[:8]}...\n"
                                f"YOUR P&L: {position.pnl_percent:+.1f}%\n"
                                f"MC: ${position.entry_market_cap:,.0f}→${current_mc:,.0f}\n"
                                f"Remaining: {100 - position.total_sold_percent}%"
                            )
                            await self.telegram.send_message(update_msg)
                            last_notification_pnl = position.pnl_percent
                        
                        # Check stop loss FIRST
                        if position.pnl_percent <= -STOP_LOSS_PERCENTAGE and position.total_sold_percent < 100:
                            logger.warning(f"🛑 STOP LOSS HIT for {mint[:8]}...")
                            await self._close_position_full(mint, reason="stop_loss")
                            break
                        
                        # Then check profit targets
                        for target in position.profit_targets:
                            target_name = target['name']
                            target_pnl = target['target']
                            sell_percent = target['sell_percent']
                            
                            if position.pnl_percent >= target_pnl and target_name not in position.partial_sells:
                                logger.info(f"🎯 {target_name} TARGET HIT for {mint[:8]}...")
                                
                                success = await self._execute_partial_sell(
                                    mint, sell_percent, target_name, position.pnl_percent
                                )
                                
                                if success:
                                    position.partial_sells[target_name] = {
                                        'pnl': position.pnl_percent,
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
                            logger.warning(f"Too many data failures for {mint[:8]}...")
                            await asyncio.sleep(1)
                            continue
                        else:
                            await asyncio.sleep(1)
                            continue
                
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
        """Execute a partial sell with REALISTIC profit calculation"""
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
            
            logger.info(f"💰 Executing {target_name} partial sell for {mint[:8]}...")
            logger.info(f"   Selling: {sell_percent}% ({ui_tokens_to_sell:,.2f} tokens)")
            logger.info(f"   YOUR P&L: {current_pnl:+.1f}%")
            
            # Calculate realistic SOL received based on YOUR actual P&L
            base_sol_for_portion = position.amount_sol * (sell_percent / 100)
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
                    slippage=50,
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
                
                logger.info(f"✅ {target_name} SELL EXECUTED")
                logger.info(f"   Est. received: {sol_received:.4f} SOL")
                logger.info(f"   Est. profit: {profit_sol:+.4f} SOL")
                
                if self.telegram:
                    msg = (
                        f"💰 {target_name} TARGET HIT!\n"
                        f"Token: {mint[:16]}...\n"
                        f"Sold: {sell_percent}% of position\n"
                        f"YOUR P&L: {current_pnl:+.1f}%\n"
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
            
            logger.info(f"📤 Closing remaining {remaining_percent}% of {mint[:8]}...")
            
            # Calculate realistic SOL based on YOUR actual P&L
            base_sol_for_portion = position.amount_sol * (remaining_percent / 100)
            multiplier = 1 + (position.pnl_percent / 100)
            
            if reason == "migration":
                multiplier *= 0.8
            
            sol_received = base_sol_for_portion * multiplier
            final_pnl = sol_received - base_sol_for_portion
            
            if DRY_RUN:
                logger.info(f"[DRY RUN] Would sell {ui_token_balance:,.2f} tokens")
                signature = f"dry_run_close_{mint[:10]}"
            else:
                actual_balance = self.wallet.get_token_balance(mint)
                if actual_balance > 0:
                    ui_token_balance = actual_balance
                
                curve_data = self.dex.get_bonding_curve_data(mint)
                is_migrated = curve_data is None or curve_data.get('is_migrated', False)
                
                token_decimals = self.wallet.get_token_decimals(mint)
                
                signature = await self.trader.create_sell_transaction(
                    mint=mint,
                    token_amount=ui_token_balance,
                    slippage=100 if is_migrated else 50,
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
                
                logger.info(f"✅ POSITION CLOSED: {mint[:8]}...")
                logger.info(f"   Reason: {reason}")
                logger.info(f"   YOUR P&L: {position.pnl_percent:+.1f}%")
                logger.info(f"   Est. realized: {position.realized_pnl_sol:+.4f} SOL")
                logger.info(f"   Consecutive losses: {self.consecutive_losses}")
                
                if self.telegram:
                    emoji = "💰" if position.realized_pnl_sol > 0 else "🔴"
                    msg = (
                        f"{emoji} POSITION CLOSED\n"
                        f"Token: {mint[:16]}\n"
                        f"Reason: {reason}\n"
                        f"YOUR P&L: {position.pnl_percent:+.1f}%\n"
                        f"Est. realized: {position.realized_pnl_sol:+.4f} SOL"
                    )
                    if self.consecutive_losses >= 2:
                        msg += f"\n⚠️ Losses: {self.consecutive_losses}/3"
                    await self.telegram.send_message(msg)
            else:
                logger.error(f"❌ Close transaction failed")
                position.status = 'close_failed'
                
                if reason in ["migration", "max_age", "no_data"]:
                    logger.warning(f"Removing unsellable position {mint[:8]}...")
                    if self.telegram:
                        await self.telegram.send_message(
                            f"⚠️ Could not sell {mint[:16]}\n"
                            f"Reason: {reason}\nRemoving to free slot"
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
            
            logger.info("✅ Bot running with PATH B Configuration (FIXED P&L)")
            logger.info(f"⏱️ Grace period: {SELL_DELAY_SECONDS}s, Max hold: {MAX_POSITION_AGE_SECONDS}s")
            logger.info(f"🎯 Circuit breaker: 3 consecutive losses")
            
            last_stats_time = time.time()
            
            while self.running and not self.shutdown_requested:
                await asyncio.sleep(10)
                
                # Periodic stats
                if time.time() - last_stats_time > 60:
                    if self.positions:
                        logger.info(f"📊 ACTIVE POSITIONS: {len(self.positions)}")
                        for mint, pos in self.positions.items():
                            age = time.time() - pos.entry_time
                            targets_hit = ', '.join(pos.partial_sells.keys()) if pos.partial_sells else 'None'
                            logger.info(
                                f"  • {mint[:8]}... | YOUR P&L: {pos.pnl_percent:+.1f}% | "
                                f"Sold: {pos.total_sold_percent}% | Targets: {targets_hit} | "
                                f"Age: {age:.0f}s"
                            )
                    
                    perf_stats = self.tracker.get_session_stats()
                    if perf_stats['total_buys'] > 0:
                        logger.info(f"📊 SESSION PERFORMANCE:")
                        logger.info(f"  • Trades: {perf_stats['total_buys']} buys, {perf_stats['total_sells']} sells")
                        logger.info(f"  • Win rate: {perf_stats['win_rate_percent']:.1f}%")
                        logger.info(f"  • P&L: {perf_stats['total_pnl_sol']:+.4f} SOL")
                        logger.info(f"  • Session losses: {self.session_loss_count}")
                        logger.info(f"  • Consecutive losses: {self.consecutive_losses}/3")
                    
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
