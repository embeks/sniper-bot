"""
Main Orchestrator - Path B: MC-Based Entry & FIXED Balance Verification
UPDATED: Added liquidity validation before buys
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
    PARTIAL_TAKE_PROFIT, LIQUIDITY_MULTIPLIER,
    MIN_LIQUIDITY_SOL, MAX_SLIPPAGE_PERCENT
)

from wallet import WalletManager
from dex import PumpFunDEX
from pumpportal_monitor import PumpPortalMonitor
from pumpportal_trader import PumpPortalTrader
from performance_tracker import PerformanceTracker
from curve_reader import BondingCurveReader

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
        self.pending_sells = set()
        self.pending_token_amounts = {}
        self.total_sold_percent = 0
        self.realized_pnl_sol = 0
        
        # CRITICAL FIX: Prevent multiple simultaneous closes
        self.is_closing = False
        
        # CRITICAL HOTFIX: Track retry attempts
        self.retry_counts = {}
        
        # Price tracking
        self.last_valid_price = 0
        self.last_price_update = time.time()
        self.consecutive_stale_reads = 0
        self.last_valid_balance = tokens
        self.curve_check_retries = 0
        
        # Volume and momentum tracking
        self.consecutive_no_movement = 0
        self.last_checked_price = 0
        
        # MC-based tracking
        self.entry_market_cap = entry_market_cap
        self.current_market_cap = entry_market_cap
        self.entry_sol_in_curve = 0
        
        # FIXED: Calculate actual price paid
        if tokens > 0:
            self.entry_token_price_sol = amount_sol / tokens
        else:
            self.entry_token_price_sol = 0
        
        # Build profit targets from environment
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
            logger.info(f"Profit targets configured: {targets_str}")
        else:
            logger.warning("No profit targets configured")

class SniperBot:
    """Main sniper bot orchestrator with liquidity validation"""
    
    def __init__(self):
        """Initialize all components"""
        logger.info("=" * 60)
        logger.info("🚀 INITIALIZING SNIPER BOT - WITH LIQUIDITY VALIDATION")
        logger.info("=" * 60)
        
        # Core components
        self.wallet = WalletManager()
        self.dex = PumpFunDEX(self.wallet)
        self.scanner = None
        self.scanner_task = None
        self.telegram = None
        self.telegram_polling_task = None
        self.tracker = PerformanceTracker()
        
        # Initialize curve reader for liquidity validation
        from solana.rpc.api import Client
        from config import RPC_ENDPOINT, PUMPFUN_PROGRAM_ID
        
        rpc_client = Client(RPC_ENDPOINT.replace('wss://', 'https://').replace('ws://', 'http://'))
        self.curve_reader = BondingCurveReader(rpc_client, PUMPFUN_PROGRAM_ID)
        
        # Initialize trader
        client = Client(RPC_ENDPOINT.replace('wss://', 'https://').replace('ws://', 'http://'))
        self.trader = PumpPortalTrader(self.wallet, client)
        
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
        
        logger.info(f"📊 STARTUP STATUS:")
        logger.info(f"  • Strategy: MC + Holder + Liquidity Validation")
        logger.info(f"  • Liquidity gate: {LIQUIDITY_MULTIPLIER}x buy size (min {MIN_LIQUIDITY_SOL} SOL)")
        logger.info(f"  • Max slippage: {MAX_SLIPPAGE_PERCENT}%")
        logger.info(f"  • Wallet: {self.wallet.pubkey}")
        logger.info(f"  • Balance: {sol_balance:.4f} SOL")
        logger.info(f"  • Max positions: {MAX_POSITIONS}")
        logger.info(f"  • Buy amount: {BUY_AMOUNT_SOL} SOL")
        logger.info(f"  • Stop loss: -{STOP_LOSS_PERCENTAGE}%")
        logger.info(f"  • Targets: 2x/3x/5x (REAL SOL TRACKING)")
        logger.info(f"  • Available trades: {actual_trades}")
        logger.info(f"  • Circuit breaker: 3 consecutive losses")
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
            
            # Calculate SOL delta
            sol_delta = 0.0
            account_keys = [str(key) for key in tx.transaction.transaction.message.account_keys]
            
            try:
                wallet_index = account_keys.index(my_pubkey_str)
                pre_sol_lamports = meta.pre_balances[wallet_index]
                post_sol_lamports = meta.post_balances[wallet_index]
                sol_delta = (post_sol_lamports - pre_sol_lamports) / 1e9
            except (ValueError, IndexError) as e:
                logger.warning(f"Wallet not found in transaction accounts: {e}")
            
            # Calculate token delta
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
                    "🚀 Bot started - WITH LIQUIDITY VALIDATION\n"
                    f"💰 Balance: {sol_balance:.4f} SOL\n"
                    f"🎯 Buy: {BUY_AMOUNT_SOL} SOL\n"
                    f"🛡️ Liquidity: {LIQUIDITY_MULTIPLIER}x\n"
                    f"📊 Max slippage: {MAX_SLIPPAGE_PERCENT}%\n"
                    f"📈 Targets: 2x/3x/5x\n"
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
        """Handle new token found - with liquidity validation"""
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
            
            # Check total positions
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
            
            # LIQUIDITY VALIDATION - critical new check
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
            
            # Get entry price from curve
            entry_price = curve_data['price_sol_per_token']
            
            # Estimate slippage
            estimated_slippage = self.curve_reader.estimate_slippage(mint, BUY_AMOUNT_SOL)
            if estimated_slippage:
                logger.info(f"📊 Estimated slippage: {estimated_slippage:.2f}%")
                if estimated_slippage > MAX_SLIPPAGE_PERCENT:
                    logger.warning(f"⚠️ High estimated slippage ({estimated_slippage:.2f}% > {MAX_SLIPPAGE_PERCENT}%), skipping")
                    return
            
            # Increment pending buys
            self.pending_buys += 1
            logger.debug(f"Pending buys: {self.pending_buys}, Active: {len(self.positions)}")
            
            # Extract entry market cap
            entry_market_cap = token_data.get('market_cap', 0)
            
            # Log detection
            detection_time_ms = (time.time() - detection_start) * 1000
            self.tracker.log_token_detection(mint, token_data.get('source', 'pumpportal'), detection_time_ms)
            
            logger.info(f"🎯 Processing new token: {mint}")
            logger.info(f"   Detection latency: {detection_time_ms:.0f}ms")
            logger.info(f"   Entry price: {entry_price:.10f} SOL per token")
            logger.info(f"   SOL raised: {curve_data['sol_raised']:.4f}")
            
            cost_breakdown = self.tracker.log_buy_attempt(mint, BUY_AMOUNT_SOL, 50)
            
            # Execute buy with tight slippage (30 BPS = 0.3%)
            execution_start = time.time()
            
            bonding_curve_key = None
            if 'data' in token_data and 'bondingCurveKey' in token_data['data']:
                bonding_curve_key = token_data['data']['bondingCurveKey']
            
            # Calculate expected tokens
            expected_tokens = BUY_AMOUNT_SOL / entry_price if entry_price > 0 else 0
            
            signature = await self.trader.create_buy_transaction(
                mint=mint,
                sol_amount=BUY_AMOUNT_SOL,
                bonding_curve_key=bonding_curve_key,
                slippage=30,  # 0.3% - tight since liquidity validated
                urgency="normal"
            )
            
            bought_tokens = 0
            if signature:
                await asyncio.sleep(2)
                bought_tokens = self.wallet.get_token_balance(mint)
                if bought_tokens == 0:
                    bought_tokens = expected_tokens if expected_tokens > 0 else 350000
            
            if signature:
                execution_time_ms = (time.time() - execution_start) * 1000
                
                # Calculate actual entry price and slippage
                if bought_tokens > 0:
                    actual_entry_price = BUY_AMOUNT_SOL / bought_tokens
                    actual_slippage = ((actual_entry_price / entry_price) - 1) * 100 if entry_price > 0 else 0
                    logger.info(f"📊 Actual slippage: {actual_slippage:.2f}%")
                else:
                    actual_entry_price = entry_price
                
                self.tracker.log_buy_executed(
                    mint=mint,
                    amount_sol=BUY_AMOUNT_SOL,
                    signature=signature,
                    tokens_received=bought_tokens,
                    execution_time_ms=execution_time_ms
                )
                
                # Create position
                position = Position(mint, BUY_AMOUNT_SOL, bought_tokens, entry_market_cap)
                position.buy_signature = signature
                position.initial_tokens = bought_tokens
                position.remaining_tokens = bought_tokens
                position.last_valid_balance = bought_tokens
                position.entry_time = time.time()
                position.entry_token_price_sol = actual_entry_price
                
                if 'data' in token_data:
                    position.entry_sol_in_curve = token_data['data'].get('vSolInBondingCurve', 30)
                
                self.positions[mint] = position
                self.total_trades += 1
                self.pending_buys -= 1
                
                logger.info(f"✅ BUY EXECUTED: {mint[:8]}...")
                logger.info(f"   Amount: {BUY_AMOUNT_SOL} SOL")
                logger.info(f"   Tokens: {bought_tokens:,.0f}")
                logger.info(f"   Entry Price: {actual_entry_price:.10f} SOL per token")
                logger.info(f"   Active positions: {len(self.positions)}/{MAX_POSITIONS}")
                
                if self.telegram:
                    await self.telegram.notify_buy(mint, BUY_AMOUNT_SOL, signature)
                
                # Start monitoring
                position.monitor_task = asyncio.create_task(self._monitor_position(mint))
                logger.info(f"📊 Started monitoring position {mint[:8]}...")
            else:
                self.pending_buys -= 1
                self.tracker.log_buy_failed(mint, BUY_AMOUNT_SOL, "Transaction failed")
                
        except Exception as e:
            self.pending_buys = max(0, self.pending_buys - 1)
            logger.error(f"Failed to process token: {e}")
            self.tracker.log_buy_failed(mint, BUY_AMOUNT_SOL, str(e))
    
    async def _monitor_position(self, mint: str):
        """Monitor position with RUG TRAP during grace period"""
        try:
            position = self.positions.get(mint)
            if not position:
                return
            
            # Calculate grace period end time
            grace_end_time = position.entry_time + SELL_DELAY_SECONDS
            
            logger.info(f"📈 Starting monitoring for {mint[:8]}... (grace: {SELL_DELAY_SECONDS}s)")
            logger.info(f"   Entry MC: ${position.entry_market_cap:,.0f}")
            logger.info(f"   Entry Price: {position.entry_token_price_sol:.10f} SOL per token")
            logger.info(f"   Your Tokens: {position.remaining_tokens:,.0f}")
            
            check_count = 0
            last_notification_pnl = 0
            consecutive_data_failures = 0
            
            while mint in self.positions and position.status == 'active':
                check_count += 1
                age = time.time() - position.entry_time
                in_grace_period = time.time() < grace_end_time
                
                # Check age limit
                if age > MAX_POSITION_AGE_SECONDS:
                    logger.warning(f"⏰ MAX AGE REACHED for {mint[:8]}... ({age:.0f}s)")
                    await self._close_position_full(mint, reason="max_age")
                    break
                
                try:
                    # Get current price data
                    curve_data = self.dex.get_bonding_curve_data(mint)
                    
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
                    
                    if curve_data and curve_data.get('is_migrated'):
                        logger.warning(f"❌ Token {mint[:8]}... has migrated")
                        await self._close_position_full(mint, reason="migration")
                        break
                    
                    if curve_data and curve_data.get('sol_in_curve', 0) > 0:
                        current_sol_in_curve = curve_data.get('sol_in_curve', 0)
                        
                        # Calculate actual token price
                        v_sol_reserves = curve_data.get('virtual_sol_reserves', 0)
                        v_token_reserves = curve_data.get('virtual_token_reserves', 0)
                        
                        if v_token_reserves > 0 and v_sol_reserves > 0:
                            sol_human = v_sol_reserves / 1e9
                            tokens_human = v_token_reserves / 1e6
                            
                            current_token_price_sol = sol_human / tokens_human
                            
                            if position.entry_token_price_sol > 0:
                                price_change = ((current_token_price_sol / position.entry_token_price_sol) - 1) * 100
                            else:
                                price_change = 0
                            
                            position.pnl_percent = price_change
                            position.current_price = current_token_price_sol
                            
                            consecutive_data_failures = 0
                            position.last_valid_price = current_token_price_sol
                            position.last_price_update = time.time()
                            
                            # ===================================================================
                            # 🚨 RUG TRAP - ALWAYS ACTIVE (even during grace period)
                            # ===================================================================
                            
                            if price_change <= -10 and not position.is_closing:
                                if in_grace_period:
                                    logger.warning(f"🚨 RUG TRAP TRIGGERED in grace period ({price_change:.1f}%) - immediate exit!")
                                else:
                                    logger.warning(f"🚨 MAJOR DUMP ({price_change:.1f}%) - immediate exit!")
                                
                                position.is_closing = True
                                await self._close_position_full(mint, reason="rug_trap")
                                break
                            
                            # ===================================================================
                            # GRACE PERIOD: Only log, no other actions
                            # ===================================================================
                            
                            if in_grace_period:
                                # During grace: only monitor, no profit-taking or other exits
                                remaining_grace = grace_end_time - time.time()
                                
                                if check_count % 3 == 1:
                                    logger.info(
                                        f"⏳ GRACE {mint[:8]}... | P&L: {price_change:+.1f}% | "
                                        f"Remaining: {remaining_grace:.1f}s"
                                    )
                                
                                # Skip all other checks during grace
                                await asyncio.sleep(MONITOR_CHECK_INTERVAL)
                                continue
                            
                            # ===================================================================
                            # AFTER GRACE: Normal monitoring logic
                            # ===================================================================
                            
                            # Volume-based exit
                            if position.last_checked_price == 0:
                                position.last_checked_price = current_token_price_sol
                            
                            if abs(current_token_price_sol - position.last_checked_price) < (position.last_checked_price * 0.001):
                                position.consecutive_no_movement += 1
                            else:
                                position.consecutive_no_movement = 0
                                position.last_checked_price = current_token_price_sol
                            
                            if position.consecutive_no_movement >= 7:
                                logger.warning(f"🚫 NO VOLUME for 15s - exiting {mint[:8]}...")
                                await self._close_position_full(mint, reason="no_volume")
                                break
                            
                            # Early dump detection
                            if age < 30 and price_change < -3:
                                logger.warning(f"🚫 EARLY DUMP ({price_change:.1f}%) - exiting {mint[:8]}...")
                                await self._close_position_full(mint, reason="early_dump")
                                break
                            
                            if check_count % 10 == 1:
                                self.tracker.log_position_update(mint, price_change, current_sol_in_curve, age)
                            
                            if check_count % 3 == 1:
                                logger.info(
                                    f"📊 {mint[:8]}... | P&L: {price_change:+.1f}% | "
                                    f"Price: {position.entry_token_price_sol:.10f}→{current_token_price_sol:.10f} SOL | "
                                    f"Sold: {position.total_sold_percent}% | Age: {age:.0f}s"
                                )
                            
                            # Telegram updates
                            if self.telegram and abs(price_change - last_notification_pnl) >= 50:
                                update_msg = (
                                    f"📊 Update {mint[:8]}...\n"
                                    f"P&L: {price_change:+.1f}%\n"
                                    f"Remaining: {100 - position.total_sold_percent}%"
                                )
                                await self.telegram.send_message(update_msg)
                                last_notification_pnl = price_change
                            
                            # Fast dump detection
                            if age < 15 and price_change < -8 and not position.is_closing:
                                logger.warning(f"🚫 FAST DUMP ({price_change:.1f}%) - emergency exit")
                                position.is_closing = True
                                await self._close_position_full(mint, reason="fast_dump")
                                break
                            
                            # Stop-loss
                            if price_change <= -STOP_LOSS_PERCENTAGE and not position.is_closing:
                                logger.warning(f"🛑 STOP LOSS HIT for {mint[:8]}...")
                                position.is_closing = True
                                await self._close_position_full(mint, reason="stop_loss")
                                break
                            
                            # Profit targets
                            if not position.is_closing:
                                for target in position.profit_targets:
                                    target_name = target['name']
                                    target_pnl = target['target']
                                    sell_percent = target['sell_percent']
                                    
                                    if (position.pnl_percent >= target_pnl and 
                                        target_name not in position.partial_sells):
                                        
                                        if target_name in position.pending_sells:
                                            logger.debug(f"{target_name} already pending, skipping")
                                            continue
                                        
                                        # Calculate available tokens
                                        pending_token_amount = 0
                                        for pending_target_name in position.pending_sells:
                                            if pending_target_name in position.pending_token_amounts:
                                                pending_token_amount += position.pending_token_amounts[pending_target_name]
                                        
                                        available_tokens = position.remaining_tokens - pending_token_amount
                                        tokens_needed = position.remaining_tokens * (sell_percent / 100)
                                        
                                        if available_tokens < tokens_needed * 0.95:
                                            logger.warning(
                                                f"⚠️ Not enough tokens for {target_name}: "
                                                f"Available: {available_tokens:,.0f}, Need: {tokens_needed:,.0f}"
                                            )
                                            continue
                                        
                                        logger.info(f"🎯 {target_name} TARGET HIT for {mint[:8]}...")
                                        
                                        position.pending_sells.add(target_name)
                                        position.pending_token_amounts[target_name] = tokens_needed
                                        
                                        try:
                                            success = await self._execute_partial_sell(
                                                mint, sell_percent, target_name, position.pnl_percent
                                            )
                                            if not success:
                                                position.pending_sells.discard(target_name)
                                                if target_name in position.pending_token_amounts:
                                                    del position.pending_token_amounts[target_name]
                                            break
                                        except Exception as e:
                                            position.pending_sells.discard(target_name)
                                            if target_name in position.pending_token_amounts:
                                                del position.pending_token_amounts[target_name]
                                            logger.error(f"Sell execution error: {e}")
                                            break
                        else:
                            consecutive_data_failures += 1
                            logger.warning(f"Invalid reserve data (failure {consecutive_data_failures}/{DATA_FAILURE_TOLERANCE})")
                            
                            if consecutive_data_failures > DATA_FAILURE_TOLERANCE:
                                if position.last_valid_price > 0:
                                    logger.debug(f"Using last valid price: {position.last_valid_price:.10f}")
                                else:
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
        """Execute partial sell with priority fees"""
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
        """Track ACTUAL SOL received from wallet balance changes"""
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
                    status = self.trader.client.get_signature_statuses([signature])
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
            
            if not confirmed:
                elapsed = time.time() - start
                if first_seen is None:
                    logger.warning(f"⏱️ Timeout: TX never appeared in RPC after {elapsed:.1f}s")
                else:
                    logger.warning(f"⏱️ Timeout: TX appeared at {first_seen:.1f}s but didn't confirm")
            
            if confirmed:
                txd = await self._get_transaction_deltas(signature, mint)
                if txd["confirmed"]:
                    actual_sol_received = txd["sol_delta"] if txd["sol_delta"] > 0 else None
                    actual_tokens_sold = abs(txd["token_delta"]) if txd["token_delta"] < 0 else None
                else:
                    actual_sol_received, actual_tokens_sold = None, None

                if actual_sol_received is None:
                    logger.warning(f"Using wallet balance fallback for SOL")
                    await asyncio.sleep(2)
                    post_sol_balance = self.wallet.get_sol_balance()
                    actual_sol_received = post_sol_balance - pre_sol_balance

                if actual_tokens_sold is None:
                    logger.warning(f"Using wallet balance fallback for tokens")
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
                
                if self.telegram:
                    msg = (
                        f"💰 {target_name} CONFIRMED!\n"
                        f"Token: {mint[:16]}...\n"
                        f"Sold: {sell_percent}%\n"
                        f"P&L: {current_pnl:+.1f}%\n"
                        f"Profit: {actual_profit_sol:+.4f} SOL\n"
                        f"TX: https://solscan.io/tx/{signature}"
                    )
                    await self.telegram.send_message(msg)
                
                if position.total_sold_percent >= 100:
                    logger.info(f"✅ Position fully closed")
                    position.status = 'completed'
            else:
                logger.warning(f"❌ {target_name} sell failed for {mint[:8]}")
                
                retry_count = position.retry_counts.get(target_name, 0)
                if retry_count < 2:
                    position.retry_counts[target_name] = retry_count + 1
                    logger.info(f"Retrying {target_name} (attempt {retry_count + 2}/3) with critical urgency")
                    
                    if target_name in position.pending_sells:
                        position.pending_sells.remove(target_name)
                    if target_name in position.pending_token_amounts:
                        del position.pending_token_amounts[target_name]
                    
                    token_decimals = self.wallet.get_token_decimals(mint)
                    ui_tokens_to_sell = round(position.remaining_tokens * (sell_percent / 100), token_decimals)
                    
                    retry_signature = await self.trader.create_sell_transaction(
                        mint=mint,
                        token_amount=ui_tokens_to_sell,
                        slippage=50,
                        token_decimals=token_decimals,
                        urgency="critical"
                    )
                    
                    if retry_signature and not retry_signature.startswith("1111111"):
                        position.pending_sells.add(target_name)
                        position.pending_token_amounts[target_name] = ui_tokens_to_sell
                        
                        asyncio.create_task(
                            self._confirm_sell_background(
                                retry_signature, mint, target_name, sell_percent,
                                ui_tokens_to_sell, current_pnl,
                                pre_sol_balance, pre_token_balance
                            )
                        )
                else:
                    logger.error(f"❌ Max retries exceeded for {target_name} on {mint[:8]}")
                    
                    if target_name in position.pending_sells:
                        position.pending_sells.remove(target_name)
                    if target_name in position.pending_token_amounts:
                        del position.pending_token_amounts[target_name]
                    
                    position.partial_sells[target_name] = {
                        'pnl': current_pnl,
                        'time': time.time(),
                        'percent_sold': 0,
                        'status': 'failed',
                        'attempts': retry_count + 1
                    }
                    
                    if self.telegram:
                        await self.telegram.send_message(
                            f"⚠️ Failed to sell {target_name} on {mint[:16]}\n"
                            f"Max retries exceeded - manual intervention needed"
                        )
                
        except Exception as e:
            logger.error(f"Confirmation error for {mint[:8]}: {e}")
            if mint in self.positions:
                position = self.positions[mint]
                if target_name in position.pending_sells:
                    position.pending_sells.remove(target_name)
                if target_name in position.pending_token_amounts:
                    del position.pending_token_amounts[target_name]
                
                position.partial_sells[target_name] = {
                    'pnl': current_pnl,
                    'time': time.time(),
                    'percent_sold': 0,
                    'status': 'error',
                    'error': str(e)
                }
    
    async def _close_position_full(self, mint: str, reason: str = "manual"):
        """Close remaining position with REAL SOL tracking and critical urgency"""
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
                return
            
            remaining_percent = 100 - position.total_sold_percent
            hold_time = time.time() - position.entry_time
            
            logger.info(f"📤 Closing remaining {remaining_percent}% of {mint[:8]}...")
            
            pre_sol_balance = self.wallet.get_sol_balance()
            
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
                token_decimals=token_decimals,
                urgency="critical"
            )
            
            if signature and not signature.startswith("1111111"):
                await asyncio.sleep(3)
                
                txd = await self._get_transaction_deltas(signature, mint)
                actual_tokens_sold = None
                if txd["confirmed"] and txd["sol_delta"] > 0:
                    actual_sol_received = txd["sol_delta"]
                    if txd.get("token_delta", 0.0) < 0:
                        actual_tokens_sold = abs(txd["token_delta"])
                else:
                    post_sol_balance = self.wallet.get_sol_balance()
                    actual_sol_received = post_sol_balance - pre_sol_balance
                
                if actual_tokens_sold is None:
                    before_tokens = ui_token_balance
                    after_tokens = self.wallet.get_token_balance(mint)
                    actual_tokens_sold = max(0.0, before_tokens - after_tokens)
                
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
                    
                self.total_pnl += position.pnl_percent
                position.realized_pnl_sol += final_pnl_sol
                self.total_realized_sol += final_pnl_sol
                
                self.tracker.log_sell_executed(
                    mint=mint,
                    tokens_sold=actual_tokens_sold,
                    signature=signature,
                    sol_received=actual_sol_received,
                    pnl_sol=position.realized_pnl_sol,
                    pnl_percent=position.pnl_percent,
                    hold_time_seconds=hold_time,
                    reason=reason
                )
                
                logger.info(f"✅ POSITION CLOSED: {mint[:8]}...")
                logger.info(f"   Reason: {reason}")
                logger.info(f"   P&L: {position.pnl_percent:+.1f}%")
                logger.info(f"   Realized: {position.realized_pnl_sol:+.4f} SOL")
                logger.info(f"   Consecutive losses: {self.consecutive_losses}")
                
                if self.telegram:
                    emoji = "💰" if position.realized_pnl_sol > 0 else "🔴"
                    msg = (
                        f"{emoji} POSITION CLOSED\n"
                        f"Token: {mint[:16]}\n"
                        f"Reason: {reason}\n"
                        f"P&L: {position.pnl_percent:+.1f}%\n"
                        f"Realized: {position.realized_pnl_sol:+.4f} SOL"
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
            
            logger.info("✅ Bot running with liquidity validation")
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
                            pending = ', '.join(pos.pending_sells) if pos.pending_sells else 'None'
                            logger.info(
                                f"  • {mint[:8]}... | P&L: {pos.pnl_percent:+.1f}% | "
                                f"Sold: {pos.total_sold_percent}% | Targets: {targets_hit} | "
                                f"Pending: {pending} | Age: {age:.0f}s"
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
